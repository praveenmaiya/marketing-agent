"""Tests for the agent context engine."""


import pytest

from src.agent.config import AgentConfig
from src.agent.context import ContextEngine


@pytest.fixture
def config():
    return AgentConfig.default()


@pytest.fixture
def context(config):
    return ContextEngine(config, session_id="test-123", project_id="proj-abc")


class TestHotLayer:
    def test_add_and_get_messages(self, context):
        context.add_message("user", "hello")
        context.add_message("assistant", "hi there")

        messages = context.get_messages()
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "hello"
        assert messages[1]["role"] == "assistant"

    def test_turn_count_increments(self, context):
        assert context.turn_count == 0
        context.add_message("user", "msg1")
        assert context.turn_count == 1
        context.add_message("assistant", "msg2")
        assert context.turn_count == 2

    def test_plan_lifecycle(self, context):
        assert context.get_plan() is None

        plan = {"objective": "test", "steps": []}
        context.set_plan(plan)
        assert context.get_plan() == plan

    def test_scratchpad(self, context):
        assert context.get_scratchpad("key") is None
        context.set_scratchpad("key", "value")
        assert context.get_scratchpad("key") == "value"

    def test_session_id(self, context):
        assert context.session_id == "test-123"

    def test_project_id(self, context):
        assert context.project_id == "proj-abc"

    def test_default_project_id(self, config):
        ctx = ContextEngine(config)
        assert ctx.project_id == "default"

    def test_auto_session_id(self, config):
        ctx = ContextEngine(config)
        assert len(ctx.session_id) == 8  # UUID[:8]


class TestCompaction:
    def test_no_compaction_when_few_messages(self, context):
        for i in range(5):
            context.add_message("user", f"msg {i}")
        result = context.compact()
        assert result is None
        assert len(context.get_messages()) == 5

    def test_compaction_preserves_recent(self, context):
        for i in range(15):
            context.add_message("user" if i % 2 == 0 else "assistant", f"message {i}")

        result = context.compact()
        assert result is not None

        messages = context.get_messages()
        # summary pair (2) + bridge message if needed (1) + last 6 = 8 or 9
        assert len(messages) >= 8
        # Summary pair
        assert "[Session context summary]" in messages[0]["content"]
        # Last message preserved
        assert "message 14" in messages[-1]["content"]
        # Verify alternating roles (no consecutive same-role messages)
        for i in range(1, len(messages)):
            assert messages[i]["role"] != messages[i - 1]["role"]

    def test_compaction_with_custom_summarizer(self, context):
        for i in range(15):
            context.add_message("assistant", f"Finding {i}: important data")

        def summarizer(msgs):
            return f"Summarized {len(msgs)} messages"

        result = context.compact(summarizer_fn=summarizer)
        assert "Summarized" in result
        messages = context.get_messages()
        assert "Summarized" in messages[0]["content"]

    def test_compaction_fallback_without_summarizer(self, context):
        for i in range(12):
            context.add_message(
                "assistant" if i % 2 == 0 else "user",
                f"Turn {i} content",
            )

        result = context.compact()
        assert result is not None
        assert "Previous conversation summary" in result


class TestPreCompactionFlush:
    def test_flush_with_custom_function(self, context):
        context.add_message("user", "analyze campaigns")
        context.add_message("assistant", "CTR dropped 15% in Q4")

        def flush_fn(msgs):
            return [{"content": "CTR dropped 15% in Q4", "category": "finding"}]

        saved = context.flush_before_compaction(flush_fn=flush_fn)
        assert len(saved) == 1
        assert saved[0]["category"] == "finding"
        assert "CTR dropped" in saved[0]["content"]

    def test_flush_heuristic_fallback(self, context):
        context.add_message("assistant", "Found something important about Q4")
        context.add_message("assistant", "Revenue is up 20%")

        saved = context.flush_before_compaction()
        assert len(saved) == 1
        assert saved[0]["category"] == "session_summary"

    def test_flush_empty_when_no_messages(self, context):
        saved = context.flush_before_compaction()
        assert len(saved) == 0

    def test_flush_empty_when_no_assistant_messages(self, context):
        context.add_message("user", "hello")
        saved = context.flush_before_compaction()
        assert len(saved) == 0


