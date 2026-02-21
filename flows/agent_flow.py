"""Metaflow flow for running the marketing agent on GKE.

Wraps the agent in a Metaflow FlowSpec for:
- Scheduled execution via @schedule
- Retry on failure via @retry
- Artifact storage via self.* attributes
- Kubernetes execution via @kubernetes

Usage:
    # Run locally
    python flows/agent_flow.py run --prompt "Analyze Q4 campaign performance"

    # Run on Kubernetes (via run.sh)
    ./flows/run.sh flows/agent_flow.py "--prompt 'Daily health check'"

    # Schedule daily
    # (Add @schedule(daily=True) to the start step)
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from metaflow import FlowSpec, Parameter, current, retry, step

logger = logging.getLogger(__name__)


class MarketingAgentFlow(FlowSpec):
    """Metaflow flow that runs the marketing agent."""

    prompt = Parameter(
        "prompt",
        help="Task or question for the agent",
        default="Run daily campaign health check: verify pipeline output, check CTR trends, flag anomalies.",
    )

    config_path = Parameter(
        "config_path",
        help="Path to agent config YAML",
        default="configs/agent.yaml",
    )

    resume_session = Parameter(
        "resume_session",
        help="Session ID to resume (optional)",
        default="",
    )

    @step
    def start(self):
        """Initialize the agent and run the task."""
        # Import here to avoid dependency issues in Metaflow DAG parsing
        from src.agent.config import AgentConfig
        from src.agent.run import build_agent

        # Load config
        if Path(self.config_path).exists():
            config = AgentConfig.from_yaml(self.config_path)
        else:
            config = AgentConfig.default()

        # Build and run agent
        session_id = self.resume_session if self.resume_session else None
        agent = build_agent(config, session_id=session_id)

        if session_id:
            agent.context.load_session_from_gcs(session_id)

        self.result = agent.run(self.prompt)
        self.session_id = agent.context.session_id
        self.turn_count = agent.context.turn_count

        # Store plan if one was created
        plan = agent.context.get_plan()
        self.plan_json = json.dumps(plan, default=str) if plan else None

        self.next(self.end)

    @step
    def end(self):
        """Report results."""
        print(f"\n=== Agent Result (session {self.session_id}) ===\n")
        print(self.result)
        print(f"\nTurns: {self.turn_count}")
        if self.plan_json:
            plan = json.loads(self.plan_json)
            completed = sum(1 for s in plan.get("steps", []) if s["status"] == "completed")
            total = len(plan.get("steps", []))
            print(f"Plan: {completed}/{total} steps completed")


if __name__ == "__main__":
    MarketingAgentFlow()
