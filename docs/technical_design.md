# Marketing Agent — Technical Design Document

## 1. Problem Statement

### What We Have Today

Auxia manages marketing treatments (push notifications, in-app messages, emails) across multiple projects. Each project has surfaces, treatment types, objectives, eligibility rules, and performance data spread across:

- **Auxia Console** — treatment configuration, surfaces, objectives
- **BigQuery** — event logs, pipeline outputs, performance metrics
- **GCS** — reports, artifacts, intermediate data

### The Problems

| # | Problem | Impact |
|---|---------|--------|
| 1 | **Manual campaign analysis** — analysts query BigQuery, cross-reference Console, and build reports by hand | Hours per analysis, error-prone, not repeatable |
| 2 | **No persistent memory** — insights from past analyses are lost. The same queries get re-run | Duplicated effort across sessions |
| 3 | **Context window limits** — a single complex analysis can exceed what an LLM can hold in context, causing it to lose track of earlier findings | Incomplete analysis, hallucinated conclusions |
| 4 | **No scheduled monitoring** — anomalies (CTR drops, pipeline failures) go undetected until someone manually checks | Delayed response to production issues |
| 5 | **Multi-project blindness** — each project is analyzed in isolation with no shared patterns across the portfolio | Missed cross-project insights |
| 6 | **Security exposure** — ad-hoc LLM access to BigQuery/GCS has no guardrails against destructive operations | Risk of accidental data mutation |

### What We're Building

An autonomous marketing agent that:

- Analyzes treatment performance, detects anomalies, and generates reports **without human hand-holding**
- **Remembers** findings across sessions using durable, searchable memory
- **Survives** context window limits by intelligently compacting and preserving important information
- Runs **on schedule** for continuous monitoring
- Works across **multiple Auxia projects** with per-project isolation
- Enforces **strict security guardrails** at every layer

---

## 2. Use Cases

### UC-1: Daily Campaign Health Check (Scheduled)

**Trigger**: Cron job, daily at 9am.

The agent autonomously:
1. Searches memory for yesterday's health check findings
2. Creates a plan: verify pipeline output, check CTR/CVR trends, flag anomalies
3. Queries BigQuery for treatment performance metrics
4. Compares against historical baselines from memory
5. Lists active treatments from Auxia Console to cross-reference
6. Saves anomalies and trends to durable memory
7. Writes a markdown report to GCS

### UC-2: Treatment Performance Deep-Dive (On-Demand)

**Trigger**: CLI or Slack.

Prompt: *"Analyze treatment X performance over the last 30 days. Compare against the control group."*

The agent:
1. Fetches treatment details from Auxia Console (targeting rules, objectives, surface)
2. Queries BigQuery for impression, click, and conversion data
3. Spawns a subagent for statistical significance calculation
4. Compares against similar treatments via memory search
5. Produces a structured report with data tables and recommendations

### UC-3: Bulk Treatment Classification (Fan-Out)

**Trigger**: CLI.

Prompt: *"Classify all 200 live treatments by engagement strategy."*

The agent:
1. Lists all LIVE treatments from Auxia Console
2. Uses fan-out classification: spawns 200 parallel Haiku calls to categorize each treatment
3. Aggregates results into a summary table
4. Saves the classification as a durable finding

### UC-4: Cross-Session Pattern Detection

**Trigger**: Any.

After multiple sessions over weeks, the agent:
1. Searches memory for patterns: *"CTR trends by day of week"*
2. Finds stored findings from 10 previous sessions
3. Identifies recurring patterns (e.g., "CTR dips every Monday")
4. Saves the pattern with category `pattern` for future reference

---

## 3. Architecture

### System Diagram

