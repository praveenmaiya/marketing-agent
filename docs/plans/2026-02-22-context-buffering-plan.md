# Context Buffering Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Large BigQuery results (>50 rows) are buffered to GCS, and the LLM receives a preview + metadata instead of raw data.

**Architecture:** Modify the `query_bigquery` tool handler to detect large results and write them as CSV to GCS. The LLM sees a file reference, column names, row count, and a 10-row preview. Small results (<= 50 rows) remain inline as today. Graceful fallback to inline on GCS failure.

**Tech Stack:** Python 3.12, pandas, GCS (via `src/gcs_utils.upload_blob`), hashlib for file naming.

---

### Task 1: Add `context_buffer_threshold` to config

**Files:**
- Modify: `src/agent/config.py:47-53` (AgentBehaviorConfig dataclass)
- Modify: `configs/agent.yaml:32-38` (agent section)

**Step 1: Add field to AgentBehaviorConfig**

In `src/agent/config.py`, add `context_buffer_threshold` to `AgentBehaviorConfig`:

```python
@dataclass
class AgentBehaviorConfig:
    max_turns: int = 50
    max_plan_steps: int = 20
    checkpoint_interval: int = 5
    enable_subagents: bool = True
    enable_fan_out: bool = True
    fan_out_max_parallel: int = 20
    context_buffer_threshold: int = 50  # Rows above this get buffered to GCS
```

**Step 2: Add to agent.yaml**

In `configs/agent.yaml`, add under `agent:`:

```yaml
agent:
  max_turns: 50
  max_plan_steps: 20
  checkpoint_interval: 5
  enable_subagents: true
  enable_fan_out: true
  fan_out_max_parallel: 20
  context_buffer_threshold: 50  # Rows above this get buffered to GCS
```

**Step 3: Run existing config tests to verify nothing breaks**

Run: `python -m pytest tests/test_agent_config.py -v`
Expected: All existing tests PASS

**Step 4: Commit**

```bash
git add src/agent/config.py configs/agent.yaml
git commit -m "feat: add context_buffer_threshold config field"
```

---

### Task 2: Write failing tests for context buffering

**Files:**
- Modify: `tests/test_agent_tools.py` (add new test class at end of file)

**Step 1: Write the failing tests**

Add to `tests/test_agent_tools.py`:

