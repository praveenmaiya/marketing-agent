"""BigQuery tools for the marketing agent.

Wraps the existing BQClient to provide tool-calling interface for the LLM.
Includes SQL safety guard: only SELECT/WITH queries are allowed (allowlist).
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
import time
from pathlib import Path

import pandas as pd

from src.agent.config import AgentBehaviorConfig, BigQueryConfig, GCSConfig
from src.agent.tools import ToolDef
from src.bq_client import BQClient
from src.gcs_utils import upload_blob

logger = logging.getLogger(__name__)

# Allowlist: first keyword must be SELECT or WITH (after whitespace/comments)
_ALLOWED_SQL_START = re.compile(
    r"^\s*(?:(?:--[^\n]*\n|/\*.*?\*/)\s*)*(SELECT|WITH)\b",
    re.IGNORECASE | re.DOTALL,
)

# When WITH is matched, verify the outer statement is SELECT (not DML after CTEs).
# Strips CTE bodies then checks the remaining text starts with SELECT.
_WITH_OUTER_SELECT = re.compile(
    r"^\s*(?:(?:--[^\n]*\n|/\*.*?\*/)\s*)*SELECT\b",
    re.IGNORECASE | re.DOTALL,
)

# Reject multi-statement scripts: semicolon followed by non-whitespace
# (applied after stripping literals/comments to avoid false positives)
_HAS_MULTI_STATEMENT = re.compile(r";\s*\S")

# Patterns to strip literals/comments/quoted identifiers before paren walking
_STRING_LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'")
_DOUBLE_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')
_BACKTICK_QUOTED = re.compile(r'`[^`]*`')
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")

# Max rows shown in the buffered-result preview sent to the LLM
_PREVIEW_ROWS = 10

# Max characters for preview output (handles wide/text-heavy columns)
_PREVIEW_MAX_CHARS = 4000

# Safety cap: max rows materialized into memory to prevent OOM
_MAX_MATERIALIZE_ROWS = 10_000

# Clamp bounds for max_rows parameter
_MAX_ROWS_LOWER = 1
_MAX_ROWS_UPPER = 1000


def _strip_noise(sql: str) -> str:
    """Strip literals, quoted identifiers, and comments from SQL."""
    s = _STRING_LITERAL.sub("", sql)
    s = _DOUBLE_QUOTED.sub("", s)
    s = _BACKTICK_QUOTED.sub("", s)
    s = _BLOCK_COMMENT.sub("", s)
    s = _LINE_COMMENT.sub("", s)
    return s


def _is_safe_sql(sql: str) -> bool:
    """Check SQL is a safe read-only query using allowlist approach.

    Rules:
    - First keyword must be SELECT or WITH (after whitespace/comments)
    - WITH must lead to SELECT (not DELETE/UPDATE/INSERT/MERGE after CTEs)
    - No multi-statement scripts (semicolon followed by more SQL)
    - Literals and comments are stripped before all checks to keep offsets consistent
    """
    stripped = _strip_noise(sql)
    if _HAS_MULTI_STATEMENT.search(stripped):
        return False
    # Match on stripped (not original sql) so m.end() is valid for indexing stripped
    m = _ALLOWED_SQL_START.match(stripped)
    if not m:
        return False
    if m.group(1).upper() == "SELECT":
        return True
    # WITH: verify the outer statement (after all CTEs) is SELECT.
    # Walk past balanced parentheses in CTE definitions.
    after_with = stripped[m.end():]
    depth = 0
    i = 0
    while i < len(after_with):
        ch = after_with[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                rest = after_with[i + 1:].lstrip()
                # CTE column list: name(col1, ...) AS (...) — skip into the body
                col_match = re.match(r'(?i)AS\s*\(', rest)
                if col_match:
                    ws = len(after_with[i + 1:]) - len(rest)
                    paren_pos = i + 1 + ws + col_match.end() - 1
                    depth = 1
                    i = paren_pos + 1
                    continue
                # Comma separates CTEs — advance past it
                elif rest.startswith(","):
                    i = after_with.index(",", i + 1) + 1
                else:
                    # This should be the final SELECT
                    return bool(_WITH_OUTER_SELECT.match(rest))
        i += 1
    return False


def _make_client(config: BigQueryConfig) -> BQClient:
    return BQClient(
        project=config.project,
        dataset=config.dataset,
        location=config.location,
    )


def _buffer_to_gcs(
    df: pd.DataFrame,
    sql: str,
    bucket: str,
    session_id: str,
    artifact_prefix: str = "agent/artifacts/",
) -> str | None:
    """Write *df* to GCS as CSV. Returns the GCS URI, or None on failure."""
    sql_hash = hashlib.sha256(sql.encode()).hexdigest()[:8]
    ts = int(time.time())
    rand = os.urandom(4).hex()
    blob_name = f"{artifact_prefix}query_{session_id}_{ts}_{sql_hash}_{rand}.csv"
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        df.to_csv(tmp_path, index=False)
        uri = upload_blob(tmp_path, bucket, blob_name)
        return uri
    except Exception:
        logger.warning("GCS buffering failed — falling back to inline", exc_info=True)
        return None
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                logger.debug("Failed to clean up temp file: %s", tmp_path)


def _truncate_preview(markdown: str, max_chars: int = _PREVIEW_MAX_CHARS) -> str:
    """Truncate markdown preview to stay within character budget."""
    if len(markdown) <= max_chars:
        return markdown
    return markdown[:max_chars] + "\n\n... (preview truncated)"


def _format_buffered_result(df: pd.DataFrame, gcs_uri: str) -> str:
    """Return a compact summary + preview for the LLM."""
    preview = df.head(_PREVIEW_ROWS)
    columns = ", ".join(df.columns)
    preview_md = _truncate_preview(preview.to_markdown(index=False))
    return (
        f"Query returned {len(df)} rows — buffered to GCS.\n"
        f"File: {gcs_uri}\n"
        f"Columns: {columns}\n"
        f"Row count: {len(df)}\n\n"
        f"Preview (first {len(preview)} rows):\n\n"
        f"{preview_md}"
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
        raw_max_rows = params.get("max_rows", 100)
        max_rows = min(max(raw_max_rows, _MAX_ROWS_LOWER), _MAX_ROWS_UPPER)

        # SQL allowlist guard: only SELECT/WITH allowed, no multi-statement
        if not _is_safe_sql(sql):
            return (
                "ERROR: Only SELECT/WITH queries are allowed. "
                "DDL/DML and multi-statement scripts are blocked."
            )

        if dry_run:
            client.run_query(sql, dry_run=True)
            return "Dry run succeeded. Query is valid."

        # Request one extra row to detect if results were capped
        df = client.run_query(sql, max_results=_MAX_MATERIALIZE_ROWS + 1)

        capped = len(df) > _MAX_MATERIALIZE_ROWS
        if capped:
            df = df.head(_MAX_MATERIALIZE_ROWS)
        cap_note = (
            f"\nNote: Results capped at {_MAX_MATERIALIZE_ROWS} rows. "
            "Add LIMIT or WHERE to narrow your query."
            if capped else ""
        )

        # Track whether GCS buffering was attempted and failed
        gcs_fallback = False

        # Context buffering: large results go to GCS
        if len(df) > threshold and buffering_enabled:
            gcs_uri = _buffer_to_gcs(
                df, sql, gcs_config.bucket, session_id,
                artifact_prefix=gcs_config.artifact_prefix,
            )
            if gcs_uri:
                return _format_buffered_result(df, gcs_uri) + cap_note
            gcs_fallback = True

        fallback_warning = (
            "WARNING: GCS buffering failed — showing inline fallback.\n\n"
            if gcs_fallback else ""
        )

        if len(df) > max_rows:
            preview = df.head(max_rows)
            table_md = _truncate_preview(preview.to_markdown(index=False))
            return (
                f"{fallback_warning}"
                f"Query returned {len(df)} rows (showing first {max_rows}):\n\n"
                f"{table_md}{cap_note}"
            )
        table_md = _truncate_preview(df.to_markdown(index=False))
        return (
            f"{fallback_warning}"
            f"Query returned {len(df)} rows:\n\n"
            f"{table_md}{cap_note}"
        )

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
                f"Small results (<={threshold} rows) are returned inline. "
                f"Large results (>{threshold} rows) are buffered to GCS and you receive a preview + file path. "
                f"Results are capped at {_MAX_MATERIALIZE_ROWS} rows to prevent memory issues. "
                "Use dry_run=true to validate syntax and estimate cost without running. "
                "Only SELECT/WITH queries are allowed — DDL/DML is blocked. "
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
                        "description": "Max rows to return in preview (default 100, range 1-1000)",
                        "default": 100,
                        "minimum": 1,
                        "maximum": 1000,
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