```
                    ┌──────────────────────────────────────┐
                    │           Trigger Layer               │
                    │     CLI    │    Cron    │    Slack     │
                    └────────────────┬─────────────────────┘
                                     │
                    ┌────────────────▼─────────────────────┐
                    │         Session Router                │
                    │   cli:{user}:{project}:{session}      │
                    │   cron:{job}:{project}:{session}       │
                    │   slack:{channel}:{project}:{session}  │
                    └────────────────┬─────────────────────┘
                                     │
                    ┌────────────────▼─────────────────────┐
                    │       Orchestrator (GKE Pod)          │
                    │                                       │
                    │  ┌─────────────────────────────────┐  │
                    │  │ ReAct Loop (Reason → Act → Obs)  │  │
                    │  │  ┌────────────────────────────┐  │  │
                    │  │  │ Plan Manager               │  │  │
                    │  │  │ make → execute → update →   │  │  │
                    │  │  │ revise → checkpoint         │  │  │
                    │  │  └────────────────────────────┘  │  │
                    │  └──────────────┬──────────────────┘  │
                    │                 │                      │
                    │  ┌──────────────▼──────────────────┐  │
                    │  │ Context Engine (3-Tier Memory)   │  │
                    │  │ Hot: in-memory dict/list         │  │
                    │  │ Warm: GCS JSONL append log       │  │
                    │  │ Cold: BigQuery BM25 searchable   │  │
                    │  └──────────────┬──────────────────┘  │
                    │                 │                      │
                    │  ┌──────────────▼──────────────────┐  │
                    │  │ Tool Router (19 tools)           │  │
                    │  │ Auxia Console │ BigQuery │ GCS   │  │
                    │  │ Memory Search │ Subagent Spawner │  │
                    │  └─────────────────────────────────┘  │
                    └──────────────────────────────────────┘
                                     │
                 ┌───────────────────┼───────────────────┐
                 ▼                   ▼                   ▼
         ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
         │ Subagent      │   │ Subagent      │   │ Fan-out       │
         │ SQL Analyst   │   │ Creative      │   │ 200x Haiku    │
         │ (Sonnet)      │   │ Reviewer      │   │ classifiers   │
         └──────────────┘   └──────────────┘   └──────────────┘
```

### Component Inventory

| Component | File | Lines | Purpose |
|-----------|------|-------|---------|
| Orchestrator | `src/agent/orchestrator.py` | 509 | ReAct loop, plan manager, system prompt, compaction |
| Context Engine | `src/agent/context.py` | 547 | 3-tier memory, BM25 search, session persistence |
| Session Router | `src/agent/session.py` | 135 | Trigger-based isolation, session keys |
| Config | `src/agent/config.py` | 97 | Typed dataclasses + YAML loader |
| CLI Runner | `src/agent/run.py` | 214 | Entrypoint, agent wiring, argument parsing |
| Tool Registry | `src/agent/tools/__init__.py` | 71 | ToolDef dataclass + registry |
| Auxia Console Tools | `src/agent/tools/auxia.py` | 304 | BFF API client, project context builder |
| BigQuery Tools | `src/agent/tools/bigquery.py` | 136 | SELECT-only queries, schema inspection |
| GCS Tools | `src/agent/tools/gcs.py` | 133 | Path-validated read/write/list |
| Memory Tools | `src/agent/tools/memory.py` | 132 | BM25 search, categorized save, session history |
| Subagent Tools | `src/agent/tools/subagent.py` | 171 | Scoped spawning, fan-out classification |
| Metaflow Flow | `flows/agent_flow.py` | 98 | GKE execution wrapper |

---

## 4. How Each Component Solves a Problem

### 4.1 Orchestrator — Solves: Manual Analysis + Drift in Long Tasks

**Problem**: Human analysts manually run queries, cross-reference data, and write reports. When LLMs try to do this autonomously, they lose track of the objective in long-running sessions.

**Solution**: A ReAct (Reason-Act-Observe) loop with a **plan-as-artifact** that anchors every turn.

**How it works**:

```
┌──────────────────────────────────────────────────────┐
│  for turn in range(max_turns):                       │
│    1. Build system prompt:                           │
│       - Agent identity + project context             │
│       - Current plan state (objectives + step status)│
│       - 10 behavioral rules                          │
│    2. Call Claude API with all 19 registered tools    │
│    3. If stop_reason == "tool_use":                   │
│       - Execute tool calls → inject results → loop   │
│    4. If stop_reason == "end_turn":                   │
│       - Return final text response                   │
│    5. Every N turns: checkpoint to GCS                │
│    6. If context > 120K tokens: compact               │
└──────────────────────────────────────────────────────┘
```

**Plan-as-artifact**: The plan is stored as a tool-call output, not as a static instruction. This means the LLM sees the plan (with step statuses and results) in every turn and can revise it based on what it learns. Each step tracks `pending → in_progress → completed → failed`.

