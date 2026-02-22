"""Agent configuration loader."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.config import load_config

logger = logging.getLogger(__name__)


@dataclass
class ModelConfig:
    orchestrator: str = "claude-sonnet-4-6"
    subagent: str = "claude-sonnet-4-6"
    classifier: str = "claude-haiku-4-5-20251001"


@dataclass
class TokenConfig:
    max_context: int = 180_000
    max_output: int = 8192
    compaction_threshold: int = 120_000
    summary_target: int = 4000


@dataclass
class BigQueryConfig:
    project: str = "auxia-reporting"
    dataset: str = "agent_dataset"
    location: str = "US"
    max_bytes_billed: int = 10_737_418_240  # 10 GB


@dataclass
class GCSConfig:
    bucket: str = "auxia-agent"
    session_prefix: str = "agent/sessions/"
    artifact_prefix: str = "agent/artifacts/"
    memory_prefix: str = "agent/memory/"


@dataclass
class AgentBehaviorConfig:
    max_turns: int = 50
    max_plan_steps: int = 20
    checkpoint_interval: int = 5
    enable_subagents: bool = True
    enable_fan_out: bool = True
    fan_out_max_parallel: int = 20
    context_buffer_threshold: int = 50  # Rows above this get buffered to GCS


@dataclass
class AuxiaConfig:
    """Configuration for Auxia Console API integration."""
    enabled: bool = True
    # BFF base URL — can be overridden for staging/dev
    bff_base_url: str = "https://console.auxia.io"


@dataclass
class AgentConfig:
    models: ModelConfig = field(default_factory=ModelConfig)
    tokens: TokenConfig = field(default_factory=TokenConfig)
    bigquery: BigQueryConfig = field(default_factory=BigQueryConfig)
    gcs: GCSConfig = field(default_factory=GCSConfig)
    agent: AgentBehaviorConfig = field(default_factory=AgentBehaviorConfig)
    auxia: AuxiaConfig = field(default_factory=AuxiaConfig)
    project_id: str = ""  # Auxia project ID, set at runtime

    @classmethod
    def from_yaml(cls, path: str | Path) -> AgentConfig:
        """Load config from YAML file with env var substitution."""
        raw = load_config(str(path))
        return cls._from_dict(raw)

    @classmethod
    def _from_dict(cls, d: dict[str, Any]) -> AgentConfig:
        """Build config from raw dictionary."""
        return cls(
            models=ModelConfig(**d.get("models", {})),
            tokens=TokenConfig(**d.get("tokens", {})),
            bigquery=BigQueryConfig(**d.get("bigquery", {})),
            gcs=GCSConfig(**d.get("gcs", {})),
            agent=AgentBehaviorConfig(**d.get("agent", {})),
            auxia=AuxiaConfig(**d.get("auxia", {})),
            project_id=d.get("project_id", ""),
        )

    @classmethod
    def default(cls) -> AgentConfig:
        """Return default config (no file needed)."""
        return cls()