class TestSerialization:
    def test_to_dict_and_from_dict(self, config):
        ctx = ContextEngine(config, session_id="ser-test", project_id="proj-xyz")
        ctx.add_message("user", "hello")
        ctx.set_plan({"objective": "test", "steps": []})
        ctx.set_scratchpad("key", "val")
        ctx.save_memory("important fact", category="finding")

        data = ctx.to_dict()
        assert data["session_id"] == "ser-test"
        assert data["project_id"] == "proj-xyz"
        assert len(data["messages"]) == 1
        assert data["plan"]["objective"] == "test"
        assert data["scratchpad"]["key"] == "val"
        assert len(data["memories"]) == 1

        # Restore
        restored = ContextEngine.from_dict(data, config)
        assert restored.session_id == "ser-test"
        assert restored.project_id == "proj-xyz"
        assert len(restored.get_messages()) == 1
        assert restored.get_plan()["objective"] == "test"
        assert restored.get_scratchpad("key") == "val"

    def test_roundtrip_preserves_turn_count(self, config):
        ctx = ContextEngine(config, session_id="tc-test")
        ctx.add_message("user", "a")
        ctx.add_message("assistant", "b")
        ctx.add_message("user", "c")

        data = ctx.to_dict()
        restored = ContextEngine.from_dict(data, config)
        assert restored.turn_count == 3

    def test_roundtrip_preserves_project_id(self, config):
        ctx = ContextEngine(config, session_id="pid-test", project_id="my-proj")
        data = ctx.to_dict()
        restored = ContextEngine.from_dict(data, config)
        assert restored.project_id == "my-proj"

    def test_default_project_id_on_restore(self, config):
        """Old serialized data without project_id should default to 'default'."""
        data = {
            "session_id": "old-session",
            "created_at": "2024-01-01T00:00:00",
            "messages": [],
        }
        restored = ContextEngine.from_dict(data, config)
        assert restored.project_id == "default"

    def test_migration_timestamp_to_created_at(self, config):
        """Phase 1 memories with 'timestamp' should be migrated to 'created_at'."""
        data = {
            "session_id": "old-session",
            "created_at": "2024-01-01T00:00:00",
            "messages": [],
            "memories": [
                {
                    "id": "abc",
                    "content": "old finding",
                    "category": "finding",
                    "session_id": "old-session",
                    "timestamp": "2024-01-15T10:00:00",
                },
            ],
        }
        restored = ContextEngine.from_dict(data, config)
        mem = restored._memories[0]
        assert "created_at" in mem
        assert mem["created_at"] == "2024-01-15T10:00:00"
        assert "timestamp" not in mem

    def test_migration_preserves_existing_created_at(self, config):
        """Phase 2 memories with 'created_at' should not be changed."""
        data = {
            "session_id": "new-session",
            "created_at": "2025-01-01T00:00:00",
            "messages": [],
            "memories": [
                {
                    "id": "def",
                    "content": "new finding",
                    "category": "finding",
                    "session_id": "new-session",
                    "created_at": "2025-01-15T10:00:00",
                },
            ],
        }
        restored = ContextEngine.from_dict(data, config)
        mem = restored._memories[0]
        assert mem["created_at"] == "2025-01-15T10:00:00"


class TestMemory:
    def test_save_and_search_memory_in_hot_layer(self, context):
        context.save_memory("CTR for personalized is 2.5%", category="finding")
        context.save_memory("Revenue increased 15% in Q4", category="finding")

        results = context.search_memories("CTR")
        assert len(results) >= 1
        assert any("CTR" in r["content"] for r in results)

    def test_search_no_match(self, context):
        context.save_memory("unrelated fact")
        results = context.search_memories("nonexistent_xyz")
        assert len(results) == 0

    def test_memory_has_metadata(self, context):
        context.save_memory("test memory", category="decision")
        results = context.search_memories("test memory")
        assert len(results) == 1
        assert results[0]["category"] == "decision"
        assert results[0]["session_id"] == "test-123"
        assert results[0]["project_id"] == "proj-abc"
        assert "created_at" in results[0]
        assert "id" in results[0]

    def test_memory_categories(self, context):
        context.save_memory("a finding", category="finding")
        context.save_memory("a decision", category="decision")
        context.save_memory("a pattern", category="pattern")

        # All should be searchable
        findings = context.search_memories("finding")
        assert len(findings) >= 1

    def test_save_memory_returns_dict(self, context):
        mem = context.save_memory("test content", category="finding")
        assert isinstance(mem, dict)
        assert mem["content"] == "test content"
        assert mem["category"] == "finding"
        assert "id" in mem
