"""Tool system for the marketing agent.

Each tool has:
- name: identifier for the Anthropic API
- description: what it does (shown to the model)
- input_schema: JSON Schema for parameters
- handler: callable that executes the tool and returns a string result
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ToolDef:
    """A tool available to the agent."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], str]

    def to_api_schema(self) -> dict[str, Any]:
        """Convert to Anthropic API tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def execute(self, params: dict[str, Any]) -> str:
        """Execute the tool with given parameters."""
        logger.info(f"Executing tool: {self.name}")
        try:
            result = self.handler(params)
            logger.info(f"Tool {self.name} completed ({len(result)} chars)")
            return result
        except Exception as e:
            error_msg = f"Tool {self.name} failed: {type(e).__name__}: {e}"
            logger.error(error_msg)
            return error_msg


class ToolRegistry:
    """Registry of available tools."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDef | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[ToolDef]:
        """List all registered tools."""
        return list(self._tools.values())

    def to_api_schemas(self) -> list[dict[str, Any]]:
        """Get all tool schemas for the Anthropic API."""
        return [tool.to_api_schema() for tool in self._tools.values()]
