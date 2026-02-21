# Marketing Agent

Long-running marketing agent for campaign analysis on GCP. ReAct loop with tool calling, plan-as-artifact management, hybrid memory, and subagent fan-out.

## Quick Start

```bash
# Run the agent (CLI)
python -m src.agent.run "Analyze Q4 campaign performance"

# Dry run (show config + tools, no execution)
python -m src.agent.run --dry-run "test"

# Resume a previous session
python -m src.agent.run --resume <session-id> "Continue"

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
Trigger (CLI / Cron / Slack / Webhook)
  │
  ▼
Orchestrator (ReAct loop on GKE)
  ├── Plan Manager (plan-as-artifact, revisable mid-flight)
  ├── Context Engine (hot/warm/cold hybrid memory)
  ├── Tool Router
  │     ├── BigQuery (SELECT only, SQL injection guard)
  │     ├── GCS (path-validated, agent prefixes only)
  │     ├── Memory (keyword search, Phase 2: vector similarity)
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
| `src/agent/orchestrator.py` | **ReAct loop, plan manager, system prompt assembly** |
| `src/agent/context.py` | **Hybrid memory engine (hot/warm/cold)** |
| `src/agent/config.py` | Config dataclasses + YAML loader |
| `src/agent/run.py` | CLI entrypoint |

### Tools
| Path | Purpose |
|------|---------|
| `src/agent/tools/__init__.py` | ToolDef + ToolRegistry |
| `src/agent/tools/bigquery.py` | BigQuery query tool (SELECT only) |
| `src/agent/tools/gcs.py` | GCS read/write (path-validated) |
| `src/agent/tools/memory.py` | Memory search/store |
| `src/agent/tools/subagent.py` | Subagent spawning + fan-out |

### Infrastructure
| Path | Purpose |
|------|---------|
| `flows/agent_flow.py` | Metaflow flow for GKE execution |
| `configs/agent.yaml` | Agent configuration |
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

## Design Decisions
- See [docs/architecture/system_architecture.md](docs/architecture/system_architecture.md) for rationale
- Plan-as-artifact pattern from Hightouch: plan stored as tool output, model can reference/revise
- Subagent isolation from OpenClaw: child LLMs get scoped context, not full conversation
- Hybrid memory (not SQLite-only): hot in-memory + warm GCS + cold BigQuery
- Session compaction: summarize old turns, promote durable facts before truncating

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
