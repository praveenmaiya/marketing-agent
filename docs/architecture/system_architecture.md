# Marketing Agent — System Architecture

## Overview

A long-running marketing automation agent for Auxia built on GCP (BigQuery, GCS, Metaflow on GKE). Autonomously analyzes campaigns, browses treatments via the Auxia Console API, generates reports, and monitors performance. Uses Claude's tool-calling API with a ReAct loop, plan-as-artifact management, BM25 memory search, multi-project support, and subagent fan-out.

## Architecture Diagram

```
                        ┌──────────────────────────────────┐
                        │         Trigger Layer            │
                        │     CLI  │  Cron  │  Slack       │
                        └──────────────┬───────────────────┘
                                       │
                        ┌──────────────▼───────────────────┐
                        │     Session Router               │
                        │  cli:{user}:{project}:{session}  │
                        │  cron:{job}:{project}:{session}  │
                        │  slack:{chan}:{project}:{session} │
                        └──────────────┬───────────────────┘
                                       │
                        ┌──────────────▼───────────────────┐
                        │     Orchestrator (on GKE)        │
                        │  ┌─────────────────────────┐     │
                        │  │ Plan Manager             │     │
                        │  │ - make_plan()            │     │
                        │  │ - execute_step()         │     │
                        │  │ - update_plan()          │     │
                        │  │ - checkpoint_state()     │     │
                        │  └────────────┬────────────┘     │
                        │               │                   │
                        │  ┌────────────▼────────────┐     │
                        │  │ Context Engine           │     │
                        │  │ - Hot (in-memory)        │     │
                        │  │ - Warm (GCS JSONL)       │     │
                        │  │ - Cold (BigQuery BM25)   │     │
                        │  │ - Pre-compaction flush   │     │
                        │  └────────────┬────────────┘     │
                        │               │                   │
                        │  ┌────────────▼────────────┐     │
                        │  │ Tool Router              │     │
                        │  │ - Auxia Console (API)    │     │
                        │  │ - BigQuery (SQL)         │     │
                        │  │ - GCS (artifacts)        │     │
                        │  │ - Memory (BM25 search)   │     │
                        │  │ - Subagent spawner       │     │
                        │  └─────────────────────────┘     │
                        └──────────────────────────────────┘
                                       │
                    ┌──────────────────┼──────────────────┐
                    ▼                  ▼                  ▼
            ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
            │  Subagent:   │  │  Subagent:   │  │  Fan-out:    │
            │  SQL Analyst │  │  Creative    │  │  100x Haiku  │
            │  (Sonnet)    │  │  Reviewer    │  │  classifiers │
            └──────────────┘  └──────────────┘  └──────────────┘
```

## Components

### 1. Session Router (`src/agent/session.py`)

Trigger-based session isolation prevents cross-contamination between different invocation contexts. Each session key encodes:
- **Trigger type**: `cli`, `slack`, or `cron`
- **Source**: username, channel ID, or job name
- **Project ID**: Auxia project scope
- **Session ID**: unique per invocation

All GCS paths are scoped to `agent/memory/{project_id}/`, ensuring project-level isolation.

### 2. Orchestrator (`src/agent/orchestrator.py`)

The main ReAct loop:
1. Assemble system prompt with dynamic project context + plan state + rules
2. Call Claude API with registered tools
3. If tool_use: execute tools, inject results, loop
4. If end_turn: return final response
5. Checkpoint to GCS periodically
6. Pre-compaction flush + context compaction when approaching token limits

**Plan Manager**: Plans are stored as tool-call outputs (the "plan-as-artifact" pattern from Hightouch). The model can create, reference, update, and revise plans mid-flight. Each step tracks status (pending/in_progress/completed/failed) and result.

**System Prompt Rules**: 10 explicit rules enforce disciplined agent behavior:
- Plan-first: always create a plan before executing
- Memory-first: search memory before starting new analysis
- Anti-looping: max 2 retries on the same failed action
- Session summary: save findings to memory before finishing
- Subagent delegation: use subagents for analyses requiring >3 tool calls