```python
# Plan structure
{
    "objective": "Analyze Q4 campaign performance",
    "status": "active",
    "steps": [
        {"id": 1, "description": "Search memory for prior Q4 analyses", "status": "completed", "result": "Found 3 prior analyses"},
        {"id": 2, "description": "Query BigQuery for Q4 metrics",        "status": "in_progress", "result": null},
        {"id": 3, "description": "Compare against Q3 baseline",          "status": "pending",     "result": null},
    ]
}
```

**10 System Rules** enforce disciplined behavior:

| Rule | What It Prevents |
|------|-----------------|
| Plan-first | Aimless exploration — agent must create a plan before executing |
| Memory-first | Redundant work — agent must search memory before new analysis |
| Anti-looping (2 retries) | Infinite retry loops on failed actions |
| Session summary on exit | Lost findings — agent saves a summary before finishing |
| Subagent delegation (>3 tools) | Context pollution from complex sub-analyses |
| Dry-run before queries | Wasted BigQuery spend on invalid SQL |
| No duplicate queries | Redundant BigQuery costs |
| Concise, data-driven | Verbose, narrative-heavy responses |

**API Retry**: Exponential backoff (2^attempt seconds) on rate limits. Checkpoint on unrecoverable errors so work is not lost.

---

### 4.2 Context Engine — Solves: Context Window Limits + Lost Memory

**Problem**: LLMs have finite context windows (~200K tokens). A complex analysis easily exceeds this. When context is truncated, important findings are permanently lost. Across sessions, there is zero persistent memory — the same analysis gets repeated.

**Solution**: A 3-tier hybrid memory system with intelligent compaction.

#### Three Memory Layers

| Layer | Storage | Latency | What Goes There | Lifecycle |
|-------|---------|---------|-----------------|-----------|
| **Hot** | In-process `dict`/`list` | ~ms | Current plan, messages, scratchpad, memory cache | Single session |
| **Warm** | GCS JSONL per session | ~100ms | Append-only memory log, session checkpoint | Durable, per-session file |
| **Cold** | BigQuery table | ~1-5s | All memories across all sessions, BM25 indexed | Durable, cross-session searchable |

#### Memory Write Path

When the agent calls `save_memory(content, category)`:

```
save_memory("Email CTR dropped 15% vs last week", category="finding")
    │
    ├── 1. Hot:  append to self._memories list (in-memory)
    │
    ├── 2. Warm: append JSONL line to GCS
    │          gs://{bucket}/agent/memory/{project_id}/memories_{session_id}.jsonl
    │
    └── 3. Cold: INSERT row into BigQuery `memories` table
               (id, project_id, category, content, session_id, created_at)
```

**Why per-session JSONL files**: A shared file has a read-modify-write race condition when multiple sessions run concurrently for the same project. Per-session files are append-safe without coordination.

#### Memory Search Path

When the agent calls `search_memory(query)`:

```
search_memory("CTR trends")
    │
    ├── 1. Cold: BigQuery BM25 full-text search
    │       SELECT * FROM memories
    │       WHERE project_id = @project_id
    │         AND SEARCH(content, @query)
    │       ORDER BY created_at DESC
    │
    └── 2. Hot: substring fallback for recent memories not yet in BigQuery
           (deduplicated by memory ID)
```

**Why BM25 via BigQuery SEARCH, not vector embeddings**: BigQuery `SEARCH()` provides full-text search with zero additional infrastructure — no vector DB, no embedding model, no index maintenance. Sufficient for keyword-based memory recall. Vector search can be added as a complementary layer later.

#### Memory Categories

| Category | What | Example |
|----------|------|---------|
| `finding` | Data-driven observations | "Email CTR dropped 15% this week" |
| `decision` | Choices made during analysis | "Switched from weekly to daily push frequency" |
| `pattern` | Recurring trends across sessions | "CTR always dips on Mondays" |
| `session_summary` | Auto-generated session recap | "Analyzed Q4 push performance, found 3 anomalies" |
| `general` | Everything else | "BigQuery table X has column Y" |

#### Pre-Compaction Memory Flush

**Problem within the problem**: Simply truncating old messages destroys information. The LLM may have discovered something important 50 messages ago that it hasn't saved.

**Solution**: Before compaction, the LLM is asked to review the conversation and extract important findings:

