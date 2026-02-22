"""Tests for the agent tool system."""

from unittest.mock import MagicMock, patch

import pandas as pd

from src.agent.config import AgentBehaviorConfig, BigQueryConfig, GCSConfig
from src.agent.tools import ToolDef, ToolRegistry
from src.agent.tools.bigquery import create_bigquery_tools


class TestToolDef:
    def test_to_api_schema(self):
        tool = ToolDef(
            name="test_tool",
            description="A test tool",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=lambda p: "result",
        )

        schema = tool.to_api_schema()
        assert schema["name"] == "test_tool"
        assert schema["description"] == "A test tool"
        assert schema["input_schema"]["type"] == "object"
        assert "query" in schema["input_schema"]["properties"]

    def test_execute_success(self):
        tool = ToolDef(
            name="echo",
            description="Echo input",
            input_schema={"type": "object", "properties": {}},
            handler=lambda p: f"echo: {p.get('msg', '')}",
        )
        result = tool.execute({"msg": "hello"})
        assert result == "echo: hello"

    def test_execute_error_returns_error_string(self):
        def failing_handler(params):
            raise ValueError("something broke")

        tool = ToolDef(
            name="failing",
            description="Always fails",
            input_schema={"type": "object", "properties": {}},
            handler=failing_handler,
        )
        result = tool.execute({})
        assert "failed" in result
        assert "ValueError" in result
        assert "something broke" in result


class TestToolRegistry:
    def test_register_and_get(self):
        registry = ToolRegistry()
        tool = ToolDef(
            name="my_tool",
            description="test",
            input_schema={"type": "object", "properties": {}},
            handler=lambda p: "ok",
        )
        registry.register(tool)
        assert registry.get("my_tool") is tool
        assert registry.get("nonexistent") is None

    def test_list_tools(self):
        registry = ToolRegistry()
        for i in range(3):
            registry.register(ToolDef(
                name=f"tool_{i}",
                description=f"Tool {i}",
                input_schema={"type": "object", "properties": {}},
                handler=lambda p: "ok",
            ))
        tools = registry.list_tools()
        assert len(tools) == 3

    def test_to_api_schemas(self):
        registry = ToolRegistry()
        registry.register(ToolDef(
            name="query",
            description="Run SQL",
            input_schema={
                "type": "object",
                "properties": {"sql": {"type": "string"}},
            },
            handler=lambda p: "result",
        ))
        schemas = registry.to_api_schemas()
        assert len(schemas) == 1
        assert schemas[0]["name"] == "query"

    def test_register_overwrites(self):
        registry = ToolRegistry()
        tool1 = ToolDef(
            name="x",
            description="version 1",
            input_schema={"type": "object", "properties": {}},
            handler=lambda p: "v1",
        )
        tool2 = ToolDef(
            name="x",
            description="version 2",
            input_schema={"type": "object", "properties": {}},
            handler=lambda p: "v2",
        )
        registry.register(tool1)
        registry.register(tool2)
        assert registry.get("x").description == "version 2"
        assert len(registry.list_tools()) == 1


# ---------------------------------------------------------------------------
# BigQuery context-buffering tests
# ---------------------------------------------------------------------------


def _make_df(n_rows: int) -> pd.DataFrame:
    """Helper: create a simple DataFrame with *n_rows* rows."""
    return pd.DataFrame({"id": range(n_rows), "value": [f"v{i}" for i in range(n_rows)]})


def _get_query_tool(tools: list[ToolDef]) -> ToolDef:
    """Return the query_bigquery ToolDef from a list of tools."""
    return next(t for t in tools if t.name == "query_bigquery")