```python
import hashlib
import json
from unittest.mock import MagicMock, patch

import pandas as pd

from src.agent.config import AgentBehaviorConfig, GCSConfig


class TestBigQueryContextBuffering:
    """Tests for context buffering in query_bigquery."""

    def _make_tools(self, threshold=50, session_id="test123"):
        """Create BigQuery tools with mocked client and GCS."""
        from src.agent.config import BigQueryConfig
        from src.agent.tools.bigquery import create_bigquery_tools

        config = BigQueryConfig(project="test", dataset="test_ds", location="US")
        gcs_config = GCSConfig(bucket="test-bucket")
        agent_config = AgentBehaviorConfig(context_buffer_threshold=threshold)

        with patch("src.agent.tools.bigquery._make_client") as mock_make:
            mock_client = MagicMock()
            mock_make.return_value = mock_client
            tools = create_bigquery_tools(
                config,
                gcs_config=gcs_config,
                session_id=session_id,
                agent_behavior=agent_config,
            )
        return tools, mock_client

    def test_small_result_returns_inline(self):
        """Results <= threshold stay inline (existing behavior)."""
        tools, mock_client = self._make_tools(threshold=50)
        query_tool = next(t for t in tools if t.name == "query_bigquery")

        # 10 rows — well under threshold
        df = pd.DataFrame({"col_a": range(10), "col_b": ["x"] * 10})
        mock_client.run_query.return_value = df

        result = query_tool.execute({"sql": "SELECT * FROM t"})
        assert "10 rows" in result
        assert "col_a" in result
        assert "buffered" not in result.lower()

    def test_large_result_buffers_to_gcs(self):
        """Results > threshold get buffered to GCS with preview + metadata."""
        tools, mock_client = self._make_tools(threshold=50, session_id="sess01")
        query_tool = next(t for t in tools if t.name == "query_bigquery")

        # 100 rows — above threshold
        df = pd.DataFrame({"user_id": range(100), "value": [1.5] * 100})
        mock_client.run_query.return_value = df

        with patch("src.agent.tools.bigquery.upload_blob") as mock_upload:
            mock_upload.return_value = "gs://test-bucket/agent/artifacts/query_sess01_abcd1234.csv"
            result = query_tool.execute({"sql": "SELECT * FROM t"})

        assert "100 rows" in result
        assert "buffered to GCS" in result
        assert "gs://" in result
        assert "Preview" in result
        assert "user_id" in result
        mock_upload.assert_called_once()

    def test_gcs_failure_falls_back_to_inline(self):
        """If GCS upload fails, fall back to inline markdown."""
        tools, mock_client = self._make_tools(threshold=50)
        query_tool = next(t for t in tools if t.name == "query_bigquery")

        df = pd.DataFrame({"col": range(100)})
        mock_client.run_query.return_value = df

        with patch("src.agent.tools.bigquery.upload_blob", side_effect=Exception("GCS down")):
            result = query_tool.execute({"sql": "SELECT * FROM t"})

        # Should fall back to inline with first max_rows
        assert "100 rows" in result
        assert "buffered" not in result.lower()

    def test_preview_has_10_rows_max(self):
        """Buffered preview shows at most 10 rows."""
        tools, mock_client = self._make_tools(threshold=50)
        query_tool = next(t for t in tools if t.name == "query_bigquery")

        df = pd.DataFrame({"id": range(200), "name": ["test"] * 200})
        mock_client.run_query.return_value = df

        with patch("src.agent.tools.bigquery.upload_blob") as mock_upload:
            mock_upload.return_value = "gs://test-bucket/agent/artifacts/query.csv"
            result = query_tool.execute({"sql": "SELECT * FROM t"})

        # Count data rows in the preview table (exclude header and separator)
        lines = [l for l in result.split("\n") if l.strip().startswith("|")]
        data_lines = [l for l in lines if "---" not in l and "id" not in l.split("|")[1]]
        assert len(data_lines) <= 10
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_agent_tools.py::TestBigQueryContextBuffering -v`
Expected: FAIL — `create_bigquery_tools` does not accept `gcs_config`, `session_id`, `agent_behavior` params yet.

**Step 3: Commit failing tests**

```bash
git add tests/test_agent_tools.py
git commit -m "test: add failing tests for BigQuery context buffering"
```

---

### Task 3: Implement context buffering in BigQuery tool

**Files:**
- Modify: `src/agent/tools/bigquery.py` (full file rewrite of the handler logic)

**Step 1: Update `create_bigquery_tools` signature and handler**

Rewrite `src/agent/tools/bigquery.py`:

