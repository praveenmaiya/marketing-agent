"""Tests for the session routing system."""

import pytest

from src.agent.session import (
    SessionKey,
    TriggerType,
    create_session_key,
    parse_session_key,
)


class TestSessionKey:
    def test_key_format(self):
        sk = SessionKey(
            trigger=TriggerType.CLI,
            source="alice",
            project_id="proj-1",
            session_id="abc123",
        )
        assert sk.key == "cli:alice:proj-1:abc123"

    def test_gcs_prefix(self):
        sk = SessionKey(
            trigger=TriggerType.CLI,
            source="alice",
            project_id="proj-1",
            session_id="abc123",
        )
        assert sk.gcs_prefix == "agent/memory/proj-1/sessions/"

    def test_memory_prefix(self):
        sk = SessionKey(
            trigger=TriggerType.SLACK,
            source="C12345",
            project_id="proj-2",
            session_id="def456",
        )
        assert sk.memory_prefix == "agent/memory/proj-2/"

    def test_frozen(self):
        sk = SessionKey(
            trigger=TriggerType.CLI,
            source="alice",
            project_id="proj-1",
            session_id="abc123",
        )
        with pytest.raises(AttributeError):
            sk.source = "bob"


class TestCreateSessionKey:
    def test_cli_auto_source(self):
        sk = create_session_key(TriggerType.CLI, project_id="proj-1")
        assert sk.trigger == TriggerType.CLI
        assert sk.source != ""  # auto-detected user
        assert sk.project_id == "proj-1"
        assert len(sk.session_id) == 8

    def test_explicit_source(self):
        sk = create_session_key(
            TriggerType.SLACK,
            project_id="proj-2",
            source="C_CHANNEL",
        )
        assert sk.source == "C_CHANNEL"

    def test_explicit_session_id(self):
        sk = create_session_key(
            TriggerType.CLI,
            project_id="proj-1",
            session_id="my-session",
        )
        assert sk.session_id == "my-session"

    def test_cron_default_source(self):
        sk = create_session_key(TriggerType.CRON, project_id="proj-3")
        assert sk.source == "default"

    def test_empty_project_defaults(self):
        sk = create_session_key(TriggerType.CLI, project_id="")
        assert sk.project_id == "default"

    def test_none_project_defaults(self):
        sk = create_session_key(TriggerType.CLI, project_id=None)
        assert sk.project_id == "default"


class TestParseSessionKey:
    def test_roundtrip(self):
        original = create_session_key(
            TriggerType.CLI,
            project_id="proj-1",
            source="alice",
            session_id="abc123",
        )
        parsed = parse_session_key(original.key)
        assert parsed.trigger == original.trigger
        assert parsed.source == original.source
        assert parsed.project_id == original.project_id
        assert parsed.session_id == original.session_id

    def test_parse_slack(self):
        parsed = parse_session_key("slack:C12345:proj-2:def456")
        assert parsed.trigger == TriggerType.SLACK
        assert parsed.source == "C12345"
        assert parsed.project_id == "proj-2"
        assert parsed.session_id == "def456"

    def test_parse_cron(self):
        parsed = parse_session_key("cron:daily_health:proj-3:ghi789")
        assert parsed.trigger == TriggerType.CRON
        assert parsed.source == "daily_health"

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid session key"):
            parse_session_key("bad-key")

    def test_too_few_parts_raises(self):
        with pytest.raises(ValueError, match="Invalid session key"):
            parse_session_key("cli:user")

    def test_unknown_trigger_raises(self):
        with pytest.raises(ValueError, match="Unknown trigger type"):
            parse_session_key("webhook:x:y:z")

    def test_session_id_with_colons(self):
        """Session IDs containing colons should parse correctly with maxsplit."""
        parsed = parse_session_key("cli:alice:proj-1:session:with:colons")
        assert parsed.trigger == TriggerType.CLI
        assert parsed.source == "alice"
        assert parsed.project_id == "proj-1"
        assert parsed.session_id == "session:with:colons"
