"""Memory tools for the marketing agent.

Provides BM25 search and categorized storage for cross-session learnings.
Backed by the context engine's warm (GCS JSONL) + cold (BigQuery) layers.

Memory categories:
    finding         — data-driven observations (e.g., "Email CTR dropped 15%")
    decision        — choices made (e.g., "Switched from weekly to daily push")
    pattern         — recurring trends (e.g., "CTR always dips on Mondays")
    session_summary — auto-generated session recap
    general         — anything else
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from src.agent.tools import ToolDef

if TYPE_CHECKING:
    from src.agent.context import ContextEngine

logger = logging.getLogger(__name__)

MEMORY_CATEGORIES = ["finding", "decision", "pattern", "session_summary", "general"]


def create_memory_tools(context_engine: ContextEngine) -> list[ToolDef]:
    """Create memory tools that use the context engine for storage."""

    def search_memory(params: dict) -> str:
        """Search past memories using BM25 via BigQuery."""
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
        if category not in MEMORY_CATEGORIES:
            return (
                f"Invalid category '{category}'. "
                f"Use one of: {MEMORY_CATEGORIES}"
            )
        memory = context_engine.save_memory(content, category=category)
        return (
            f"Memory saved (id={memory['id']}, category='{category}'). "
            f"This will persist across sessions and be searchable via BM25."
        )

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
                "Search past analyses, learnings, and session history using BM25 full-text search. "
                "Use this BEFORE starting work to check if similar analysis was done before. "
                "Returns memories sorted by recency."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (matched via BigQuery SEARCH / BM25)",
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
                "Save an important finding, decision, or pattern for future sessions. "
                "Memories persist to GCS and BigQuery and survive session compaction. "
                "Use categories: 'finding' for data observations, 'decision' for choices made, "
                "'pattern' for recurring trends, 'general' for everything else."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The fact, finding, or learning to remember",
                    },
                    "category": {
                        "type": "string",
                        "enum": MEMORY_CATEGORIES,
                        "description": "Memory category",
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
