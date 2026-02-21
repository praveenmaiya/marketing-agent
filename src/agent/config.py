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
    dataset: str = "temp_holley_v5_17"
    location: str = "US"
    max_bytes_billed: int = 10_737_418_240  # 10 GB


@dataclass
class GCSConfig:
    bucket: str = "holley-models-dev"
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


@dataclass
class DomainConfig:
    company: str = "Holley"
    domain: str = "vehicle fitment recommendations"
    description: str = ""
    key_tables: list[str] = field(default_factory=list)
    key_metrics: list[str] = field(default_factory=list)


@dataclass
class AgentConfig:
    models: ModelConfig = field(default_factory=ModelConfig)
    tokens: TokenConfig = field(default_factory=TokenConfig)
    bigquery: BigQueryConfig = field(default_factory=BigQueryConfig)
    gcs: GCSConfig = field(default_factory=GCSConfig)
    agent: AgentBehaviorConfig = field(default_factory=AgentBehaviorConfig)
    domain: DomainConfig = field(default_factory=DomainConfig)

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
            domain=DomainConfig(**d.get("domain", {})),
        )

    @classmethod
    def default(cls) -> AgentConfig:
        """Return default config (no file needed)."""
        return cls()