```
┌──────────────────────────────────────────────────────┐
│  Context at ~120K tokens — compaction triggered       │
│                                                       │
│  Step 1: Pre-compaction flush                         │
│    - Send old messages to Haiku                       │
│    - Prompt: "Extract important findings, decisions,  │
│      and patterns that should be remembered"          │
│    - Save each extracted item to durable memory       │
│                                                       │
│  Step 2: Summarize                                    │
│    - Haiku summarizes old messages into ~4K tokens     │
│                                                       │
│  Step 3: Compact                                      │
│    - Replace old messages with:                       │
│      [summary] + [bridge message] + [last 6 messages] │
│    - Bridge messages maintain role alternation         │
└──────────────────────────────────────────────────────┘
```

#### Pod Restart Recovery

Full session state is serializable to/from GCS JSON:

```json
{
    "session_id": "abc123",
    "project_id": "project-x",
    "turn_count": 15,
    "plan": { ... },
    "scratchpad": { ... },
    "memories": [ ... ],
    "messages": [ /* last 20 */ ]
}
```

GCS path: `agent/memory/{project_id}/sessions/{session_id}.json`

On pod restart, the agent reloads this state and continues from the last checkpoint.

---

### 4.3 Session Router — Solves: Cross-Contamination Between Triggers

**Problem**: Different invocation contexts (CLI debug, Slack request, scheduled cron) have different usage patterns. A CLI debug session should not pollute a cron job's memory. Concurrent runs for the same project must not overwrite each other's state.

**Solution**: Structured session keys with trigger-based isolation.

```
Session Key Format:
    {trigger}:{source}:{project_id}:{session_id}

Examples:
    cli:praveenm:project-x:a1b2c3d4
    slack:C0123CHAN:project-x:e5f6g7h8
    cron:daily-health:project-x:i9j0k1l2
```

| Field | Source |
|-------|--------|
| `trigger` | Enum: `cli`, `slack`, `cron` |
| `source` | CLI: `getpass.getuser()`, Slack: channel ID, Cron: job name |
| `project_id` | From `--project` CLI flag or `AUXIA_PROJECT_ID` env var |
| `session_id` | Auto-generated UUID (8 chars) or provided via `--resume` |

**GCS path scoping**: All state is under `agent/memory/{project_id}/`, ensuring project-level isolation. Session state is under `agent/memory/{project_id}/sessions/{session_id}.json`.

---

### 4.4 Tool System — Solves: Structured LLM-to-Infrastructure Access

**Problem**: The LLM needs to query BigQuery, read/write GCS, browse Auxia Console, manage memory, and spawn subagents. Each integration needs guardrails.

**Solution**: A typed tool registry where each tool is a `ToolDef` with name, description, JSON Schema, and a handler function. Tools are registered at startup and exposed to the Claude API.

#### 19 Registered Tools

| Category | Tool | What It Does |
|----------|------|-------------|
| **Plan** | `make_plan` | Create a structured plan with objective + steps |
| | `update_plan_step` | Mark a step as completed/failed with result |
| | `revise_plan` | Replace remaining steps (keeps completed ones) |
| **Auxia Console** | `list_treatments` | Browse treatments, filter by surface/type/state |
| | `get_treatment` | Get full treatment details (content, rules, objectives) |
| | `list_surfaces` | List placement locations (home page, checkout, etc.) |
| | `list_treatment_types` | List categories (push, in-app, email) |
| | `list_objectives` | List business goals (purchases, engagement) |
| | `list_data_fields` | List user attributes for targeting |
| **BigQuery** | `query_bigquery` | Execute SELECT queries with dry_run option |
| | `get_table_schema` | Get column names and types |
| | `check_table_exists` | Verify a table exists before querying |
| **GCS** | `read_gcs` | Read text files from agent-prefixed paths |
| | `write_gcs` | Write reports/artifacts to agent-prefixed paths |
| | `list_gcs` | List blobs under an agent-prefixed path |
| **Memory** | `search_memory` | BM25 search across all sessions |
| | `save_memory` | Persist a finding/decision/pattern to durable storage |
| | `get_session_history` | Get summaries of past sessions |
| **Subagent** | `spawn_subagent` | Spawn a focused child LLM with scoped context |
| | `fan_out_classify` | Classify N items in parallel via Haiku |

---

### 4.5 Auxia Console Integration — Solves: Agent Blindness to Treatment Config

**Problem**: The agent can query BigQuery for performance data but has no visibility into treatment configuration — what treatments exist, what surfaces they target, what objectives they optimize for.

**Solution**: Read-only HTTP client wrapping the Auxia Console BFF API.

