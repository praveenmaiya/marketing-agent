"""BigQuery tools for the marketing agent.

Wraps the existing BQClient to provide tool-calling interface for the LLM.
Includes SQL injection guard: only SELECT queries are allowed.
"""

from __future__ import annotations

import json
import logging
import re

from src.agent.config import BigQueryConfig
from src.agent.tools import ToolDef
from src.bq_client import BQClient

logger = logging.getLogger(__name__)

# Guard: reject mutating SQL statements
_FORBIDDEN_SQL = re.compile(
    r"\b(DROP|DELETE|TRUNCATE|INSERT|UPDATE|MERGE|CREATE|ALTER|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def _make_client(config: BigQueryConfig) -> BQClient:
    return BQClient(
        project=config.project,
        dataset=config.dataset,
        location=config.location,
    )


def create_bigquery_tools(config: BigQueryConfig) -> list[ToolDef]:
    """Create BigQuery tools using the project's existing BQClient."""
    client = _make_client(config)

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
