"""Tests for the agent tool system."""

from unittest.mock import MagicMock, patch

import pandas as pd

from src.agent.config import AgentBehaviorConfig, BigQueryConfig, GCSConfig
from src.agent.tools import ToolDef, ToolRegistry
from src.agent.tools.bigquery import (
    _MAX_MATERIALIZE_ROWS,
    _PREVIEW_MAX_CHARS,
    _is_safe_sql,
    _truncate_preview,
    create_bigquery_tools,
)


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
# SQL allowlist tests
# ---------------------------------------------------------------------------


class TestSQLAllowlist:
    """Tests for the _is_safe_sql allowlist guard."""

    def test_select_allowed(self):
        assert _is_safe_sql("SELECT * FROM table") is True

    def test_select_with_leading_whitespace(self):
        assert _is_safe_sql("  \n  SELECT 1") is True

    def test_with_cte_allowed(self):
        assert _is_safe_sql("WITH cte AS (SELECT 1) SELECT * FROM cte") is True

    def test_select_with_comment(self):
        assert _is_safe_sql("-- get users\nSELECT * FROM users") is True

    def test_insert_blocked(self):
        assert _is_safe_sql("INSERT INTO table VALUES (1)") is False

    def test_drop_blocked(self):
        assert _is_safe_sql("DROP TABLE users") is False

    def test_delete_blocked(self):
        assert _is_safe_sql("DELETE FROM users WHERE id=1") is False

    def test_create_blocked(self):
        assert _is_safe_sql("CREATE TABLE foo (id INT)") is False

    def test_update_blocked(self):
        assert _is_safe_sql("UPDATE users SET name='x'") is False

    def test_multi_statement_blocked(self):
        assert _is_safe_sql("SELECT 1; DROP TABLE users") is False

    def test_trailing_semicolon_allowed(self):
        # Trailing semicolon with only whitespace after is fine
        assert _is_safe_sql("SELECT 1;") is True
        assert _is_safe_sql("SELECT 1; ") is True

    def test_semicolon_in_string_literal_allowed(self):
        # Semicolons inside string literals should not trigger rejection
        assert _is_safe_sql("SELECT ';' as s") is True
        assert _is_safe_sql("SELECT '; DROP' as s") is True
        assert _is_safe_sql("SELECT 'a; b'") is True

    def test_semicolon_in_comment_allowed(self):
        assert _is_safe_sql("SELECT 1 /* hi;there */") is True

    def test_semicolon_in_line_comment_allowed(self):
        # Semicolons inside -- line comments should not trigger rejection
        assert _is_safe_sql("SELECT 1 -- hi;there") is True
        assert _is_safe_sql("SELECT 1 -- hi;there\n") is True
        assert _is_safe_sql("SELECT 1; -- finished statement") is True

    def test_with_cte_then_delete_blocked(self):
        # WITH ... DELETE bypasses naive allowlist — must be blocked
        assert _is_safe_sql("WITH c AS (SELECT 1) DELETE FROM t WHERE id=1") is False

    def test_with_cte_then_update_blocked(self):
        assert _is_safe_sql("WITH c AS (SELECT 1) UPDATE t SET x=1") is False

    def test_with_cte_then_insert_blocked(self):
        assert _is_safe_sql("WITH c AS (SELECT 1) INSERT INTO t(x) SELECT 1") is False

    def test_with_cte_then_merge_blocked(self):
        assert _is_safe_sql(
            "WITH c AS (SELECT 1) MERGE t USING c ON t.id=c.id "
            "WHEN MATCHED THEN UPDATE SET x=1"
        ) is False

    def test_with_multiple_ctes_then_select_allowed(self):
        sql = "WITH a AS (SELECT 1), b AS (SELECT 2) SELECT * FROM a JOIN b"
        assert _is_safe_sql(sql) is True

    def test_with_nested_parens_select_allowed(self):
        sql = "WITH c AS (SELECT * FROM (SELECT 1)) SELECT * FROM c"
        assert _is_safe_sql(sql) is True

    # Leading-comment offset regression tests (Codex round 4) ----------------

    def test_with_leading_block_comment_select_allowed(self):
        """WITH query with leading block comment should be allowed."""
        assert _is_safe_sql("/*comment*/WITH cte AS (SELECT 1) SELECT * FROM cte") is True
        assert _is_safe_sql("/* long padding */WITH c AS (SELECT 1) SELECT * FROM c") is True

    def test_with_leading_block_comment_dml_blocked(self):
        """WITH+DML must be blocked regardless of leading comment length."""
        for n in (1, 12, 20, 50):
            sql = f"/*{'a' * n}*/WITH c AS (SELECT 1) INSERT INTO t(x) SELECT 1"
            assert _is_safe_sql(sql) is False, f"Failed for comment length {n}"

    def test_with_leading_line_comment_select_allowed(self):
        assert _is_safe_sql("-- comment\nWITH c AS (SELECT 1) SELECT * FROM c") is True

    # CTE column-list tests (Codex round 4) ----------------------------------

    def test_with_cte_column_list_allowed(self):
        """WITH cte(col) AS (...) SELECT is valid SQL."""
        assert _is_safe_sql("WITH cte(col) AS (SELECT 1) SELECT * FROM cte") is True

    def test_with_multi_cte_column_lists_allowed(self):
        sql = "WITH a(x) AS (SELECT 1), b(y) AS (SELECT 2) SELECT * FROM a, b"
        assert _is_safe_sql(sql) is True

    def test_with_cte_column_list_dml_blocked(self):
        """WITH cte(col) AS (...) DELETE must still be blocked."""
        assert _is_safe_sql("WITH c(x) AS (SELECT 1) DELETE FROM t") is False

    # Quoted-identifier bypass tests (Codex round 5) -------------------------

    def test_backtick_identifier_with_paren_dml_blocked(self):
        """Backtick identifiers containing ) must not break paren walker."""
        assert _is_safe_sql("WITH c AS (SELECT 1 AS `) SELECT`) DELETE FROM t") is False
        assert _is_safe_sql("WITH c AS (SELECT 1 AS `) SELECT`) INSERT INTO t(x) SELECT 1") is False

    def test_double_quoted_with_paren_dml_blocked(self):
        """Double-quoted strings containing ) must not break paren walker."""
        assert _is_safe_sql('WITH c AS (SELECT 1 AS ") SELECT") DELETE FROM t') is False

    def test_backtick_identifier_select_allowed(self):
        """Backtick identifiers in safe queries should still pass."""
        assert _is_safe_sql("WITH `c` AS (SELECT 1) SELECT * FROM `c`") is True
        assert _is_safe_sql("SELECT * FROM `my-project.dataset.table`") is True

    # Escaped-backtick bypass tests (Codex round 6) ----------------------------

    def test_escaped_backtick_dml_blocked(self):
        r"""Escaped backtick \` inside identifier must not break paren walker."""
        assert _is_safe_sql(r"WITH c AS (SELECT 1 AS `x\`) SELECT`) DELETE FROM t") is False
        assert _is_safe_sql(r"WITH c AS (SELECT 1 AS `x\`) SELECT`) INSERT INTO t(x) SELECT 1") is False

    def test_escaped_backtick_select_allowed(self):
        r"""Escaped backtick in safe query should still pass."""
        assert _is_safe_sql(r"WITH c AS (SELECT 1 AS `abc\`def`) SELECT * FROM c") is True
        assert _is_safe_sql(r"SELECT * FROM `my\`table`") is True