```
AuxiaBFFClient
    ├── GET /api/projects/{id}/treatments       → list_treatments tool
    ├── GET /api/projects/{id}/treatments/{tid}  → get_treatment tool
    ├── GET /api/projects/{id}/surfaces          → list_surfaces tool
    ├── GET /api/projects/{id}/treatment-types   → list_treatment_types tool
    ├── GET /api/projects/{id}/objectives        → list_objectives tool
    └── GET /api/projects/{id}/data-fields       → list_data_fields tool
```

**Dynamic project context**: On startup, `build_project_context()` fetches surfaces, treatment types, and objectives and injects them into the system prompt. This gives the agent awareness of the project's structure before it starts working.

**Authentication**: Uses the Auxia BFF session cookie from `AUXIA_SESSION_COOKIE` env var.

**Phase 1 constraint**: Read-only. No write operations. Treatment mutations are deferred to Phase 4 behind human-in-the-loop review.

---

### 4.6 Subagent System — Solves: Context Pollution in Complex Analysis

**Problem**: When the agent performs a complex sub-analysis (e.g., statistical significance test), the intermediate work pollutes the main context window. Irrelevant tool outputs accumulate, degrading the agent's ability to reason about the primary task.

**Solution**: Two patterns for delegating work to child LLMs:

#### Pattern 1: Scoped Subagent

A child Sonnet call with a focused objective and limited context. The child produces a concise summary, and only the summary is injected back into the parent context.

```
Parent context (50K tokens)
    │
    ├── spawn_subagent(
    │       objective="Calculate statistical significance of treatment X vs control",
    │       context="Treatment X: 1200 clicks / 50000 impressions. Control: 900 clicks / 48000 impressions."
    │   )
    │
    │   Child LLM (Sonnet):
    │       - Gets ONLY the scoped context (not the full 50K)
    │       - Produces: "Z-score: 4.2, p-value: 0.000013. Statistically significant at p<0.001."
    │
    └── Result injected: "Z-score: 4.2, p-value: 0.000013. Statistically significant at p<0.001."
        (Clean summary, not the intermediate work)
```

#### Pattern 2: Fan-Out Classification

For bulk categorization tasks. Spawns N parallel Haiku calls via `ThreadPoolExecutor`.

```
fan_out_classify(
    items=["Treatment A copy...", "Treatment B copy...", ... (200 items)],
    prompt="Classify this treatment's engagement strategy",
    categories=["urgency", "social_proof", "personalization", "discount", "informational"]
)
    │
    ├── ThreadPoolExecutor(max_workers=20)
    │   ├── Haiku call 1: {"category": "urgency", "confidence": 0.92}
    │   ├── Haiku call 2: {"category": "discount", "confidence": 0.87}
    │   ├── ...
    │   └── Haiku call 200: {"category": "social_proof", "confidence": 0.94}
    │
    └── Aggregated JSON result injected into parent context
```

**Model allowlist**: Only models in the config (`orchestrator`, `subagent`, `classifier`) can be used. Prevents accidental Opus usage and cost spikes.

---

## 5. End-to-End Data Flow

### Example: Agent Startup → Analysis → Report

