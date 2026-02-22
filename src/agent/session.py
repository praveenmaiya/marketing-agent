"""Session routing and isolation.

Session keys follow a structured pattern to prevent cross-contamination
between different trigger types:

    cli:{user}:{project_id}       — interactive CLI sessions
    slack:{channel}:{project_id}  — Slack-triggered sessions
    cron:{job_name}:{project_id}  — scheduled cron sessions

Each session key maps to isolated state in GCS. No session can
access another session's state.
"""

from __future__ import annotations

import getpass
import logging
import uuid
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class TriggerType(Enum):
    CLI = "cli"
    SLACK = "slack"
    CRON = "cron"


@dataclass(frozen=True)
class SessionKey:
    """Unique session identifier with trigger-based isolation."""

    trigger: TriggerType
    source: str  # user, channel, or job name
    project_id: str
    session_id: str  # unique per invocation

    @property
    def key(self) -> str:
        """Full session key string."""
        return f"{self.trigger.value}:{self.source}:{self.project_id}:{self.session_id}"

    @property
    def gcs_prefix(self) -> str:
        """GCS path prefix for this session's state."""
        return f"agent/memory/{self.project_id}/sessions/"

    @property
    def memory_prefix(self) -> str:
        """GCS path prefix for this project's memories."""
        return f"agent/memory/{self.project_id}/"


def create_session_key(
    trigger: TriggerType,
    project_id: str,
    source: str | None = None,
    session_id: str | None = None,
) -> SessionKey:
    """Create a session key for the given trigger type.

    Args:
        trigger: How the agent was triggered (CLI, Slack, Cron).
        project_id: Auxia project ID.
        source: Trigger-specific source identifier.
                CLI: username, Slack: channel ID, Cron: job name.
                Auto-detected for CLI if not provided.
        session_id: Unique session ID. Auto-generated if not provided.

    Returns:
        SessionKey with all fields populated.
    """
    if source is None:
        if trigger == TriggerType.CLI:
            source = _get_current_user()
        elif trigger == TriggerType.CRON:
            source = "default"
        else:
            source = "unknown"

    if session_id is None:
        session_id = str(uuid.uuid4())[:8]

    return SessionKey(
        trigger=trigger,
        source=source,
        project_id=project_id or "default",
        session_id=session_id,
    )


def _get_current_user() -> str:
    """Get the current system username."""
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def parse_session_key(key: str) -> SessionKey:
    """Parse a session key string back into a SessionKey.

    Args:
        key: Session key string (e.g., "cli:user:project:abc123").

    Returns:
        Parsed SessionKey.

    Raises:
        ValueError: If the key format is invalid.
    """
    parts = key.split(":", maxsplit=3)
    if len(parts) != 4:
        raise ValueError(
            f"Invalid session key format: '{key}'. "
            f"Expected 'trigger:source:project_id:session_id'."
        )

    try:
        trigger = TriggerType(parts[0])
    except ValueError:
        raise ValueError(
            f"Unknown trigger type: '{parts[0]}'. "
            f"Expected one of: {[t.value for t in TriggerType]}"
        )

    return SessionKey(
        trigger=trigger,
        source=parts[1],
        project_id=parts[2],
        session_id=parts[3],
    )