**API Retry**: Exponential backoff on rate limits. Checkpoint on unrecoverable errors.

### 3. Context Engine (`src/agent/context.py`)

Three-tier hybrid memory with per-project isolation:

| Layer | Storage | What Goes There | Access Pattern |
|-------|---------|----------------|----------------|
| **Hot** | In-process dict/list | Current plan, messages, scratchpad, memory cache | Every turn (~ms) |
| **Warm** | GCS JSONL | Per-session memory logs, session checkpoints | On-demand (~100ms) |
| **Cold** | BigQuery | All memories across sessions, BM25 searchable | SQL query (~1-5s) |

**Memory Categories**: `finding` (data observations), `decision` (choices made), `pattern` (recurring trends), `session_summary` (auto-generated recap), `general` (everything else).

**Memory Persistence**: `save_memory()` writes to all three layers:
1. Hot: append to in-memory list
2. Warm: append to per-session JSONL file in GCS (`memories_{session_id}.jsonl`)
3. Cold: insert row into BigQuery `memories` table

**Memory Search**: `search_memories()` uses BigQuery `SEARCH()` function for BM25 full-text search, with hot-layer substring fallback for memories not yet in BigQuery.

**Pre-Compaction Flush** (from OpenClaw): Before compacting context, the LLM is asked to extract important findings, decisions, and patterns from the conversation and save them to durable memory. This ensures nothing important is lost during truncation.

**Session Compaction**: When context exceeds the token threshold (~120K), old messages are summarized (via Haiku) and the conversation is truncated to summary + last 6 messages. Bridge messages maintain role alternation.

**Pod Restart Recovery**: Full session state serializable to/from GCS JSON.

**GCS Layout**:
```
gs://{bucket}/agent/memory/{project_id}/
  memories_{session_id}.jsonl    # Per-session memory log (avoids race conditions)
  sessions/{session_id}.json     # Session state checkpoint
```

### 4. Tool System (`src/agent/tools/`)

Each tool is a `ToolDef` with:
- `name`: identifier for the API
- `description`: shown to the model
- `input_schema`: JSON Schema for parameters
- `handler`: Python callable -> string result

**Registered tools**:

| Tool | File | Purpose |
|------|------|---------|
| `make_plan`, `update_plan_step`, `revise_plan` | orchestrator.py | Plan lifecycle management |
| `list_treatments`, `get_treatment` | tools/auxia.py | Browse Auxia treatments |
| `list_surfaces`, `list_treatment_types` | tools/auxia.py | Project structure discovery |
| `list_objectives`, `list_data_fields` | tools/auxia.py | Business goals and targeting attributes |
| `query_bigquery`, `get_table_schema`, `check_table_exists` | tools/bigquery.py | Data analysis |
| `read_gcs`, `write_gcs`, `list_gcs` | tools/gcs.py | Artifact storage |
| `search_memory`, `save_memory`, `get_session_history` | tools/memory.py | Cross-session memory |
| `spawn_subagent`, `fan_out_classify` | tools/subagent.py | Parallel child LLM calls |

**Security guardrails**:
- BigQuery: Only SELECT allowed (regex guard blocks DDL/DML). Exception: `_ensure_memories_table` DDL for bootstrapping, controlled by config only.
- GCS: Path validation enforces `agent/` prefixes only, no `..` traversal.
- Subagent: Model allowlist prevents cost spikes.
- Auxia Console: Read-only access (Phase 1). No write operations exposed.
- Table ID validation: regex ensures only safe characters in BigQuery identifiers.
- URL encoding: `urllib.parse.urlencode()` for BFF API query parameters.

### 5. Auxia Console Integration (`src/agent/tools/auxia.py`)

Read-only tools wrapping the Auxia Console BFF API. The agent can browse treatments, surfaces, treatment types, objectives, and data fields for any project it has access to.