class TestBigQueryContextBuffering:
    """Tests for the context buffering behaviour in the BigQuery tool."""

    bq_config = BigQueryConfig(project="p", dataset="d", location="US")
    gcs_config = GCSConfig(bucket="my-bucket")
    session_id = "sess-001"
    agent_behavior = AgentBehaviorConfig(context_buffer_threshold=50)

    # 1. Small result: inline (existing behaviour) --------------------------

    @patch("src.agent.tools.bigquery._make_client")
    def test_small_result_returns_inline(self, mock_make_client):
        """Results with <= 50 rows stay inline as markdown."""
        mock_client = MagicMock()
        mock_client.run_query.return_value = _make_df(10)
        mock_make_client.return_value = mock_client

        tools = create_bigquery_tools(
            self.bq_config,
            gcs_config=self.gcs_config,
            session_id=self.session_id,
            agent_behavior=self.agent_behavior,
        )
        result = _get_query_tool(tools).handler({"sql": "SELECT 1"})

        assert "10 rows" in result
        assert "buffered" not in result.lower()
        assert "gs://" not in result

    # 2. Large result: buffer to GCS ----------------------------------------

    @patch("src.agent.tools.bigquery.upload_blob")
    @patch("src.agent.tools.bigquery._make_client")
    def test_large_result_buffers_to_gcs(self, mock_make_client, mock_upload):
        """Results with > 50 rows are written to GCS; LLM sees preview + URI."""
        mock_client = MagicMock()
        mock_client.run_query.return_value = _make_df(100)
        mock_make_client.return_value = mock_client

        mock_upload.return_value = "gs://my-bucket/agent/artifacts/query_sess-001_abc.csv"

        tools = create_bigquery_tools(
            self.bq_config,
            gcs_config=self.gcs_config,
            session_id=self.session_id,
            agent_behavior=self.agent_behavior,
        )
        result = _get_query_tool(tools).handler({"sql": "SELECT 1"})

        assert "100 rows" in result
        assert "buffered to GCS" in result
        assert "gs://" in result
        mock_upload.assert_called_once()

    # 3. GCS failure: fall back to inline -----------------------------------

    @patch("src.agent.tools.bigquery.upload_blob", side_effect=RuntimeError("boom"))
    @patch("src.agent.tools.bigquery._make_client")
    def test_gcs_failure_falls_back_to_inline(self, mock_make_client, mock_upload):
        """If GCS upload fails, the result is returned inline (no crash)."""
        mock_client = MagicMock()
        mock_client.run_query.return_value = _make_df(100)
        mock_make_client.return_value = mock_client

        tools = create_bigquery_tools(
            self.bq_config,
            gcs_config=self.gcs_config,
            session_id=self.session_id,
            agent_behavior=self.agent_behavior,
        )
        result = _get_query_tool(tools).handler({"sql": "SELECT 1"})

        # Should NOT mention GCS buffering — it fell back to inline
        assert "buffered" not in result.lower()
        assert "100 rows" in result
        # Should still render as markdown table
        assert "|" in result  # markdown table pipe chars

    # 4. Preview has at most 10 rows ----------------------------------------

    @patch("src.agent.tools.bigquery.upload_blob")
    @patch("src.agent.tools.bigquery._make_client")
    def test_preview_has_10_rows_max(self, mock_make_client, mock_upload):
        """The buffered preview shows at most 10 rows of data."""
        n = 200
        mock_client = MagicMock()
        mock_client.run_query.return_value = _make_df(n)
        mock_make_client.return_value = mock_client

        mock_upload.return_value = "gs://my-bucket/agent/artifacts/query_sess-001_xyz.csv"

        tools = create_bigquery_tools(
            self.bq_config,
            gcs_config=self.gcs_config,
            session_id=self.session_id,
            agent_behavior=self.agent_behavior,
        )
        result = _get_query_tool(tools).handler({"sql": "SELECT 1"})

        assert "200 rows" in result
        assert "first 10 rows" in result
        # Count data rows in the markdown table (exclude header + separator)
        table_lines = [
            ln for ln in result.split("\n")
            if ln.strip().startswith("|") and "---" not in ln
        ]
        # header (1) + 10 data rows = 11 lines starting with "|"
        assert len(table_lines) == 11