```python
"""BigQuery tools for the marketing agent.

Wraps the existing BQClient to provide tool-calling interface for the LLM.
Includes SQL injection guard: only SELECT queries are allowed.
Large results (> context_buffer_threshold rows) are buffered to GCS.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
from pathlib import Path

from src.agent.config import AgentBehaviorConfig, BigQueryConfig, GCSConfig
from src.agent.tools import ToolDef
from src.bq_client import BQClient

logger = logging.getLogger(__name__)

# Guard: reject mutating SQL statements
_FORBIDDEN_SQL = re.compile(
    r"\b(DROP|DELETE|TRUNCATE|INSERT|UPDATE|MERGE|CREATE|ALTER|GRANT|REVOKE)\b",
    re.IGNORECASE,
)

_PREVIEW_ROWS = 10  # Rows shown in buffered preview


def _make_client(config: BigQueryConfig) -> BQClient:
    return BQClient(
        project=config.project,
        dataset=config.dataset,
        location=config.location,
    )


def _buffer_to_gcs(
    df,
    sql: str,
    bucket: str,
    session_id: str,
) -> str | None:
    """Write DataFrame to GCS as CSV. Returns GCS URI or None on failure."""
    try:
        from src.gcs_utils import upload_blob

        sql_hash = hashlib.sha256(sql.encode()).hexdigest()[:8]
        blob_name = f"agent/artifacts/query_{session_id}_{sql_hash}.csv"

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            df.to_csv(f, index=False)
            tmp_path = f.name

        try:
            uri = upload_blob(tmp_path, bucket, blob_name)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        return uri
    except Exception as e:
        logger.warning(f"Failed to buffer query result to GCS: {e}")
        return None


def _format_buffered_result(df, gcs_uri: str) -> str:
    """Format a buffered result with preview + metadata."""
    columns = ", ".join(df.columns.tolist())
    preview = df.head(_PREVIEW_ROWS).to_markdown(index=False)

    return (
        f"Query returned {len(df)} rows (buffered to GCS).\n"
        f"File: {gcs_uri}\n"
        f"Columns: {columns}\n"
        f"Rows: {len(df)}\n\n"
        f"Preview (first {min(len(df), _PREVIEW_ROWS)} rows):\n{preview}"
    )


def create_bigquery_tools(
    config: BigQueryConfig,
    gcs_config: GCSConfig | None = None,
    session_id: str | None = None,
    agent_behavior: AgentBehaviorConfig | None = None,
) -> list[ToolDef]:
    """Create BigQuery tools using the project's existing BQClient.

    Args:
        config: BigQuery configuration.
        gcs_config: GCS config for buffering large results. If None, buffering disabled.
        session_id: Current session ID for GCS file naming.
        agent_behavior: Agent behavior config (contains context_buffer_threshold).
    """
    client = _make_client(config)
    threshold = (agent_behavior.context_buffer_threshold if agent_behavior else 50)
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

        # Inline result (small or GCS fallback)
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
                "Execute a SELECT query against BigQuery and return results. "
                "Small results (<=50 rows) are returned inline as a markdown table. "
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
```

**Step 2: Run the buffering tests**

Run: `python -m pytest tests/test_agent_tools.py::TestBigQueryContextBuffering -v`
Expected: All 4 tests PASS

**Step 3: Run ALL tests to verify nothing is broken**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS (existing tests may need minor adjustment if they call `create_bigquery_tools` without new params — the new params default to `None` so existing callers are fine)

**Step 4: Commit**

```bash
git add src/agent/tools/bigquery.py
git commit -m "feat: buffer large BigQuery results to GCS instead of LLM context"
```

---

### Task 4: Wire up buffering in the CLI runner

**Files:**
- Modify: `src/agent/run.py:88` (the `create_bigquery_tools` call in `build_agent`)

**Step 1: Pass GCS config and session ID to BigQuery tools**

In `src/agent/run.py`, change line 88 from:

```python
orchestrator.add_tools(create_bigquery_tools(config.bigquery))
```

to:

```python
orchestrator.add_tools(create_bigquery_tools(
    config.bigquery,
    gcs_config=config.gcs,
    session_id=session_key.session_id,
    agent_behavior=config.agent,
))
```

**Step 2: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All PASS

**Step 3: Run linter**

Run: `python -m ruff check src/ tests/`
Expected: No errors

**Step 4: Commit**

```bash
git add src/agent/run.py
git commit -m "feat: wire context buffering into agent runner"
```

---

### Task 5: Update architecture doc

**Files:**
- Modify: `docs/architecture/system_architecture.md` (tool table and security section)

**Step 1: Update the tool table**

In the **Registered tools** table, update the `query_bigquery` row description to mention buffering:

```
| `query_bigquery`, `get_table_schema`, `check_table_exists` | tools/bigquery.py | Data analysis (large results buffered to GCS) |
```

**Step 2: Add a note in the Security guardrails section**

Add:
```
- Context buffering: Query results >50 rows written to GCS as CSV, LLM sees preview + file reference only. Prevents context overload.
```

**Step 3: Commit everything**

```bash
git add docs/
git commit -m "docs: update architecture and tech design for context buffering"
```

---

### Summary

| Task | Files | What |
|------|-------|------|
| 1 | config.py, agent.yaml | Add `context_buffer_threshold` config field |
| 2 | test_agent_tools.py | Write 4 failing tests |
| 3 | bigquery.py | Implement buffering logic |
| 4 | run.py | Wire buffering into agent runner |
| 5 | docs/ | Update architecture docs |

Total: 5 files modified, ~100 lines of new code, 4 new tests.
