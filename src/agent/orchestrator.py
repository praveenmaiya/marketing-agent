"""Orchestrator: the main ReAct loop with plan-as-artifact management.

Implements the core agent loop:
1. Assemble system prompt with domain context + plan state
2. Send messages to Claude API with available tools
3. Execute tool calls, inject results
4. Check if plan needs updating
5. Checkpoint state periodically to GCS
6. Compact context when approaching token limits

Design choices:
- Plan is stored as a tool-call output, so the model can reference and revise it
- Each plan step tracks status (pending/in_progress/completed/failed) and result
- Subagent isolation: child LLMs get scoped context, not the full conversation
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import anthropic

from src.agent.config import AgentConfig
from src.agent.context import ContextEngine
from src.agent.tools import ToolDef, ToolRegistry

logger = logging.getLogger(__name__)


class PlanManager:
    """Manages the plan-as-artifact lifecycle."""

    def __init__(self, context: ContextEngine, max_steps: int = 20) -> None:
        self.context = context
        self.max_steps = max_steps

    def make_plan(self, objective: str, steps: list[str]) -> dict[str, Any]:
        """Create a new plan."""
        plan = {
            "objective": objective,
            "status": "active",
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
            "steps": [
                {
                    "id": i + 1,
                    "description": step,
                    "status": "pending",
                    "result": None,
                }
                for i, step in enumerate(steps[: self.max_steps])
            ],
        }
        self.context.set_plan(plan)
        return plan

    def get_current_step(self) -> dict[str, Any] | None:
        """Get the next pending step."""
        plan = self.context.get_plan()
        if not plan:
            return None
        for step in plan["steps"]:
            if step["status"] == "pending":
                return step
        return None

    def update_step(self, step_id: int, status: str, result: str | None = None) -> None:
        """Update a plan step's status and result."""
        plan = self.context.get_plan()
        if not plan:
            return
        for step in plan["steps"]:
            if step["id"] == step_id:
                step["status"] = status
                if result:
                    step["result"] = result
                break
        plan["updated_at"] = datetime.now(UTC).isoformat()
        self.context.set_plan(plan)

    def revise_plan(self, new_steps: list[str]) -> dict[str, Any] | None:
        """Revise remaining steps (keep completed ones)."""
        plan = self.context.get_plan()
        if not plan:
            return None

        completed = [s for s in plan["steps"] if s["status"] == "completed"]
        next_id = len(completed) + 1

        new_step_dicts = [
            {
                "id": next_id + i,
                "description": step,
                "status": "pending",
                "result": None,
            }
            for i, step in enumerate(new_steps[: self.max_steps - len(completed)])
        ]

        plan["steps"] = completed + new_step_dicts
        plan["updated_at"] = datetime.now(UTC).isoformat()
        self.context.set_plan(plan)
        return plan

    def is_complete(self) -> bool:
        """Check if all plan steps are done."""
        plan = self.context.get_plan()
        if not plan:
            return True
        return all(s["status"] in ("completed", "failed") for s in plan["steps"])

    def format_for_prompt(self) -> str:
        """Format plan as text for the system prompt."""
        plan = self.context.get_plan()
        if not plan:
            return "No active plan."

        lines = [f"Objective: {plan['objective']}", ""]
        for step in plan["steps"]:
            icon = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]", "failed": "[!]"}
            lines.append(f"  {icon.get(step['status'], '[ ]')} Step {step['id']}: {step['description']}")
            if step["result"]:
                lines.append(f"      Result: {step['result'][:200]}")
        return "\n".join(lines)


