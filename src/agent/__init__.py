"""Long-running marketing agent for campaign analysis and recommendations.

Architecture:
    Orchestrator (ReAct loop) -> Tool Router -> {BigQuery, GCS, Memory, Subagents}
                               -> Context Engine (hot/warm/cold memory layers)
                               -> Plan Manager (plan-as-artifact pattern)
"""

from src.agent.config import AgentConfig

__all__ = ["AgentConfig", "Orchestrator"]


def __getattr__(name: str):
    """Lazy import Orchestrator to avoid requiring anthropic at import time."""
    if name == "Orchestrator":
        from src.agent.orchestrator import Orchestrator

        return Orchestrator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
