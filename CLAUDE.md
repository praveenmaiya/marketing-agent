# Marketing Agent

Long-running marketing automation agent for Auxia on GCP. ReAct loop with tool calling, plan-as-artifact management, BM25 memory search, multi-project support, and subagent fan-out.

## Quick Start

```bash
# Run the agent (CLI) with project
python -m src.agent.run --project <project_id> "Analyze treatment performance"

# Dry run (show config + tools, no execution)
python -m src.agent.run --project <id> --dry-run "test"

# Resume a previous session
python -m src.agent.run --project <id> --resume <session-id> "Continue"

# Run on Kubernetes via Metaflow
python flows/agent_flow.py run --prompt "Daily health check"

# Run tests
python -m pytest tests/ -v

# Lint
python -m ruff check src/ tests/
```

## Architecture

See [docs/architecture/system_architecture.md](docs/architecture/system_architecture.md) for the full design.

```
Trigger (CLI / Cron / Slack)
  │
  ▼
Session Router (trigger-based isolation: cli/slack/cron)
  │
  ▼
Orchestrator (ReAct loop on GKE)
  ├── Plan Manager (plan-as-artifact, revisable mid-flight)
  ├── Context Engine (hot/warm/cold hybrid memory)
  │     ├── Hot: in-memory cache
  │     ├── Warm: GCS JSONL append log (per project)
  │     ├── Cold: BigQuery BM25 search
  │     └── Pre-compaction flush (save findings before truncation)
  ├── Tool Router
  │     ├── Auxia Console (list treatments, surfaces, objectives — read-only)
  │     ├── BigQuery (SELECT only, SQL injection guard)
  │     ├── GCS (path-validated, agent prefixes only)
  │     ├── Memory (BM25 search via BigQuery SEARCH)
  │     └── Subagent Spawner (scoped context, fan-out classification)
  └── Checkpoint (GCS persistence, pod restart recovery)
```

## Stack
- Python 3.12+, Anthropic SDK, BigQuery, GCS, Metaflow on GKE
- Models: Sonnet (orchestrator/subagent), Haiku (fan-out classifier)
- Config: `configs/agent.yaml`

## Key Files

### Core
| Path | Purpose |
|------|---------|
| `src/agent/orchestrator.py` | **ReAct loop, plan manager, system prompt + rules, pre-compaction flush** |
| `src/agent/context.py` | **Hybrid memory engine (hot/warm/cold), BM25 search, JSONL persistence** |
| `src/agent/config.py` | Config dataclasses + YAML loader (project-agnostic) |
| `src/agent/session.py` | **Session key routing + trigger-based isolation** |
| `src/agent/run.py` | CLI entrypoint with --project flag |

### Tools
| Path | Purpose |
|------|---------|
| `src/agent/tools/__init__.py` | ToolDef + ToolRegistry |
| `src/agent/tools/auxia.py` | **Auxia Console BFF API (read-only treatments, surfaces, objectives)** |
| `src/agent/tools/bigquery.py` | BigQuery query tool (SELECT only) |
| `src/agent/tools/gcs.py` | GCS read/write (path-validated) |
| `src/agent/tools/memory.py` | Memory search (BM25) + categorized save |
| `src/agent/tools/subagent.py` | Subagent spawning + fan-out |

### Infrastructure
| Path | Purpose |
|------|---------|
| `flows/agent_flow.py` | Metaflow flow for GKE execution |
| `configs/agent.yaml` | Agent configuration (project-agnostic) |
| `src/bq_client.py` | BigQuery client wrapper |
| `src/gcs_utils.py` | GCS utilities |
| `src/config.py` | YAML config loader with env var substitution |

### Docs
| Path | Purpose |
|------|---------|
| `docs/architecture/system_architecture.md` | Full system design |
| `specs/marketing_agent_v1.md` | Original architecture analysis / spec |

## Commands
```bash
# Tests
python -m pytest tests/ -v
python -m pytest tests/ -v --tb=short -q  # Quick

# Lint
python -m ruff check src/ tests/
python -m ruff check --fix src/ tests/  # Auto-fix

# Install dependencies
uv pip install -e ".[dev,agent]"
```

## Security Guardrails
- **SQL injection guard**: Only SELECT queries allowed — DDL/DML blocked by regex
- **GCS path validation**: Only `agent/` prefixes accessible — no traversal
- **Subagent model allowlist**: Only configured models can be used
- **API retry**: Exponential backoff on rate limits, checkpoint on failure
- **Temp file cleanup**: try/finally on all temp files
- **Auxia Console**: Read-only access (Phase 1) — no write operations

## Design Decisions
- See [docs/architecture/system_architecture.md](docs/architecture/system_architecture.md) for rationale
- Plan-as-artifact pattern from Hightouch: plan stored as tool output, model can reference/revise
- Subagent isolation from OpenClaw: child LLMs get scoped context, not full conversation
- Hybrid memory: hot in-memory + warm GCS JSONL + cold BigQuery BM25 search
- Pre-compaction flush from OpenClaw: LLM extracts important findings before context truncation
- Session isolation: trigger-based keys (cli/slack/cron) prevent cross-contamination
- Multi-project: project context loaded dynamically from Auxia Console API
- System prompt rules: anti-looping (2 retries), memory-first, plan-first, session summary

## Memory System
- **Categories**: finding, decision, pattern, session_summary, general
- **Storage**: in-memory (hot) → GCS JSONL (warm) → BigQuery (cold)
- **Search**: BM25 via BigQuery `SEARCH()` function, with hot-layer substring fallback
- **GCS layout**: `gs://{bucket}/agent/memory/{project_id}/memories.jsonl`
- **Pre-compaction**: LLM identifies important findings before context truncation

## Workflow: Plan → Code → Review

### PLAN
1. Create spec in `specs/`
2. Define: problem, approach, validation criteria
3. ASK if unclear

### CODE
1. Follow existing patterns in `src/agent/`
2. Run tests after changes
3. Run linter

### REVIEW
1. Run full test suite
2. Check security guardrails still hold
3. Update docs if architecture changed
