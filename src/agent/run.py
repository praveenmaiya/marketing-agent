"""CLI entrypoint for the marketing agent.

Usage:
    # Interactive prompt with project
    python -m src.agent.run --project <project_id> "Analyze treatment performance"

    # Resume a previous session
    python -m src.agent.run --project <id> --resume abc123 "Continue"

    # With custom config
    python -m src.agent.run --config configs/agent.yaml --project <id> "Query"

    # Dry run (show config, don't execute)
    python -m src.agent.run --project <id> --dry-run "test prompt"
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src.agent.config import AgentConfig
from src.agent.context import ContextEngine
from src.agent.orchestrator import Orchestrator
from src.agent.session import TriggerType, create_session_key
from src.agent.tools.bigquery import create_bigquery_tools
from src.agent.tools.gcs import create_gcs_tools
from src.agent.tools.memory import create_memory_tools
from src.agent.tools.subagent import create_subagent_tools

logger = logging.getLogger(__name__)


def build_agent(
    config: AgentConfig,
    project_id: str,
    session_id: str | None = None,
    trigger: TriggerType = TriggerType.CLI,
    trigger_source: str | None = None,
) -> Orchestrator:
    """Build a fully-wired agent with all tools.

    Args:
        config: Agent configuration.
        project_id: Auxia project ID.
        session_id: Optional session ID for resumption.
        trigger: How the agent was triggered.
        trigger_source: Trigger-specific source (user, channel, job).

    Returns:
        Configured Orchestrator ready to run.
    """
    # Create session key
    session_key = create_session_key(
        trigger=trigger,
        project_id=project_id,
        source=trigger_source,
        session_id=session_id,
    )
    logger.info(f"Session: {session_key.key}")

    # Create context engine with project isolation
    context = ContextEngine(
        config,
        session_id=session_key.session_id,
        project_id=project_id,
    )

    # Try to build dynamic project context from Auxia Console
    project_context = ""
    if config.auxia.enabled and project_id:
        try:
            from src.agent.tools.auxia import build_project_context
            project_context = build_project_context(config.auxia, project_id)
            logger.info("Loaded project context from Auxia Console")
        except Exception as e:
            logger.warning(f"Could not load project context: {e}")

    orchestrator = Orchestrator(
        config,
        context=context,
        project_context=project_context,
    )

    # Wire up domain tools
    orchestrator.add_tools(create_bigquery_tools(config.bigquery))
    orchestrator.add_tools(create_gcs_tools(config.gcs))
    orchestrator.add_tools(create_memory_tools(context))
    orchestrator.add_tools(create_subagent_tools(config))

    # Wire up Auxia Console tools (read-only)
    if config.auxia.enabled and project_id:
        try:
            from src.agent.tools.auxia import create_auxia_tools
            orchestrator.add_tools(create_auxia_tools(config.auxia, project_id))
            logger.info("Auxia Console tools registered")
        except Exception as e:
            logger.warning(f"Could not load Auxia tools: {e}")

    return orchestrator


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auxia Marketing Agent — campaign analysis and automation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("prompt", nargs="?", help="Task or question for the agent")
    parser.add_argument(
        "--project",
        metavar="PROJECT_ID",
        help="Auxia project ID (required for most operations)",
    )
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

    # Determine project ID: CLI flag > config > empty
    project_id = args.project or config.project_id or ""

    # Build agent
    agent = build_agent(
        config,
        project_id=project_id,
        session_id=args.resume,
    )

    # Resume session if requested
    if args.resume:
        if agent.context.load_session_from_gcs(args.resume):
            logger.info(f"Resumed session {args.resume}")
        else:
            logger.warning(f"Could not restore session {args.resume}, starting fresh")

    # Dry run: show config and exit
    if args.dry_run:
        print("=== Agent Configuration ===")
        print(f"Project ID: {project_id or '(not set)'}")
        print(f"Orchestrator model: {config.models.orchestrator}")
        print(f"Subagent model: {config.models.subagent}")
        print(f"Classifier model: {config.models.classifier}")
        print(f"Max turns: {config.agent.max_turns}")
        print(f"BigQuery project: {config.bigquery.project}")
        print(f"GCS bucket: {config.gcs.bucket}")
        print(f"Auxia Console: {'enabled' if config.auxia.enabled else 'disabled'}")
        print(f"\n=== Registered Tools ({len(agent.registry.list_tools())}) ===")
        for tool in agent.registry.list_tools():
            print(f"  - {tool.name}: {tool.description[:80]}")
        print("\n=== Session ===")
        print(f"Session ID: {agent.context.session_id}")
        print(f"Project ID: {agent.context.project_id}")
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
    print(f"\n--- Agent Session {agent.context.session_id} (project: {project_id or 'default'}) ---\n")
    result = agent.run(prompt)
    print(result)
    print(f"\n--- Session {agent.context.session_id} complete ---")


if __name__ == "__main__":
    main()