def _build_plan_tools(plan_manager: PlanManager) -> list[ToolDef]:
    """Create tools for plan management."""

    def make_plan(params: dict) -> str:
        objective = params["objective"]
        steps = params["steps"]
        plan = plan_manager.make_plan(objective, steps)
        return json.dumps(plan, indent=2, default=str)

    def update_plan_step(params: dict) -> str:
        step_id = params["step_id"]
        status = params["status"]
        result = params.get("result")
        plan_manager.update_step(step_id, status, result)
        return f"Step {step_id} updated to {status}."

    def revise_plan(params: dict) -> str:
        new_steps = params["new_steps"]
        plan = plan_manager.revise_plan(new_steps)
        if plan:
            return json.dumps(plan, indent=2, default=str)
        return "No active plan to revise."

    return [
        ToolDef(
            name="make_plan",
            description=(
                "Create a structured plan for the current task. "
                "Break the objective into concrete, executable steps."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "objective": {
                        "type": "string",
                        "description": "The overall goal",
                    },
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered list of steps to achieve the objective",
                    },
                },
                "required": ["objective", "steps"],
            },
            handler=make_plan,
        ),
        ToolDef(
            name="update_plan_step",
            description="Update the status and result of a plan step.",
            input_schema={
                "type": "object",
                "properties": {
                    "step_id": {"type": "integer", "description": "Step number"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "failed"],
                    },
                    "result": {
                        "type": "string",
                        "description": "Summary of what was accomplished or why it failed",
                    },
                },
                "required": ["step_id", "status"],
            },
            handler=update_plan_step,
        ),
        ToolDef(
            name="revise_plan",
            description=(
                "Revise the remaining plan steps based on new information. "
                "Completed steps are preserved; pending steps are replaced."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "new_steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New list of remaining steps",
                    },
                },
                "required": ["new_steps"],
            },
            handler=revise_plan,
        ),
    ]