```
1. TRIGGER
   $ python -m src.agent.run --project project-x "Analyze Q4 push notification performance"

2. SESSION SETUP
   create_session_key(trigger=CLI, project="project-x") → cli:praveenm:project-x:a1b2c3d4
   ContextEngine(session_id="a1b2c3d4", project_id="project-x")

3. PROJECT CONTEXT (Auxia Console)
   build_project_context() →
     GET /api/projects/project-x/surfaces         → "Surfaces: Home Page, Checkout, Profile"
     GET /api/projects/project-x/treatment-types   → "Treatment types: Push, In-App, Email"
     GET /api/projects/project-x/objectives        → "Objectives: Purchases, Engagement, Retention"
   → Injected into system prompt

4. REACT LOOP

   Turn 1 (Reason → Act):
     LLM decides: "Search memory first"
     Tool call: search_memory(query="Q4 push notification analysis")
     Result: 2 findings from previous sessions

   Turn 2 (Reason → Act):
     LLM decides: "Make a plan"
     Tool call: make_plan(
       objective="Analyze Q4 push notification performance",
       steps=["Query push metrics", "Compare vs Q3", "Identify anomalies", "Generate report"]
     )

   Turn 3 (Reason → Act):
     Tool call: query_bigquery(sql="SELECT ... FROM events WHERE ...", dry_run=true)
     Result: "Dry run succeeded"

   Turn 4 (Reason → Act):
     Tool call: query_bigquery(sql="SELECT ... FROM events WHERE ...")
     Result: 15 rows of metrics

   Turn 5 (Reason → Act):
     Tool call: update_plan_step(step_id=1, status="completed", result="Got Q4 metrics")
     Tool call: list_treatments(state="LIVE", treatment_type="Push")
     Result: 12 active push treatments

   Turn 6 (Reason → Act):
     Tool call: spawn_subagent(
       objective="Compare Q4 vs Q3 push metrics and calculate deltas",
       context="Q4: {metrics}. Q3: {prior_findings_from_memory}"
     )
     Result: "CTR: +5.2%, CVR: -1.8%, Volume: +22%"

   Turn 7 (Reason → Act):
     Tool call: save_memory(content="Q4 push: CTR +5.2%, CVR -1.8%", category="finding")
     Tool call: update_plan_step(step_id=2, status="completed")

   Turn 8 (Reason → Act):
     Tool call: write_gcs(path="agent/artifacts/q4-push-report.md", content="# Q4 Push Report\n...")
     Tool call: save_memory(content="Session: Q4 push analysis complete", category="session_summary")

   Turn 9 (end_turn):
     Final response: "Q4 push analysis complete. CTR improved 5.2% over Q3..."

5. CHECKPOINT
   Session state → GCS: agent/memory/project-x/sessions/a1b2c3d4.json

6. RESULT
   Report at: gs://auxia-agent/agent/artifacts/q4-push-report.md
   4 memories saved to BigQuery (searchable by future sessions)
```

---

## 6. Deployment Architecture

### Infrastructure

```
┌──────────────────────────────────────────────────────────┐
│                     GKE Cluster                           │
│                                                           │
│   ┌─────────────────────┐   ┌─────────────────────────┐  │
│   │  Agent Pod           │   │  Agent Pod (cron)        │  │
│   │  - Python 3.12       │   │  - Metaflow FlowSpec     │  │
│   │  - Anthropic SDK     │   │  - @schedule(daily)      │  │
│   │  - google-cloud-*    │   │  - @retry(times=2)       │  │
│   └────────┬────────────┘   └────────┬──────────────────┘  │
│            │                         │                      │
└────────────┼─────────────────────────┼──────────────────────┘
             │                         │
     ┌───────▼─────────────────────────▼───────┐
     │              GCP Services                │
     │                                          │
     │  ┌────────────┐  ┌────────────────────┐  │
     │  │  BigQuery   │  │  GCS               │  │
     │  │  - Events   │  │  - Sessions        │  │
     │  │  - Metrics  │  │  - Memories (JSONL) │  │
     │  │  - Memories │  │  - Artifacts        │  │
     │  └────────────┘  └────────────────────┘  │
     │                                          │
     │  ┌────────────────────────────────────┐  │
     │  │  Auxia Console BFF API             │  │
     │  │  - Treatments, surfaces, objectives │  │
     │  └────────────────────────────────────┘  │
     │                                          │
     │  ┌────────────────────────────────────┐  │
     │  │  Anthropic API                     │  │
     │  │  - Claude Sonnet (orchestrator)     │  │
     │  │  - Claude Sonnet (subagent)         │  │
     │  │  - Claude Haiku (classifier/summary)│  │
     │  └────────────────────────────────────┘  │
     └──────────────────────────────────────────┘
```

### Metaflow Integration

The agent is wrapped in a Metaflow `FlowSpec` for GKE execution:

```python
class MarketingAgentFlow(FlowSpec):
    prompt = Parameter("prompt", default="Daily health check")

    @step
    def start(self):
        agent = build_agent(config, project_id="...")
        self.result = agent.run(self.prompt)      # Stored as Metaflow artifact
        self.session_id = agent.context.session_id
        self.next(self.end)
```

- **`@schedule`**: Cron-triggered daily/weekly runs
- **`@retry(times=2)`**: Automatic retry on pod OOM or API failures
- **`@kubernetes`**: Resource requests/limits for GKE pods
- **Artifacts**: Result, session ID, plan JSON stored via Metaflow's artifact system

### Configuration

All config in `configs/agent.yaml` with `${ENV_VAR:-default}` substitution:

