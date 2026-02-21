"""Memory tools for the marketing agent.

Provides semantic search and storage for cross-session learnings.
Backed by the context engine's warm/cold storage layers.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from src.agent.tools import ToolDef

if TYPE_CHECKING:
    from src.agent.context import ContextEngine

logger = logging.getLogger(__name__)


def create_memory_tools(context_engine: ContextEngine) -> list[ToolDef]:
    """Create memory tools that use the context engine for storage."""

    def search_memory(params: dict) -> str:
        """Search past memories and session history."""
        query = params["query"]
        limit = params.get("limit", 10)
        results = context_engine.search_memories(query, limit=limit)
        if not results:
            return "No matching memories found."
        return json.dumps(results, indent=2, default=str)

    def save_memory(params: dict) -> str:
        """Save a durable fact or learning to memory."""
        content = params["content"]
        category = params.get("category", "general")
        context_engine.save_memory(content, category=category)
        return f"Memory saved under category '{category}'."

    def get_session_history(params: dict) -> str:
        """Get summary of past agent sessions."""
        limit = params.get("limit", 5)
        sessions = context_engine.get_session_summaries(limit=limit)
        if not sessions:
            return "No past sessions found."
        return json.dumps(sessions, indent=2, default=str)

    return [
        ToolDef(
            name="search_memory",
            description=(
                "Search past analyses, learnings, and session history. "
                "Use this to recall what was done before and avoid duplicate work."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Semantic search query",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 10)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
            handler=search_memory,
        ),
        ToolDef(
            name="save_memory",
            description=(
                "Save an important finding, decision, or learning for future sessions. "
                "Use this for durable facts that should survive session compaction."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The fact or learning to remember",
                    },
                    "category": {
                        "type": "string",
                        "description": "Category: 'analysis', 'decision', 'finding', 'general'",
                        "default": "general",
                    },
                },
                "required": ["content"],
            },
            handler=save_memory,
        ),
        ToolDef(
            name="get_session_history",
            description="Get summaries of past agent sessions to understand recent activity.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max sessions to return (default 5)",
                        "default": 5,
                    },
                },
            },
            handler=get_session_history,
        ),
    ]
