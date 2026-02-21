"""Subagent spawning tools for the marketing agent.

Implements two patterns from the architecture:
1. Scoped subagent: Spawn a child LLM with a focused objective and limited tools
2. Fan-out classification: Spawn N parallel Haiku calls for bulk classification
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import anthropic

from src.agent.config import AgentConfig
from src.agent.tools import ToolDef

logger = logging.getLogger(__name__)


def _call_llm(
    client: anthropic.Anthropic,
    model: str,
    system: str,
    prompt: str,
    max_tokens: int = 4096,
) -> str:
    """Make a single LLM call (no tool use)."""
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def create_subagent_tools(config: AgentConfig) -> list[ToolDef]:
    """Create subagent tools for spawning child LLM calls."""
    client = anthropic.Anthropic()

    allowed_models = {config.models.orchestrator, config.models.subagent, config.models.classifier}

    def spawn_subagent(params: dict) -> str:
        """Spawn a focused subagent with scoped context."""
        objective = params["objective"]
        context = params.get("context", "")
        model = params.get("model", config.models.subagent)

        if model not in allowed_models:
            return f"ERROR: Model '{model}' not allowed. Use one of: {sorted(allowed_models)}"

        system = (
            f"You are a focused analyst for {config.domain.company}. "
            f"Domain: {config.domain.domain}.\n\n"
            "Your single objective is stated below. "
            "Provide a concise, structured answer. No preamble.\n\n"
            f"Context:\n{context}"
        )

        logger.info(f"Spawning subagent ({model}): {objective[:80]}...")
        result = _call_llm(client, model, system, objective)
        logger.info(f"Subagent completed ({len(result)} chars)")
        return result

    def fan_out_classify(params: dict) -> str:
        """Classify multiple items in parallel using Haiku."""
        items = params["items"]
        classification_prompt = params["prompt"]
        categories = params["categories"]
        max_parallel = min(
            params.get("max_parallel", config.agent.fan_out_max_parallel),
            config.agent.fan_out_max_parallel,
        )

        model = config.models.classifier
        system = (
            "You are a classifier. Respond with ONLY a JSON object: "
            '{"category": "<one of the categories>", "confidence": <0.0-1.0>}\n\n'
            f"Categories: {json.dumps(categories)}"
        )

        results: list[dict[str, Any]] = []

        def classify_one(item: str) -> dict:
            prompt = f"{classification_prompt}\n\nItem: {item}"
            try:
                raw = _call_llm(client, model, system, prompt, max_tokens=256)
                parsed = json.loads(raw)
                return {"item": item, **parsed}
            except (json.JSONDecodeError, KeyError):
                return {"item": item, "category": "unknown", "confidence": 0.0, "raw": raw}

        logger.info(f"Fan-out: classifying {len(items)} items with {model}")
        with ThreadPoolExecutor(max_workers=max_parallel) as executor:
            futures = {executor.submit(classify_one, item): item for item in items}
            for future in as_completed(futures):
                results.append(future.result())

        return json.dumps(results, indent=2)

    tools = [
        ToolDef(
            name="spawn_subagent",
            description=(
                "Spawn a focused subagent to analyze a specific question. "
                "The subagent gets scoped context and a single objective. "
                "Use this to delegate analysis without polluting the main context."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "objective": {
                        "type": "string",
                        "description": "The specific question or task for the subagent",
                    },
                    "context": {
                        "type": "string",
                        "description": "Relevant context to provide (keep focused)",
                        "default": "",
                    },
                    "model": {
                        "type": "string",
                        "description": "Model to use (default: subagent model from config)",
                    },
                },
                "required": ["objective"],
            },
            handler=spawn_subagent,
        ),
    ]

    if config.agent.enable_fan_out:
        tools.append(
            ToolDef(
                name="fan_out_classify",
                description=(
                    "Classify many items in parallel using fast Haiku model. "
                    "Good for bulk categorization of campaigns, treatments, or content."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of items to classify",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "Classification instruction for each item",
                        },
                        "categories": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of valid category names",
                        },
                        "max_parallel": {
                            "type": "integer",
                            "description": "Max concurrent calls (default from config)",
                        },
                    },
                    "required": ["items", "prompt", "categories"],
                },
                handler=fan_out_classify,
            )
        )

    return tools
