"""Context engine with hybrid memory (hot/warm/cold layers).

Architecture:
    Hot  (in-memory)  — current plan, recent tool results, active context  (~ms)
    Warm (GCS JSONL)  — append-only memory log per project                (~100ms)
    Cold (BigQuery)   — BM25 search across all memories                   (~1-5s)

Key insight from OpenClaw: don't just truncate old context — promote important
information to durable storage before compacting.

Memory flow:
    save_memory() → append to in-memory list + GCS JSONL + BigQuery row
    search_memories() → BM25 via BigQuery SEARCH function
    compact() → flush important findings to memory, then summarize + truncate
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.agent.config import AgentConfig

logger = logging.getLogger(__name__)

# BigQuery table for memories — created lazily
_MEMORIES_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.memories` (
  id STRING NOT NULL,
  project_id STRING NOT NULL,
  category STRING NOT NULL,
  content STRING NOT NULL,
  session_id STRING,
  created_at TIMESTAMP NOT NULL,
  superseded_at TIMESTAMP
)
"""


class ContextEngine:
    """Manages the agent's memory across hot, warm, and cold layers."""

    def __init__(
        self,
        config: AgentConfig,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> None:
        self.config = config
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self.project_id = project_id or "default"
        self.created_at = datetime.now(UTC).isoformat()

        # Hot layer: in-memory state for the current session
        self._messages: list[dict[str, Any]] = []
        self._plan: dict[str, Any] | None = None
        self._scratchpad: dict[str, str] = {}
        self._memories: list[dict[str, Any]] = []  # In-memory cache
        self._turn_count = 0

        # BigQuery client cache (lazy-initialized)
        self._cached_bq_client: Any | None = None
        self._table_verified: bool = False

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
                "project_id": self.project_id,
                "created_at": self.created_at,
                "saved_at": datetime.now(UTC).isoformat(),
                "turn_count": self._turn_count,
                "plan": self._plan,
                "scratchpad": self._scratchpad,
                "memories": self._memories,
                "message_count": len(self._messages),
                "messages": self._messages[-20:],  # Keep last 20 messages
            }

            blob_name = (
                f"agent/memory/{self.project_id}/sessions/{self.session_id}.json"
            )
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

            blob_name = (
                f"agent/memory/{self.project_id}/sessions/{session_id}.json"
            )
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                tmp_path = f.name

            try:
                download_blob(self.config.gcs.bucket, blob_name, tmp_path)
                state = json.loads(Path(tmp_path).read_text())
            finally:
                Path(tmp_path).unlink(missing_ok=True)

            self.session_id = state["session_id"]
            self.project_id = state.get("project_id", self.project_id)
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

    # ── BigQuery Client (cached) ─────────────────────────────────

    @property
    def _bq_client(self) -> Any:
        """Lazily create and cache the BigQuery client."""
        if self._cached_bq_client is None:
            from google.cloud import bigquery

            self._cached_bq_client = bigquery.Client(
                project=self.config.bigquery.project,
                location=self.config.bigquery.location,
            )
        return self._cached_bq_client

    # ── Memory Persistence (JSONL + BigQuery) ─────────────────────

    def save_memory(
        self,
        content: str,
        category: str = "general",
    ) -> dict[str, Any]:
        """Save a durable fact to warm + cold storage.

        Writes to:
        1. In-memory list (hot)
        2. GCS JSONL append log (warm)
        3. BigQuery memories table (cold, for BM25 search)
        """
        memory = {
            "id": str(uuid.uuid4())[:8],
            "project_id": self.project_id,
            "content": content,
            "category": category,
            "session_id": self.session_id,
            "created_at": datetime.now(UTC).isoformat(),
            "superseded_at": None,
        }
        self._memories.append(memory)

        # Warm: append to GCS JSONL
        self._append_memory_to_gcs(memory)

        # Cold: insert into BigQuery
        self._insert_memory_to_bigquery(memory)

        return memory

    def _append_memory_to_gcs(self, memory: dict[str, Any]) -> None:
        """Append a memory record to a per-session JSONL file in GCS.

        Uses per-session files to avoid read-modify-write race conditions
        when multiple sessions run concurrently for the same project.
        Layout: agent/memory/{project_id}/memories_{session_id}.jsonl
        """
        try:
            from src.gcs_utils import download_blob, upload_blob

            blob_name = (
                f"agent/memory/{self.project_id}/"
                f"memories_{self.session_id}.jsonl"
            )
            line = json.dumps(memory, default=str) + "\n"

            # Download existing session file (if any) and append
            existing = ""
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".jsonl", delete=False
                ) as f:
                    tmp_path = f.name
                download_blob(self.config.gcs.bucket, blob_name, tmp_path)
                existing = Path(tmp_path).read_text()
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass  # File doesn't exist yet (first memory this session)

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".jsonl", delete=False
            ) as f:
                f.write(existing + line)
                tmp_path = f.name

            try:
                upload_blob(tmp_path, self.config.gcs.bucket, blob_name)
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Failed to append memory to GCS: {e}")

    def _insert_memory_to_bigquery(self, memory: dict[str, Any]) -> None:
        """Insert a memory row into the BigQuery memories table."""
        try:
            table_id = self._memories_table_id()
            self._ensure_memories_table(table_id)

            rows = [{
                "id": memory["id"],
                "project_id": memory["project_id"],
                "category": memory["category"],
                "content": memory["content"],
                "session_id": memory["session_id"],
                "created_at": memory["created_at"],
                "superseded_at": None,
            }]
            errors = self._bq_client.insert_rows_json(table_id, rows)
            if errors:
                logger.warning(f"BigQuery insert errors: {errors}")
        except Exception as e:
            logger.warning(f"Failed to insert memory to BigQuery: {e}")

    def _memories_table_id(self) -> str:
        """Build and validate the memories table ID from config."""
        table_id = (
            f"{self.config.bigquery.project}."
            f"{self.config.bigquery.dataset}.memories"
        )
        # Validate table_id contains only safe characters (project.dataset.table)
        if not re.match(r'^[a-zA-Z0-9_.-]+\.[a-zA-Z0-9_.-]+\.[a-zA-Z0-9_]+$', table_id):
            raise ValueError(f"Invalid table ID format: {table_id}")
        return table_id

    def _ensure_memories_table(self, table_id: str) -> None:
        """Create the memories table if it doesn't exist (cached check).

        NOTE: This is an intentional exception to the SELECT-only SQL guardrail.
        The DDL here is controlled by config values (not user input) and only
        runs once per session to bootstrap the agent's memory infrastructure.
        """
        if self._table_verified:
            return
        try:
            self._bq_client.get_table(table_id)
        except Exception:
            sql = _MEMORIES_TABLE_SCHEMA.format(
                project=self.config.bigquery.project,
                dataset=self.config.bigquery.dataset,
            )
            self._bq_client.query(sql).result()
            logger.info(f"Created memories table: {table_id}")
        self._table_verified = True

    # ── Memory Search (BM25 via BigQuery SEARCH) ──────────────────

    def search_memories(
        self,
        query: str,
        limit: int = 10,
        include_hot: bool = True,
    ) -> list[dict[str, Any]]:
        """Search memories using BigQuery BM25 SEARCH, with hot layer fallback.

        Args:
            query: Search query string.
            limit: Max results to return.
            include_hot: Also search in-memory (hot) cache.

        Returns:
            List of matching memory dicts, most recent first.
        """
        results = []

        # Cold layer: BM25 search via BigQuery
        bq_results = self._search_memories_bigquery(query, limit)
        results.extend(bq_results)

        # Hot layer: substring fallback for memories not yet in BigQuery
        if include_hot:
            bq_ids = {r["id"] for r in results}
            query_lower = query.lower()
            for mem in self._memories:
                if mem["id"] not in bq_ids and query_lower in mem["content"].lower():
                    results.append(mem)

        # Sort by created_at descending, cap at limit
        results.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        return results[:limit]

    def _search_memories_bigquery(
        self,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search memories via BigQuery SEARCH function (BM25)."""
        try:
            from google.cloud import bigquery as bq

            table_id = self._memories_table_id()

            sql = f"""
                SELECT id, project_id, category, content, session_id,
                       CAST(created_at AS STRING) as created_at
                FROM `{table_id}`
                WHERE project_id = @project_id
                  AND superseded_at IS NULL
                  AND SEARCH(content, @query)
                ORDER BY created_at DESC
                LIMIT @limit
            """
            job_config = bq.QueryJobConfig(
                query_parameters=[
                    bq.ScalarQueryParameter("project_id", "STRING", self.project_id),
                    bq.ScalarQueryParameter("query", "STRING", query),
                    bq.ScalarQueryParameter("limit", "INT64", limit),
                ]
            )
            rows = self._bq_client.query(sql, job_config=job_config).result()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.debug(f"BigQuery memory search failed: {e}")
            return []

    def get_session_summaries(self, limit: int = 5) -> list[dict[str, Any]]:
        """Get summaries of past sessions from GCS."""
        summaries = []
        try:
            from src.gcs_utils import download_blob, list_blobs

            prefix = f"agent/memory/{self.project_id}/sessions/"
            blobs = list_blobs(self.config.gcs.bucket, prefix=prefix)
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
                        "project_id": state.get("project_id"),
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

    # ── Pre-Compaction Memory Flush ───────────────────────────────

    def flush_before_compaction(self, flush_fn: Any = None) -> list[dict[str, Any]]:
        """Flush important findings to memory before compaction.

        From OpenClaw: before compacting, give the model a chance to
        save any important information from the conversation to durable memory.

        Args:
            flush_fn: Callable that takes conversation messages and returns
                     a list of memory dicts [{content, category}] to save.
                     If None, uses simple heuristic extraction.

        Returns:
            List of memories saved during the flush.
        """
        saved = []

        if flush_fn:
            # Let the LLM decide what to save
            memories_to_save = flush_fn(self._messages)
            if memories_to_save:
                for mem in memories_to_save:
                    saved_mem = self.save_memory(
                        content=mem["content"],
                        category=mem.get("category", "session_summary"),
                    )
                    saved.append(saved_mem)
        else:
            # Heuristic fallback: save a session summary
            assistant_texts = []
            for msg in self._messages:
                if msg["role"] == "assistant" and isinstance(msg["content"], str):
                    assistant_texts.append(msg["content"][:300])
            if assistant_texts:
                summary = "Session summary: " + " | ".join(assistant_texts[-5:])
                saved_mem = self.save_memory(
                    content=summary[:2000],
                    category="session_summary",
                )
                saved.append(saved_mem)

        if saved:
            logger.info(f"Pre-compaction flush: saved {len(saved)} memories")
        return saved

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
        # The summary pair is user->assistant, so recent must start with user
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
            "project_id": self.project_id,
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
        engine = cls(
            config,
            session_id=data["session_id"],
            project_id=data.get("project_id", "default"),
        )
        engine.created_at = data["created_at"]
        engine._turn_count = data.get("turn_count", 0)
        engine._plan = data.get("plan")
        engine._scratchpad = data.get("scratchpad", {})
        engine._memories = data.get("memories", [])
        engine._messages = data.get("messages", [])

        # Migration: Phase 1 used "timestamp", Phase 2 uses "created_at"
        for mem in engine._memories:
            if "timestamp" in mem and "created_at" not in mem:
                mem["created_at"] = mem.pop("timestamp")

        return engine