**Dynamic project context**: On startup, `build_project_context()` fetches project metadata (surfaces, treatment types, objectives) and injects it into the system prompt so the agent understands the project's structure.

**Authentication**: Uses the Auxia BFF session cookie configured via `AUXIA_SESSION_COOKIE` env var or MCP.

### 6. Subagent System (`src/agent/tools/subagent.py`)

Two patterns:
1. **Scoped subagent**: Spawn a child Sonnet call with focused objective + limited context. Prevents context pollution.
2. **Fan-out classification**: Spawn N parallel Haiku calls for bulk categorization. ThreadPoolExecutor with configurable max_workers.

### 7. Metaflow Integration (`flows/agent_flow.py`)

Wraps the agent in a Metaflow FlowSpec for:
- Scheduled execution via `@schedule`
- Retry on failure via `@retry`
- Artifact storage via `self.*` attributes
- Kubernetes execution via `@kubernetes`

## Configuration

All config in `configs/agent.yaml` with env var substitution (`${VAR:-default}`):
- Model selection (orchestrator, subagent, classifier)
- Token budgets (max_context, compaction_threshold)
- BigQuery project/dataset
- GCS bucket/prefixes
- Agent behavior (max_turns, checkpoint_interval, fan_out_max_parallel)
- Auxia Console (enabled, bff_base_url)
- Project ID (set via `--project` CLI flag or `AUXIA_PROJECT_ID` env var)

## Design Rationale

### Why not SQLite? (OpenClaw's approach)
GKE pods are ephemeral. SQLite state would be lost on restart. GCS gives durable persistence without local state dependency.

### Why BM25 via BigQuery SEARCH? (not embeddings)
BigQuery `SEARCH()` provides full-text search with no extra infrastructure (no vector DB, no embedding model). Good enough for keyword-based memory recall. Semantic/vector search can be added later as a complementary layer.

### Why per-session JSONL files?
A shared JSONL file has a read-modify-write race condition when multiple sessions run concurrently for the same project. Per-session files (`memories_{session_id}.jsonl`) are append-safe without coordination. BigQuery is the primary search layer; GCS JSONL serves as a warm backup.

### Why plan-as-artifact? (Hightouch's approach)
Storing the plan as a tool output means the model can reference it in every turn and revise it based on results. This prevents drift in long-running tasks.

### Why subagent isolation? (Both Hightouch + OpenClaw)
Context pollution is the #1 killer of long-running agents. Child LLMs get only the context they need, produce a summary, and the summary is injected back — not the full intermediate work.

### Why pre-compaction flush? (OpenClaw)
Simply truncating context loses information. By asking the LLM to identify and save important findings before truncation, we ensure durable facts survive context windows.

### Why session-based isolation?
Different trigger types (CLI, Slack, cron) have different usage patterns and should not share state. A CLI debug session should not pollute a scheduled cron job's context.

## Phase Roadmap

| Phase | What | Status |
|-------|------|--------|
| 1 | Core agent loop, tools, plan management, CLI | Done |
| 2 | BM25 memory search, multi-project, Auxia Console tools, session routing, system prompt rules | Done |
| 3 | Slack trigger, cron scheduling, cost tracking per run | Planned |
| 4 | Evaluation framework, human-in-the-loop review, treatment write actions | Planned |

## References
- [Hightouch Agent Harness](https://www.amplifypartners.com/blog-posts/how-hightouch-built-their-long-running-agent-harness) — plan-as-artifact, subagent isolation, fan-out classification
- [OpenClaw](https://github.com/openclaw/openclaw) — 4-phase agent loop, hybrid memory, session compaction, pre-compaction flush, cron wakeups
- [OpenClaw Architecture Lessons](https://blog.agentailor.com/posts/openclaw-architecture-lessons-for-agent-builders) — lane queues, skills as markdown, hybrid search
- [Karpathy on Claws](https://simonwillison.net/2026/Feb/21/claws/) — orchestration, scheduling, context, tool calls and persistence