```yaml
models:
  orchestrator: claude-sonnet-4-6        # Main reasoning
  subagent: claude-sonnet-4-6            # Scoped child tasks
  classifier: claude-haiku-4-5-20251001  # Fan-out + summaries

tokens:
  max_context: 180000           # Context window budget
  compaction_threshold: 120000  # Trigger compaction here
  summary_target: 4000          # Compacted summary size

bigquery:
  project: ${BQ_PROJECT:-auxia-reporting}
  max_bytes_billed: 10737418240  # 10 GB safety limit

gcs:
  bucket: ${GCS_BUCKET:-auxia-agent}

agent:
  max_turns: 50                 # Max ReAct iterations
  checkpoint_interval: 5        # GCS checkpoint every N turns
  fan_out_max_parallel: 20      # Max concurrent Haiku calls
```

### Environment Variables

| Variable | Purpose | Required |
|----------|---------|----------|
| `ANTHROPIC_API_KEY` | Claude API authentication | Yes |
| `BQ_PROJECT` | BigQuery project ID | No (default: `auxia-reporting`) |
| `BQ_DATASET` | BigQuery dataset | No (default: `agent_dataset`) |
| `GCS_BUCKET` | GCS bucket for state/artifacts | No (default: `auxia-agent`) |
| `AUXIA_SESSION_COOKIE` | Auxia Console BFF auth cookie | If Auxia Console enabled |
| `AUXIA_PROJECT_ID` | Default project ID | No (prefer `--project` flag) |

---

## 7. Security Model

### Threat Model

| Threat | Vector | Mitigation |
|--------|--------|------------|
| **SQL injection** | LLM generates destructive SQL | Regex guard: only `SELECT` allowed. DDL/DML keywords blocked. |
| **GCS path traversal** | LLM accesses files outside agent scope | Path validation: must start with `agent/` prefix. `..` rejected. |
| **Cost spike** | LLM spawns expensive model calls | Model allowlist: only configured models (`sonnet`, `haiku`) allowed. |
| **BigQuery cost spike** | LLM runs full-table scans | `max_bytes_billed: 10 GB` safety cap per query. |
| **Cross-project data leak** | Agent accesses wrong project's data | GCS paths scoped to `agent/memory/{project_id}/`. BM25 queries filtered by `project_id`. |
| **Cross-session contamination** | CLI session pollutes cron job state | Trigger-based session keys: `cli:user:proj:id` vs `cron:job:proj:id` |
| **Infinite loops** | LLM retries same failed action forever | Rule 6: max 2 retries on same action, then report and move on |
| **Context overflow** | Long session exceeds token limit | Pre-compaction flush + summarization at 120K tokens |
| **API query injection** | Malicious params in Auxia Console calls | `urllib.parse.urlencode()` for all BFF API query parameters |
| **Table ID injection** | Malicious BigQuery table identifiers | Regex validation: `^[a-zA-Z0-9_.-]+\.[a-zA-Z0-9_.-]+\.[a-zA-Z0-9_]+$` |

### SQL Injection Guard (Detail)

```python
_FORBIDDEN_SQL = re.compile(
    r"\b(DROP|DELETE|TRUNCATE|INSERT|UPDATE|MERGE|CREATE|ALTER|GRANT|REVOKE)\b",
    re.IGNORECASE,
)

def query_bigquery(params):
    sql = params["sql"]
    if _FORBIDDEN_SQL.search(sql):
        return "ERROR: Only SELECT queries are allowed. DDL/DML statements are blocked."
```

**Exception**: The `_ensure_memories_table` DDL creates the memories table on first use. This is:
- Not user-controlled — uses config values only
- Runs once per session (cached check)
- Required for bootstrapping the memory subsystem

### GCS Path Validation (Detail)

```python
def _validate_gcs_path(path, config):
    allowed = (config.session_prefix, config.artifact_prefix, config.memory_prefix)
    if ".." in path:
        raise ValueError("Path traversal (..) is not allowed.")
    if not any(path.startswith(prefix) for prefix in allowed):
        raise ValueError(f"Path must start with one of: {allowed}")
```

Allowed prefixes: `agent/sessions/`, `agent/artifacts/`, `agent/memory/`

---

## 8. GCS Storage Layout

```
gs://{bucket}/
  agent/
    memory/
      {project_id}/
        memories_{session_id}.jsonl     # Per-session memory log (append-only)
        sessions/
          {session_id}.json             # Session state checkpoint
    artifacts/
      {report_name}.md                  # Generated reports
    sessions/
      (reserved for future use)
```

