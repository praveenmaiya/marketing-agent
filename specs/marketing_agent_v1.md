# Marketing Agent v1 — Architecture Analysis & Spec

## Context

Analyzed two reference architectures to design a GCP-native marketing agent:
- **Hightouch Agent Harness** — long-running marketing agent for campaign analysis
- **OpenClaw** — open-source personal AI agent (68K+ GitHub stars)

Goal: Build an equivalent system on our GCP stack (BigQuery, Vertex AI, GCS, Metaflow on GKE).

## What's Actually Great (and What's Hype)

### Hightouch: 3 Genuinely Good Ideas

| Pattern | What It Is | Why It's Good |
|---------|-----------|---------------|
| **Plan-as-artifact** | Plan stored as tool-call output, revisable mid-flight | Model can reference/revise; avoids drift in long runs |
| **Subagent isolation** | Spawn child LLM with scoped context + single objective | Prevents context pollution — the #1 killer of long-running agents |
| **Fan-out classification** | Hundreds of parallel Haiku calls instead of embeddings | Cheaper + more capable than RAG for multimodal creative analysis |

**What they DON'T discuss** (red flags for a VC blog): failure recovery, cost per run, latency, evaluation, guardrails, state persistence across crashes.

### OpenClaw: The Blueprint That Matters

| Pattern | Implementation | Why It Matters |
|---------|---------------|----------------|
| **Gateway control plane** | Single WebSocket server, routes all channels | Clean separation of routing vs. reasoning |
| **4-phase agent loop** | Session resolve → Context assemble → Execute → Persist | Structured lifecycle |
| **Hybrid memory** | SQLite + vector embeddings + BM25 keyword search | Semantic recall + exact match |
| **Session compaction** | Auto-summarize old context + flush durable facts to memory | Solves "context fills up" systematically |
| **Cron wakeups** | Agent evaluates task list on schedule | Enables truly autonomous work |
| **Agent-to-agent messaging** | sessions_send, sessions_spawn, sessions_history | Multi-agent coordination without shared context pollution |

## V1 Implementation

### What Was Built (Phase 1)

1. **Orchestrator** — ReAct loop with Anthropic API, plan-execute-revise cycle
2. **Tool System** — BigQuery (SELECT only), GCS (path-validated), memory, subagent spawner
3. **Context Engine** — Hot/warm/cold hybrid memory with session compaction
4. **Plan Manager** — Plan-as-artifact with create/update/revise operations
5. **Subagent System** — Scoped spawning + fan-out classification via ThreadPoolExecutor
6. **CLI Entrypoint** — argparse with --resume, --dry-run, --config
7. **Metaflow Integration** — FlowSpec wrapper for GKE execution

### Security Guardrails
- SQL injection guard (regex blocks DDL/DML)
- GCS path validation (agent/ prefixes only)
- Subagent model allowlist
- API retry with exponential backoff
- Temp file cleanup with try/finally

### Test Coverage
- 53 unit tests across 4 test files
- Covers: config, tools, context engine, plan manager, orchestrator
- Mocks: Anthropic API, BigQuery, GCS

## Honest Assessment

| Option | Verdict |
|--------|---------|
| **Claude Code (current workflow)** | Already works for ad-hoc analysis. Not persistent/scheduled. |
| **This agent (Phase 1)** | Good for automated daily health checks, scheduled analysis. |
| **Full platform (Phases 2-4)** | Only worth it if we expand beyond single-company scope. |

## What's Next (if needed)

- **Phase 2**: Vector search for memory (Vertex AI or FAISS), cost tracking per run
- **Phase 3**: Slack trigger, cron scheduling, webhook endpoint
- **Phase 4**: Evaluation framework, human-in-the-loop for high-stakes actions

## Sources
- [Hightouch Agent Harness (Amplify Partners)](https://www.amplifypartners.com/blog-posts/how-hightouch-built-their-long-running-agent-harness)
- [OpenClaw GitHub](https://github.com/openclaw/openclaw)
- [OpenClaw Documentation](https://docs.openclaw.ai/)
