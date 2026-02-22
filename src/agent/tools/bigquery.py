"""BigQuery tools for the marketing agent.

Wraps the existing BQClient to provide tool-calling interface for the LLM.
Includes SQL injection guard: only SELECT queries are allowed.
Large results (>threshold rows) are buffered to GCS as CSV; the LLM receives
a preview + metadata instead of the full dataset.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path

import pandas as pd

from src.agent.config import AgentBehaviorConfig, BigQueryConfig, GCSConfig
from src.agent.tools import ToolDef
from src.bq_client import BQClient
from src.gcs_utils import upload_blob

logger = logging.getLogger(__name__)

# Guard: reject mutating SQL statements
_FORBIDDEN_SQL = re.compile(
    r"\b(DROP|DELETE|TRUNCATE|INSERT|UPDATE|MERGE|CREATE|ALTER|GRANT|REVOKE)\b",
    re.IGNORECASE,
)

# Max rows shown in the buffered-result preview sent to the LLM
_PREVIEW_ROWS = 10


def _make_client(config: BigQueryConfig) -> BQClient:
    return BQClient(
        project=config.project,
        dataset=config.dataset,
        location=config.location,
    )


def _buffer_to_gcs(
    df: pd.DataFrame, sql: str, bucket: str, session_id: str
) -> str | None:
    """Write *df* to GCS as CSV. Returns the GCS URI, or None on failure."""
    sql_hash = hashlib.sha256(sql.encode()).hexdigest()[:8]
    blob_name = f"agent/artifacts/query_{session_id}_{sql_hash}.csv"
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".csv")
        # Close the fd immediately; to_csv will reopen by path.
        os.close(fd)
        df.to_csv(tmp_path, index=False)
        uri = upload_blob(tmp_path, bucket, blob_name)
        return uri
    except Exception:
        logger.warning("GCS buffering failed — falling back to inline", exc_info=True)
        return None
    finally:
        if tmp_path and Path(tmp_path).exists():
            Path(tmp_path).unlink()


def _format_buffered_result(df: pd.DataFrame, gcs_uri: str) -> str:
    """Return a compact summary + preview for the LLM."""
    preview = df.head(_PREVIEW_ROWS)
    columns = ", ".join(df.columns)
    return (
        f"Query returned {len(df)} rows — buffered to GCS.\n"
        f"File: {gcs_uri}\n"
        f"Columns: {columns}\n"
        f"Row count: {len(df)}\n\n"
        f"Preview (first {len(preview)} rows):\n\n"
        f"{preview.to_markdown(index=False)}"
    )


def create_bigquery_tools(
    config: BigQueryConfig,
    gcs_config: GCSConfig | None = None,
    session_id: str | None = None,
    agent_behavior: AgentBehaviorConfig | None = None,
) -> list[ToolDef]:
    """Create BigQuery tools using the project's existing BQClient."""
    client = _make_client(config)

    threshold = agent_behavior.context_buffer_threshold if agent_behavior else 50
    buffering_enabled = gcs_config is not None and session_id is not None

    def query_bigquery(params: dict) -> str:
        """Execute a SELECT query against BigQuery."""
        sql = params["sql"]
        dry_run = params.get("dry_run", False)
        max_rows = params.get("max_rows", 100)

        # SQL injection guard: only allow SELECT
        if _FORBIDDEN_SQL.search(sql):
            return "ERROR: Only SELECT queries are allowed. DDL/DML statements are blocked."

        if dry_run:
            client.run_query(sql, dry_run=True)
            return "Dry run succeeded. Query is valid."

        df = client.run_query(sql)

        # Context buffering: large results go to GCS
        if len(df) > threshold and buffering_enabled:
            gcs_uri = _buffer_to_gcs(df, sql, gcs_config.bucket, session_id)
            if gcs_uri:
                return _format_buffered_result(df, gcs_uri)
            # GCS failed — fall through to inline

        if len(df) > max_rows:
            preview = df.head(max_rows)
            return (
                f"Query returned {len(df)} rows (showing first {max_rows}):\n\n"
                f"{preview.to_markdown(index=False)}"
            )
        return f"Query returned {len(df)} rows:\n\n{df.to_markdown(index=False)}"

    def get_table_schema(params: dict) -> str:
        """Get the schema of a BigQuery table."""
        table_id = params["table_id"]
        schema = client.get_table_schema(table_id)
        return json.dumps(schema, indent=2)

    def check_table_exists(params: dict) -> str:
        """Check if a BigQuery table exists."""
        table_id = params["table_id"]
        exists = client.table_exists(table_id)
        return json.dumps({"table_id": table_id, "exists": exists})

    return [
        ToolDef(
            name="query_bigquery",
            description=(
                "Execute a SELECT query against BigQuery and return results as a markdown table. "
                "Small results (<=50 rows) are returned inline. "
                "Large results (>50 rows) are buffered to GCS and you receive a preview + file path. "
                "Use dry_run=true to validate syntax and estimate cost without running. "
                "Only SELECT queries are allowed — DDL/DML is blocked. "
                "Always use SAFE_DIVIDE for divisions. Use partition filters for large tables."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "The SELECT query to execute",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true, only validate and estimate cost",
                        "default": False,
                    },
                    "max_rows": {
                        "type": "integer",
                        "description": "Max rows to return in preview (default 100)",
                        "default": 100,
                    },
                },
                "required": ["sql"],
            },
            handler=query_bigquery,
        ),
        ToolDef(
            name="get_table_schema",
            description="Get the column names and types for a BigQuery table.",
            input_schema={
                "type": "object",
                "properties": {
                    "table_id": {
                        "type": "string",
                        "description": "Full table ID: project.dataset.table",
                    },
                },
                "required": ["table_id"],
            },
            handler=get_table_schema,
        ),
        ToolDef(
            name="check_table_exists",
            description="Check whether a BigQuery table exists.",
            input_schema={
                "type": "object",
                "properties": {
                    "table_id": {
                        "type": "string",
                        "description": "Full table ID: project.dataset.table",
                    },
                },
                "required": ["table_id"],
            },
            handler=check_table_exists,
        ),
    ]
