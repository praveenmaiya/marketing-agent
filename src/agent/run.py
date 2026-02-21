"""CLI entrypoint for the marketing agent.

Usage:
    # Interactive prompt
    python -m src.agent.run "Analyze Q4 campaign performance"

    # Resume a previous session
    python -m src.agent.run --resume abc123 "Continue the analysis"

    # With custom config
    python -m src.agent.run --config configs/agent.yaml "What's the CTR trend?"

    # Dry run (show config, don't execute)
    python -m src.agent.run --dry-run "test prompt"
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src.agent.config import AgentConfig
from src.agent.context import ContextEngine
from src.agent.orchestrator import Orchestrator
from src.agent.tools.bigquery import create_bigquery_tools
from src.agent.tools.gcs import create_gcs_tools
from src.agent.tools.memory import create_memory_tools
from src.agent.tools.subagent import create_subagent_tools

logger = logging.getLogger(__name__)


def build_agent(config: AgentConfig, session_id: str | None = None) -> Orchestrator:
    """Build a fully-wired agent with all tools."""
    context = ContextEngine(config, session_id=session_id)

    orchestrator = Orchestrator(config, context=context)

    # Wire up domain tools
    orchestrator.add_tools(create_bigquery_tools(config.bigquery))
    orchestrator.add_tools(create_gcs_tools(config.gcs))
    orchestrator.add_tools(create_memory_tools(context))
    orchestrator.add_tools(create_subagent_tools(config))

    return orchestrator


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Holley Marketing Agent — long-running campaign analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("prompt", nargs="?", help="Task or question for the agent")
    parser.add_argument(
        "--config",
        default="configs/agent.yaml",
        help="Path to agent config YAML (default: configs/agent.yaml)",
    )
    parser.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="Resume a previous session by ID",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show config and tools without executing",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Load config
    config_path = Path(args.config)
    if config_path.exists():
        config = AgentConfig.from_yaml(str(config_path))
        logger.info(f"Config loaded from {config_path}")
    else:
        config = AgentConfig.default()
        logger.info("Using default config")

    # Build agent
    agent = build_agent(config, session_id=args.resume)

    # Resume session if requested
    if args.resume:
        if agent.context.load_session_from_gcs(args.resume):
            logger.info(f"Resumed session {args.resume}")
        else:
            logger.warning(f"Could not restore session {args.resume}, starting fresh")

    # Dry run: show config and exit
    if args.dry_run:
        print("=== Agent Configuration ===")
        print(f"Orchestrator model: {config.models.orchestrator}")
        print(f"Subagent model: {config.models.subagent}")
        print(f"Classifier model: {config.models.classifier}")
        print(f"Max turns: {config.agent.max_turns}")
        print(f"BigQuery project: {config.bigquery.project}")
        print(f"GCS bucket: {config.gcs.bucket}")
        print(f"\n=== Registered Tools ({len(agent.registry.list_tools())}) ===")
        for tool in agent.registry.list_tools():
            print(f"  - {tool.name}: {tool.description[:80]}")
        print("\n=== Session ===")
        print(f"Session ID: {agent.context.session_id}")
        return

    # Get prompt
    prompt = args.prompt
    if not prompt:
        if not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        else:
            print("Enter your task (Ctrl+D to submit):")
            prompt = sys.stdin.read().strip()

    if not prompt:
        parser.error("No prompt provided")

    # Run the agent
    print(f"\n--- Agent Session {agent.context.session_id} ---\n")
    result = agent.run(prompt)
    print(result)
    print(f"\n--- Session {agent.context.session_id} complete ---")


if __name__ == "__main__":
    main()
