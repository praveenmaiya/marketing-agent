# Marketing Agent — System Architecture

## Overview

A long-running marketing agent built on GCP (BigQuery, GCS, Metaflow on GKE) that can autonomously analyze campaigns, generate reports, and monitor performance. Uses Claude's tool-calling API with a ReAct loop, plan-as-artifact management, hybrid memory, and subagent fan-out.

## Architecture Diagram

```
                        ┌──────────────────────────────────┐
                        │         Trigger Layer            │
                        │  Slack │ Cron │ Webhook │ CLI    │
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
                        │  │ - Warm (GCS JSON)        │     │
                        │  │ - Cold (BigQuery)        │     │
                        │  └────────────┬────────────┘     │
                        │               │                   │
                        │  ┌────────────▼────────────┐     │
                        │  │ Tool Router              │     │
                        │  │ - BigQuery (SQL)         │     │
                        │  │ - GCS (artifacts)        │     │
                        │  │ - Memory (search/store)  │     │
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

### 1. Orchestrator (`src/agent/orchestrator.py`)

The main ReAct loop:
1. Assemble system prompt with domain context + current plan state
2. Call Claude API with registered tools
3. If tool_use: execute tools, inject results, loop
4. If end_turn: return final response
5. Checkpoint to GCS periodically
6. Compact context when approaching token limits

**Plan Manager**: Plans are stored as tool-call outputs (the "plan-as-artifact" pattern from Hightouch). The model can create, reference, update, and revise plans mid-flight. Each step tracks status (pending/in_progress/completed/failed) and result.

**API Retry**: Exponential backoff on rate limits. Checkpoint on unrecoverable errors.

### 2. Context Engine (`src/agent/context.py`)

Three-tier hybrid memory:

| Layer | Storage | What Goes There | Access Pattern |
|-------|---------|----------------|----------------|
| **Hot** | In-process dict/list | Current plan, messages, scratchpad | Every turn (~ms) |
| **Warm** | GCS JSON | Session history, memories, intermediate results | On-demand (~100ms) |
| **Cold** | BigQuery | Historical analyses, cross-session learnings | SQL query (~1-5s) |

**Session Compaction** (from OpenClaw): When context grows too large, old messages are summarized (via Haiku) and durable facts promoted to warm storage. Bridge messages inserted to maintain role alternation.

**Pod Restart Recovery**: Full session state serializable to/from GCS JSON.

### 3. Tool System (`src/agent/tools/`)

Each tool is a `ToolDef` with:
- `name`: identifier for the API
- `description`: shown to the model
- `input_schema`: JSON Schema for parameters
- `handler`: Python callable → string result

**Security guardrails**:
- BigQuery: Only SELECT allowed (regex guard blocks DDL/DML)
- GCS: Path validation enforces agent/ prefixes only
- Subagent: Model allowlist prevents cost spikes

### 4. Subagent System (`src/agent/tools/subagent.py`)

Two patterns:
1. **Scoped subagent**: Spawn a child Sonnet call with focused objective + limited context. Prevents context pollution.
2. **Fan-out classification**: Spawn N parallel Haiku calls for bulk categorization. ThreadPoolExecutor with configurable max_workers.

### 5. Metaflow Integration (`flows/agent_flow.py`)

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
- Domain context (company, tables, metrics)

## Design Rationale

### Why not SQLite? (OpenClaw's approach)
GKE pods are ephemeral. SQLite state would be lost on restart. GCS gives durable persistence without local state dependency.

### Why not embeddings for memory search?
Phase 1 uses keyword matching (simple, no extra infra). Phase 2 will add Vertex AI Vector Search or local FAISS for semantic recall.

### Why plan-as-artifact? (Hightouch's approach)
Storing the plan as a tool output means the model can reference it in every turn and revise it based on results. This prevents drift in long-running tasks.

### Why subagent isolation? (Both Hightouch + OpenClaw)
Context pollution is the #1 killer of long-running agents. Child LLMs get only the context they need, produce a summary, and the summary is injected back — not the full intermediate work.

## Phase Roadmap

| Phase | What | Status |
|-------|------|--------|
| 1 | Core agent loop, tools, plan management, CLI | Done |
| 2 | Vector search for memory, cost tracking per run | Planned |
| 3 | Slack trigger, cron scheduling, webhook endpoint | Planned |
| 4 | Evaluation framework, human-in-the-loop review | Planned |

## References
- [Hightouch Agent Harness](https://www.amplifypartners.com/blog-posts/how-hightouch-built-their-long-running-agent-harness) — plan-as-artifact, subagent isolation, fan-out classification
- [OpenClaw](https://github.com/openclaw/openclaw) — 4-phase agent loop, hybrid memory, session compaction, cron wakeups
