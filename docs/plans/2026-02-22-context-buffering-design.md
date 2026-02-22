# Context Buffering for BigQuery Results — Design

## Problem

The `query_bigquery` tool dumps raw result rows directly into the LLM context as a markdown table. For large results (100+ rows of wide tables), this wastes thousands of tokens, triggers premature compaction, and degrades reasoning quality. The orchestrator should decide what to do with data, not read every row.

## Decision

**Approach A: Buffer in the BigQuery tool handler.** Modify `query_bigquery()` to check row count. If >50 rows, write to GCS and return preview + metadata. Minimal blast radius — only touches the BigQuery tool.

Rejected alternatives:
- Middleware layer in tool registry (over-engineered, only BigQuery produces large results)
- Separate `query_bigquery_to_gcs` tool (LLM can't predict result size, duplicates logic)

## Design

### Behavior

```
query_bigquery(sql, dry_run, max_rows)
    │
    ├── SQL guard check (unchanged)
    ├── dry_run path (unchanged)
    │
    ├── Run query → get DataFrame
    │
    ├── If len(df) <= 50:
    │     Return inline markdown (current behavior, unchanged)
    │
    └── If len(df) > 50:
          1. Write df.to_csv() to temp file
          2. Upload to GCS: agent/artifacts/query_{session_id}_{hash}.csv
          3. Return preview + metadata to LLM
```

### LLM sees (for buffered results)

```
Query returned 10,000 rows (buffered to GCS).
File: gs://auxia-agent/agent/artifacts/query_a1b2c3_e4f5g6h7.csv
Columns: user_id, event, timestamp, value
Rows: 10,000 | Date range: 2026-01-01 to 2026-01-31

Preview (first 10 rows):
| user_id | event   | timestamp  | value |
|---------|---------|------------|-------|
| u001    | click   | 2026-01-01 | 1.2   |
| u002    | purchase| 2026-01-02 | 45.0  |
| ...     | ...     | ...        | ...   |
```

### File naming

```
gs://{bucket}/agent/artifacts/query_{session_id}_{short_hash}.csv
```

- `short_hash` = first 8 chars of SHA256 of SQL query
- Files kept with TTL (GCS lifecycle policy, 7-30 days)

### Files changed

| File | Change |
|------|--------|
| `src/agent/tools/bigquery.py` | Add GCS buffering logic to `query_bigquery` handler. Add `gcs_config` and `session_id` params to `create_bigquery_tools()`. |
| `src/agent/config.py` | Add `context_buffer_threshold: int = 50` to `AgentBehaviorConfig` |
| `configs/agent.yaml` | Add `context_buffer_threshold: 50` under `agent:` |
| `src/agent/run.py` | Pass `config.gcs` and `session_id` to `create_bigquery_tools()` |
| `tests/test_agent_tools.py` | Add tests for buffering behavior |

### What doesn't change

- Orchestrator, context engine, tool registry, GCS tools, memory tools, subagent tools, Auxia tools, session router

### Tests

1. Result <= 50 rows → inline markdown (existing behavior preserved)
2. Result > 50 rows → writes CSV to GCS, returns preview + metadata
3. GCS upload failure → falls back to inline markdown (graceful degradation)
4. Preview contains first 10 rows + column names + row count