class Orchestrator:
    """Main agent orchestrator implementing the ReAct loop.

    Usage:
        config = AgentConfig.from_yaml("configs/agent.yaml")
        orchestrator = Orchestrator(config)
        result = orchestrator.run("Analyze Q4 campaign performance")
    """

    def __init__(
        self,
        config: AgentConfig,
        context: ContextEngine | None = None,
        tools: list[ToolDef] | None = None,
    ) -> None:
        self.config = config
        self.client = anthropic.Anthropic()
        self.context = context or ContextEngine(config)
        self.plan_manager = PlanManager(self.context, max_steps=config.agent.max_plan_steps)

        # Build tool registry
        self.registry = ToolRegistry()

        # Register plan management tools
        for tool in _build_plan_tools(self.plan_manager):
            self.registry.register(tool)

        # Register any externally provided tools
        if tools:
            for tool in tools:
                self.registry.register(tool)

    def add_tools(self, tools: list[ToolDef]) -> None:
        """Add tools to the registry after construction."""
        for tool in tools:
            self.registry.register(tool)

    def run(self, prompt: str) -> str:
        """Execute the main ReAct loop.

        Args:
            prompt: The user's task or question.

        Returns:
            The agent's final text response.
        """
        system = self._build_system_prompt()
        self.context.add_message("user", prompt)

        logger.info(f"Agent run started: {prompt[:100]}...")
        start_time = time.time()

        for turn in range(self.config.agent.max_turns):
            messages = self.context.get_messages()
            tool_schemas = self.registry.to_api_schemas()

            logger.debug(f"Turn {turn + 1}: {len(messages)} messages, {len(tool_schemas)} tools")

            response = self._call_api_with_retry(system, tool_schemas, messages)

            # Process the response
            assistant_content = response.content
            self.context.add_message("assistant", assistant_content)

            # Check stop reason
            if response.stop_reason == "end_turn":
                final_text = self._extract_text(assistant_content)
                elapsed = time.time() - start_time
                logger.info(
                    f"Agent run completed in {elapsed:.1f}s, {turn + 1} turns, "
                    f"{len(final_text)} chars"
                )
                self._maybe_checkpoint(force=True)
                return final_text

            if response.stop_reason == "tool_use":
                tool_results = self._execute_tool_calls(assistant_content)
                self.context.add_message("user", tool_results)

                # Periodic checkpoint
                self._maybe_checkpoint()

                # Check if compaction is needed (rough token estimate)
                self._maybe_compact()

        # Hit max turns — find the last assistant response
        logger.warning(f"Agent hit max turns ({self.config.agent.max_turns})")
        self._maybe_checkpoint(force=True)
        for msg in reversed(self.context.get_messages()):
            if msg.get("role") == "assistant":
                return self._extract_text(msg.get("content", []))
        return "Agent reached max turns without producing a final response."

    def _call_api_with_retry(
        self, system: str, tools: list[dict], messages: list[dict], max_retries: int = 3
    ) -> Any:
        """Call the Anthropic API with retry on transient errors."""
        for attempt in range(max_retries):
            try:
                return self.client.messages.create(
                    model=self.config.models.orchestrator,
                    max_tokens=self.config.tokens.max_output,
                    system=system,
                    tools=tools,
                    messages=messages,
                )
            except anthropic.RateLimitError:
                wait = 2**attempt
                logger.warning(f"Rate limited, retrying in {wait}s (attempt {attempt + 1})")
                time.sleep(wait)
            except anthropic.APIStatusError as e:
                logger.error(f"API error: {e}")
                self._maybe_checkpoint(force=True)
                raise
        self._maybe_checkpoint(force=True)
        raise RuntimeError("Max API retries exceeded")

    def _build_system_prompt(self) -> str:
        """Assemble the system prompt with domain context and plan state."""
        domain = self.config.domain
        plan_text = self.plan_manager.format_for_prompt()

        return f"""You are a marketing analyst agent for {domain.company}.
Domain: {domain.domain}

{domain.description}

## Available Data
Key BigQuery tables:
{chr(10).join(f'- {t}' for t in domain.key_tables)}

Key metrics:
{chr(10).join(f'- {m}' for m in domain.key_metrics)}

## Current Plan
{plan_text}

## Instructions
1. For complex tasks, use make_plan to create a structured plan first.
2. Execute steps one at a time, updating status as you go.
3. Use query_bigquery for data analysis. Always validate with dry_run first.
4. Use spawn_subagent for focused sub-analyses that don't need the full context.
5. Use save_memory for important findings that should persist across sessions.
6. If your plan needs adjustment based on results, use revise_plan.
7. Be concise and data-driven in your final response.
"""

    def _execute_tool_calls(self, content: list[Any]) -> list[dict[str, Any]]:
        """Execute all tool calls in the response and return results."""
        results = []
        for block in content:
            if block.type != "tool_use":
                continue

            tool = self.registry.get(block.name)
            if tool is None:
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Unknown tool: {block.name}",
                    "is_error": True,
                })
                continue

            result_text = tool.execute(block.input)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_text,
            })

        return results

    def _extract_text(self, content: Any) -> str:
        """Extract text from response content blocks."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for block in content:
                if hasattr(block, "text"):
                    texts.append(block.text)
                elif isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block["text"])
            return "\n".join(texts)
        return str(content)

    def _maybe_checkpoint(self, force: bool = False) -> None:
        """Checkpoint session state to GCS if interval reached."""
        if force or (self.context.turn_count % self.config.agent.checkpoint_interval == 0):
            self.context.save_session_to_gcs()

    def _maybe_compact(self) -> None:
        """Compact context if it's getting too large."""
        messages = self.context.get_messages()
        # Rough estimate: ~4 chars per token
        estimated_tokens = sum(
            len(json.dumps(m, default=str)) // 4 for m in messages
        )

        if estimated_tokens > self.config.tokens.compaction_threshold:
            logger.info(
                f"Context at ~{estimated_tokens} tokens, "
                f"threshold {self.config.tokens.compaction_threshold} — compacting"
            )

            def summarize(msgs: list[dict]) -> str:
                """Use the LLM to summarize old messages."""
                text = json.dumps(msgs, default=str)[:8000]
                try:
                    response = self.client.messages.create(
                        model=self.config.models.classifier,  # Use Haiku for speed
                        max_tokens=self.config.tokens.summary_target,
                        system="Summarize this conversation concisely. Keep key facts, decisions, and data points.",
                        messages=[{"role": "user", "content": text}],
                    )
                    return response.content[0].text
                except Exception as e:
                    logger.warning(f"Summarization failed: {e}")
                    return None

            self.context.compact(summarizer_fn=summarize)