# ---------------------------------------------------------------------------
# BigQuery context-buffering tests
# ---------------------------------------------------------------------------


def _make_df(n_rows: int, n_cols: int = 2) -> pd.DataFrame:
    """Helper: create a simple DataFrame with *n_rows* rows."""
    data = {"id": range(n_rows), "value": [f"v{i}" for i in range(n_rows)]}
    for c in range(2, n_cols):
        data[f"col_{c}"] = [f"data_{i}_{c}" for i in range(n_rows)]
    return pd.DataFrame(data)


def _get_query_tool(tools: list[ToolDef]) -> ToolDef:
    """Return the query_bigquery ToolDef from a list of tools."""
    return next(t for t in tools if t.name == "query_bigquery")


class TestBigQueryContextBuffering:
    """Tests for the context buffering behaviour in the BigQuery tool."""

    bq_config = BigQueryConfig(project="p", dataset="d", location="US")
    gcs_config = GCSConfig(bucket="my-bucket")
    session_id = "sess-001"
    agent_behavior = AgentBehaviorConfig(context_buffer_threshold=50)

    def _make_tools(self, mock_make_client, mock_client, **kwargs):
        """Helper to create tools with standard mocking."""
        mock_make_client.return_value = mock_client
        return create_bigquery_tools(
            kwargs.get("bq_config", self.bq_config),
            gcs_config=kwargs.get("gcs_config", self.gcs_config),
            session_id=kwargs.get("session_id", self.session_id),
            agent_behavior=kwargs.get("agent_behavior", self.agent_behavior),
        )

    # 1. Small result: inline (existing behaviour) --------------------------

    @patch("src.agent.tools.bigquery._make_client")
    def test_small_result_returns_inline(self, mock_make_client):
        """Results with <= 50 rows stay inline as markdown."""
        mock_client = MagicMock()
        mock_client.run_query.return_value = _make_df(10)

        tools = self._make_tools(mock_make_client, mock_client)
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
        mock_upload.return_value = "gs://my-bucket/agent/artifacts/query_sess-001_abc.csv"

        tools = self._make_tools(mock_make_client, mock_client)
        result = _get_query_tool(tools).handler({"sql": "SELECT 1"})

        assert "100 rows" in result
        assert "buffered to GCS" in result
        assert "gs://" in result
        mock_upload.assert_called_once()

    # 3. GCS failure: fall back to inline with warning ----------------------

    @patch("src.agent.tools.bigquery.upload_blob", side_effect=RuntimeError("boom"))
    @patch("src.agent.tools.bigquery._make_client")
    def test_gcs_failure_falls_back_to_inline_with_warning(self, mock_make_client, mock_upload):
        """If GCS upload fails, result is returned inline with a WARNING prefix."""
        mock_client = MagicMock()
        mock_client.run_query.return_value = _make_df(100)

        tools = self._make_tools(mock_make_client, mock_client)
        result = _get_query_tool(tools).handler({"sql": "SELECT 1"})

        assert "WARNING" in result
        assert "GCS buffering failed" in result
        assert "100 rows" in result
        assert "|" in result  # markdown table pipe chars

    # 3b. Hard GCS errors propagate (not silently swallowed) ----------------

    @patch("src.agent.tools.bigquery.upload_blob", side_effect=PermissionError("403"))
    @patch("src.agent.tools.bigquery._make_client")
    def test_permission_error_propagates(self, mock_make_client, mock_upload):
        """PermissionError from GCS upload must propagate — not degrade to inline."""
        import pytest

        mock_client = MagicMock()
        mock_client.run_query.return_value = _make_df(100)

        tools = self._make_tools(mock_make_client, mock_client)
        with pytest.raises(PermissionError, match="403"):
            _get_query_tool(tools).handler({"sql": "SELECT 1"})

    @patch("src.agent.tools.bigquery.upload_blob", side_effect=ValueError("bad bucket"))
    @patch("src.agent.tools.bigquery._make_client")
    def test_value_error_propagates(self, mock_make_client, mock_upload):
        """ValueError from GCS upload must propagate — not degrade to inline."""
        import pytest

        mock_client = MagicMock()
        mock_client.run_query.return_value = _make_df(100)

        tools = self._make_tools(mock_make_client, mock_client)
        with pytest.raises(ValueError, match="bad bucket"):
            _get_query_tool(tools).handler({"sql": "SELECT 1"})

    # 4. Preview has at most 10 rows ----------------------------------------

    @patch("src.agent.tools.bigquery.upload_blob")
    @patch("src.agent.tools.bigquery._make_client")
    def test_preview_has_10_rows_max(self, mock_make_client, mock_upload):
        """The buffered preview shows at most 10 rows of data."""
        n = 200
        mock_client = MagicMock()
        mock_client.run_query.return_value = _make_df(n)
        mock_upload.return_value = "gs://my-bucket/agent/artifacts/query_sess-001_xyz.csv"

        tools = self._make_tools(mock_make_client, mock_client)
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

    # 5. Boundary: exactly 50 rows = inline, 51 = buffered -----------------

    @patch("src.agent.tools.bigquery._make_client")
    def test_boundary_50_rows_stays_inline(self, mock_make_client):
        """Exactly threshold rows should stay inline (not >)."""
        mock_client = MagicMock()
        mock_client.run_query.return_value = _make_df(50)

        tools = self._make_tools(mock_make_client, mock_client)
        result = _get_query_tool(tools).handler({"sql": "SELECT 1"})

        assert "50 rows" in result
        assert "buffered" not in result.lower()
        assert "gs://" not in result

    @patch("src.agent.tools.bigquery.upload_blob")
    @patch("src.agent.tools.bigquery._make_client")
    def test_boundary_51_rows_gets_buffered(self, mock_make_client, mock_upload):
        """One row above threshold triggers buffering."""
        mock_client = MagicMock()
        mock_client.run_query.return_value = _make_df(51)
        mock_upload.return_value = "gs://my-bucket/agent/artifacts/query_sess-001_51.csv"

        tools = self._make_tools(mock_make_client, mock_client)
        result = _get_query_tool(tools).handler({"sql": "SELECT 1"})

        assert "51 rows" in result
        assert "buffered to GCS" in result
        mock_upload.assert_called_once()

    # 6. Buffering disabled without gcs_config ------------------------------

    @patch("src.agent.tools.bigquery._make_client")
    def test_buffering_disabled_without_gcs_config(self, mock_make_client):
        """Large results stay inline when gcs_config is not provided."""
        mock_client = MagicMock()
        mock_client.run_query.return_value = _make_df(100)
        mock_make_client.return_value = mock_client

        tools = create_bigquery_tools(
            self.bq_config,
            gcs_config=None,
            session_id=None,
            agent_behavior=self.agent_behavior,
        )
        result = _get_query_tool(tools).handler({"sql": "SELECT 1"})

        assert "100 rows" in result
        assert "buffered" not in result.lower()
        assert "gs://" not in result

    # 7. max_rows clamping --------------------------------------------------

    @patch("src.agent.tools.bigquery._make_client")
    def test_max_rows_clamped_to_upper_bound(self, mock_make_client):
        """max_rows > 1000 is clamped to 1000."""
        mock_client = MagicMock()
        mock_client.run_query.return_value = _make_df(30)
        mock_make_client.return_value = mock_client

        tools = create_bigquery_tools(
            self.bq_config,
            gcs_config=None,
            session_id=None,
            agent_behavior=self.agent_behavior,
        )
        # Should not crash with extreme values
        result = _get_query_tool(tools).handler({"sql": "SELECT 1", "max_rows": 99999})
        assert "30 rows" in result

    @patch("src.agent.tools.bigquery._make_client")
    def test_max_rows_clamped_to_lower_bound(self, mock_make_client):
        """max_rows < 1 is clamped to 1."""
        mock_client = MagicMock()
        mock_client.run_query.return_value = _make_df(5)
        mock_make_client.return_value = mock_client

        tools = create_bigquery_tools(
            self.bq_config,
            gcs_config=None,
            session_id=None,
            agent_behavior=self.agent_behavior,
        )
        result = _get_query_tool(tools).handler({"sql": "SELECT 1", "max_rows": -10})
        assert "5 rows" in result

    # 8. SQL allowlist integration test -------------------------------------

    @patch("src.agent.tools.bigquery._make_client")
    def test_sql_allowlist_blocks_insert(self, mock_make_client):
        """INSERT query is blocked by the allowlist guard."""
        mock_client = MagicMock()
        mock_make_client.return_value = mock_client

        tools = create_bigquery_tools(self.bq_config)
        result = _get_query_tool(tools).handler({"sql": "INSERT INTO t VALUES (1)"})

        assert "ERROR" in result
        mock_client.run_query.assert_not_called()

    @patch("src.agent.tools.bigquery._make_client")
    def test_sql_allowlist_blocks_multi_statement(self, mock_make_client):
        """Multi-statement query is blocked."""
        mock_client = MagicMock()
        mock_make_client.return_value = mock_client

        tools = create_bigquery_tools(self.bq_config)
        result = _get_query_tool(tools).handler({"sql": "SELECT 1; DROP TABLE users"})

        assert "ERROR" in result
        mock_client.run_query.assert_not_called()

    # 9. max_results passed to BQ client ------------------------------------

    @patch("src.agent.tools.bigquery._make_client")
    def test_max_results_passed_to_client(self, mock_make_client):
        """run_query is called with max_results to cap memory usage."""
        mock_client = MagicMock()
        mock_client.run_query.return_value = _make_df(5)
        mock_make_client.return_value = mock_client

        tools = create_bigquery_tools(self.bq_config)
        _get_query_tool(tools).handler({"sql": "SELECT 1"})

        mock_client.run_query.assert_called_once_with(
            "SELECT 1", max_results=_MAX_MATERIALIZE_ROWS + 1,
        )

    # 10. Dynamic threshold in tool description -----------------------------

    @patch("src.agent.tools.bigquery._make_client")
    def test_description_uses_dynamic_threshold(self, mock_make_client):
        """Tool description reflects the configured threshold, not hardcoded 50."""
        mock_make_client.return_value = MagicMock()

        custom_behavior = AgentBehaviorConfig(context_buffer_threshold=100)
        tools = create_bigquery_tools(
            self.bq_config,
            agent_behavior=custom_behavior,
        )
        query_tool = _get_query_tool(tools)

        assert "<=100" in query_tool.description
        assert ">100" in query_tool.description

    # 11. Artifact prefix from config ---------------------------------------

    @patch("src.agent.tools.bigquery.upload_blob")
    @patch("src.agent.tools.bigquery._make_client")
    def test_uses_config_artifact_prefix(self, mock_make_client, mock_upload):
        """Blob path uses gcs_config.artifact_prefix, not hardcoded."""
        mock_client = MagicMock()
        mock_client.run_query.return_value = _make_df(100)
        mock_upload.return_value = "gs://my-bucket/custom/prefix/query.csv"

        custom_gcs = GCSConfig(bucket="my-bucket", artifact_prefix="custom/prefix/")

        tools = self._make_tools(
            mock_make_client, mock_client, gcs_config=custom_gcs,
        )
        _get_query_tool(tools).handler({"sql": "SELECT 1"})

        # Verify upload_blob was called with a path starting with custom prefix
        call_args = mock_upload.call_args
        blob_name = call_args[0][2]  # 3rd positional arg is blob_name
        assert blob_name.startswith("custom/prefix/")

    # 12. Result cap note ---------------------------------------------------

    @patch("src.agent.tools.bigquery._make_client")
    def test_result_cap_note_shown_when_capped(self, mock_make_client):
        """When results exceed the cap, a note is shown and rows truncated."""
        mock_client = MagicMock()
        # Return one more than cap to trigger capping (sentinel approach)
        mock_client.run_query.return_value = _make_df(_MAX_MATERIALIZE_ROWS + 1)
        mock_make_client.return_value = mock_client

        tools = create_bigquery_tools(
            self.bq_config, gcs_config=None, session_id=None,
        )
        result = _get_query_tool(tools).handler({"sql": "SELECT 1"})

        assert "capped at" in result.lower()
        assert "LIMIT" in result
        # Row count in output should be the cap, not cap+1
        assert f"{_MAX_MATERIALIZE_ROWS} rows" in result

    @patch("src.agent.tools.bigquery._make_client")
    def test_exact_cap_no_false_positive(self, mock_make_client):
        """Exactly max rows should NOT show cap note (sentinel detects no overflow)."""
        mock_client = MagicMock()
        mock_client.run_query.return_value = _make_df(_MAX_MATERIALIZE_ROWS)
        mock_make_client.return_value = mock_client

        tools = create_bigquery_tools(
            self.bq_config, gcs_config=None, session_id=None,
        )
        result = _get_query_tool(tools).handler({"sql": "SELECT 1"})

        assert "capped" not in result.lower()

    # 13. Inline output gets char-budget truncation ---------------------------

    @patch("src.agent.tools.bigquery._make_client")
    def test_inline_output_truncated_for_wide_data(self, mock_make_client):
        """Inline markdown output is truncated when it exceeds char budget."""
        mock_client = MagicMock()
        # Build a DF with long string values that will exceed _PREVIEW_MAX_CHARS
        wide_data = {
            f"col_{c}": [f"{'x' * 200}_{i}" for i in range(10)]
            for c in range(10)
        }
        mock_client.run_query.return_value = pd.DataFrame(wide_data)
        mock_make_client.return_value = mock_client

        tools = create_bigquery_tools(
            self.bq_config, gcs_config=None, session_id=None,
            agent_behavior=self.agent_behavior,
        )
        result = _get_query_tool(tools).handler({"sql": "SELECT 1"})

        assert "10 rows" in result
        assert "... (preview truncated)" in result

    # 14. Direct unit test for _truncate_preview ---------------------------------

    def test_truncate_preview_short_unchanged(self):
        """Short markdown passes through unchanged."""
        short = "| a | b |\n|---|---|\n| 1 | 2 |"
        assert _truncate_preview(short) == short

    def test_truncate_preview_long_gets_marker(self):
        """Markdown exceeding budget is cut and gets the truncation marker."""
        long_md = "x" * (_PREVIEW_MAX_CHARS + 500)
        result = _truncate_preview(long_md)
        assert result.endswith("... (preview truncated)")
        assert len(result) < len(long_md)
