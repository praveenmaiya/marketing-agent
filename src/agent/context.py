"""Context engine with hybrid memory (hot/warm/cold layers).

Architecture:
    Hot  (in-memory)  — current plan, recent tool results, active context  (~ms)
    Warm (GCS JSON)   — session history, scratchpad, intermediate results  (~100ms)
    Cold (BigQuery)   — historical analyses, cross-session learnings       (~1-5s)

Key insight from OpenClaw: don't just truncate old context — promote important
information to durable storage before compacting.
"""

from __future__ import annotations

import json
import logging
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.agent.config import AgentConfig

logger = logging.getLogger(__name__)


class ContextEngine:
    """Manages the agent's memory across hot, warm, and cold layers."""

    def __init__(self, config: AgentConfig, session_id: str | None = None) -> None:
        self.config = config
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self.created_at = datetime.now(UTC).isoformat()

        # Hot layer: in-memory state for the current session
        self._messages: list[dict[str, Any]] = []
        self._plan: dict[str, Any] | None = None
        self._scratchpad: dict[str, str] = {}
        self._memories: list[dict[str, Any]] = []  # In-memory cache of warm memories
        self._turn_count = 0

    # ── Hot Layer (in-memory, every turn) ─────────────────────────

    def add_message(self, role: str, content: Any) -> None:
        """Add a message to the conversation history."""
        self._messages.append({"role": role, "content": content})
        self._turn_count += 1

    def get_messages(self) -> list[dict[str, Any]]:
        """Get the current conversation history."""
        return list(self._messages)

    def set_plan(self, plan: dict[str, Any]) -> None:
        """Set the current plan."""
        self._plan = plan

    def get_plan(self) -> dict[str, Any] | None:
        """Get the current plan."""
        return self._plan

    def set_scratchpad(self, key: str, value: str) -> None:
        """Store a value in the session scratchpad."""
        self._scratchpad[key] = value

    def get_scratchpad(self, key: str) -> str | None:
        """Get a value from the session scratchpad."""
        return self._scratchpad.get(key)

    @property
    def turn_count(self) -> int:
        return self._turn_count

    # ── Warm Layer (GCS, on-demand) ───────────────────────────────

    def save_session_to_gcs(self) -> str | None:
        """Persist current session state to GCS."""
        try:
            from src.gcs_utils import upload_blob

            state = {
                "session_id": self.session_id,
                "created_at": self.created_at,
                "saved_at": datetime.now(UTC).isoformat(),
                "turn_count": self._turn_count,
                "plan": self._plan,
                "scratchpad": self._scratchpad,
                "memories": self._memories,
                "message_count": len(self._messages),
                "messages": self._messages[-20:],  # Keep last 20 messages
            }

            blob_name = f"{self.config.gcs.session_prefix}{self.session_id}.json"
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                json.dump(state, f, indent=2, default=str)
                tmp_path = f.name

            try:
                uri = upload_blob(tmp_path, self.config.gcs.bucket, blob_name)
            finally:
                Path(tmp_path).unlink(missing_ok=True)
            logger.info(f"Session saved to {uri}")
            return uri
        except Exception as e:
            logger.warning(f"Failed to save session to GCS: {e}")
            return None

    def load_session_from_gcs(self, session_id: str) -> bool:
        """Restore session state from GCS (for pod restart recovery)."""
        try:
            from src.gcs_utils import download_blob

            blob_name = f"{self.config.gcs.session_prefix}{session_id}.json"
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                tmp_path = f.name

            try:
                download_blob(self.config.gcs.bucket, blob_name, tmp_path)
                state = json.loads(Path(tmp_path).read_text())
            finally:
                Path(tmp_path).unlink(missing_ok=True)

            self.session_id = state["session_id"]
            self.created_at = state["created_at"]
            self._turn_count = state.get("turn_count", 0)
            self._plan = state.get("plan")
            self._scratchpad = state.get("scratchpad", {})
            self._memories = state.get("memories", [])
            self._messages = state.get("messages", [])

            logger.info(f"Session restored from GCS: {session_id}")
            return True
        except Exception as e:
            logger.warning(f"Failed to load session from GCS: {e}")
            return False

    def save_memory(self, content: str, category: str = "general") -> None:
        """Save a durable fact to the warm memory layer."""
        memory = {
            "id": str(uuid.uuid4())[:8],
            "content": content,
            "category": category,
            "session_id": self.session_id,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self._memories.append(memory)

        # Also persist to GCS
        try:
            from src.gcs_utils import upload_blob

            blob_name = f"{self.config.gcs.memory_prefix}{memory['id']}.json"
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                json.dump(memory, f, indent=2)
                tmp_path = f.name

            try:
                upload_blob(tmp_path, self.config.gcs.bucket, blob_name)
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Failed to persist memory to GCS: {e}")

    def search_memories(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search memories by keyword matching.

        For Phase 2, this will be upgraded to vector similarity search.
        Current implementation: simple substring matching across all memories.
        """
        query_lower = query.lower()
        matches = []
        for mem in self._memories:
            if query_lower in mem["content"].lower():
                matches.append(mem)

        # Also search GCS-persisted memories
        try:
            from src.gcs_utils import download_blob, list_blobs

            blobs = list_blobs(
                self.config.gcs.bucket,
                prefix=self.config.gcs.memory_prefix,
            )
            for blob_name in blobs[:100]:  # Cap scan at 100
                if not blob_name.endswith(".json"):
                    continue
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".json", delete=False
                    ) as f:
                        tmp_path = f.name
                    download_blob(self.config.gcs.bucket, blob_name, tmp_path)
                    mem = json.loads(Path(tmp_path).read_text())
                    Path(tmp_path).unlink(missing_ok=True)
                    if query_lower in mem.get("content", "").lower():
                        # Avoid duplicates from hot layer
                        if not any(m["id"] == mem["id"] for m in matches):
                            matches.append(mem)
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"GCS memory search failed: {e}")

        return matches[:limit]

    def get_session_summaries(self, limit: int = 5) -> list[dict[str, Any]]:
        """Get summaries of past sessions from GCS."""
        summaries = []
        try:
            from src.gcs_utils import download_blob, list_blobs

            blobs = list_blobs(
                self.config.gcs.bucket,
                prefix=self.config.gcs.session_prefix,
            )
            for blob_name in sorted(blobs, reverse=True)[:limit]:
                if not blob_name.endswith(".json"):
                    continue
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".json", delete=False
                    ) as f:
                        tmp_path = f.name
                    download_blob(self.config.gcs.bucket, blob_name, tmp_path)
                    state = json.loads(Path(tmp_path).read_text())
                    Path(tmp_path).unlink(missing_ok=True)
                    summaries.append({
                        "session_id": state.get("session_id"),
                        "created_at": state.get("created_at"),
                        "turn_count": state.get("turn_count", 0),
                        "message_count": state.get("message_count", 0),
                        "has_plan": state.get("plan") is not None,
                    })
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"Session listing failed: {e}")
        return summaries

    # ── Compaction ────────────────────────────────────────────────

    def compact(self, summarizer_fn: Any = None) -> str | None:
        """Compact old messages by summarizing and promoting durable facts.

        This is the key insight from OpenClaw: don't just truncate.
        Promote important information to warm storage before compacting.

        Args:
            summarizer_fn: Callable that takes a list of messages and returns
                          a summary string. If None, uses simple truncation.

        Returns:
            Summary of compacted messages, or None if no compaction needed.
        """
        if len(self._messages) < 10:
            return None

        # Keep the last 6 messages, compact the rest
        old_messages = self._messages[:-6]
        recent_messages = self._messages[-6:]

        if summarizer_fn:
            summary = summarizer_fn(old_messages)
        else:
            # Simple fallback: keep assistant text content only
            texts = []
            for msg in old_messages:
                if msg["role"] == "assistant" and isinstance(msg["content"], str):
                    texts.append(msg["content"][:200])
            summary = "Previous conversation summary:\n" + "\n".join(texts[-5:])

        # Ensure alternating roles after compaction
        # The summary pair is user→assistant, so recent must start with user
        if recent_messages and recent_messages[0]["role"] == "assistant":
            recent_messages.insert(0, {"role": "user", "content": "[Continuing conversation]"})

        # Replace message history with summary + recent
        self._messages = [
            {"role": "user", "content": f"[Session context summary]\n{summary}"},
            {"role": "assistant", "content": "Understood. I have the context from our previous discussion."},
            *recent_messages,
        ]

        logger.info(
            f"Compacted {len(old_messages)} messages into summary "
            f"({len(summary)} chars), kept {len(recent_messages)} recent"
        )
        return summary

    # ── Serialization ─────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialize full state for checkpointing."""
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "turn_count": self._turn_count,
            "plan": self._plan,
            "scratchpad": self._scratchpad,
            "memories": self._memories,
            "messages": self._messages,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], config: AgentConfig) -> ContextEngine:
        """Restore from serialized state."""
        engine = cls(config, session_id=data["session_id"])
        engine.created_at = data["created_at"]
        engine._turn_count = data.get("turn_count", 0)
        engine._plan = data.get("plan")
        engine._scratchpad = data.get("scratchpad", {})
        engine._memories = data.get("memories", [])
        engine._messages = data.get("messages", [])
        return engine