---

## 9. Model Usage & Cost Considerations

### Model Roles

| Role | Model | When Used | Token Profile |
|------|-------|-----------|---------------|
| Orchestrator | Sonnet | Every ReAct turn | ~2K input + 1K output per turn |
| Subagent | Sonnet | Delegated analysis | ~1K input + 1K output per call |
| Classifier | Haiku | Fan-out, compaction summaries, pre-compaction flush | ~500 input + 256 output per call |

### Cost Drivers

| Operation | Typical Volume | Cost Driver |
|-----------|---------------|-------------|
| ReAct turns per session | 5-25 turns | Sonnet input/output tokens |
| Subagent calls per session | 0-5 | Sonnet (focused, smaller context) |
| Fan-out classifications | 0-200 per session | Haiku (cheapest model) |
| Compaction summaries | 0-2 per session | Haiku |
| BigQuery queries | 3-15 per session | Bytes scanned (capped at 10 GB) |
| GCS operations | 5-20 per session | Negligible |

### Cost Controls

1. **Model allowlist** — prevents accidental Opus usage
2. **`max_bytes_billed`** — 10 GB cap per BigQuery query
3. **`max_turns: 50`** — caps ReAct loop iterations
4. **`fan_out_max_parallel: 20`** — caps concurrent Haiku calls
5. **Haiku for summaries** — cheapest model for non-reasoning tasks
6. **Subagent scoped context** — child LLMs get minimal context, reducing input tokens

---

## 10. Test Coverage

53 unit tests across 6 test files:

| Test File | What It Covers |
|-----------|---------------|
| `test_agent_config.py` | YAML loading, defaults, env var substitution |
| `test_agent_tools.py` | ToolDef, ToolRegistry, BigQuery SQL guard, GCS path validation |
| `test_agent_context.py` | Memory save/search, compaction, pre-compaction flush, serialization |
| `test_agent_orchestrator.py` | Plan lifecycle, ReAct loop, API retry, system prompt assembly |
| `test_agent_session.py` | Session key creation/parsing, trigger isolation |
| `test_agent_auxia_tools.py` | Auxia BFF client, tool creation, project context builder |

All external dependencies (Anthropic API, BigQuery, GCS, Auxia Console) are mocked.

---

## 11. Phase Roadmap

| Phase | Status | What | Key Deliverable |
|-------|--------|------|-----------------|
| **1** | Done | Core agent loop, tools, plan management, CLI, Metaflow | Autonomous analysis via CLI, GKE execution |
| **2** | Done | BM25 memory, multi-project, Auxia Console tools, session routing | Cross-session learning, project-aware agent |
| **3** | Planned | Slack trigger, cron scheduling, cost tracking per run | Continuous monitoring, team accessibility |
| **4** | Planned | Evaluation framework, human-in-the-loop review, treatment write actions | Quality assurance, safe write operations |

### Phase 3 Scope (Next)

- **Slack trigger**: Respond to messages in a Slack channel. Route through session router with `slack:{channel}:{project}:{session}` keys.
- **Cron scheduling**: Metaflow `@schedule` with configurable cadence. Daily health checks, weekly performance summaries.
- **Cost tracking**: Per-session token usage and BigQuery bytes billed. Stored in session checkpoint, aggregatable for reporting.

### Phase 4 Scope (Future)

- **Evaluation framework**: Automated scoring of agent outputs against ground truth. Regression tests for common analysis tasks.
- **Human-in-the-loop**: For high-stakes actions (treatment modifications, budget changes), the agent proposes a change and waits for human approval before executing.
- **Treatment write actions**: Create/modify treatments via Auxia Console API. Gated behind human approval and audit logging.

---

## 12. Running the Agent

```bash
# Install dependencies
uv pip install -e ".[dev,agent]"

# Interactive analysis
python -m src.agent.run --project <project_id> "Analyze treatment performance"

# Dry run (show config + tools, no execution)
python -m src.agent.run --project <project_id> --dry-run "test"

# Resume a previous session
python -m src.agent.run --project <project_id> --resume <session-id> "Continue analysis"

# Scheduled execution on GKE
python flows/agent_flow.py run --prompt "Daily health check"

# Tests
python -m pytest tests/ -v

# Lint
python -m ruff check src/ tests/
```
