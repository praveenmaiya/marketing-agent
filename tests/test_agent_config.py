"""Tests for agent configuration loading."""


import pytest

from src.agent.config import (
    AgentConfig,
    BigQueryConfig,
    GCSConfig,
    ModelConfig,
    TokenConfig,
)


class TestAgentConfig:
    def test_default_config(self):
        config = AgentConfig.default()
        assert config.models.orchestrator == "claude-sonnet-4-6"
        assert config.models.classifier == "claude-haiku-4-5-20251001"
        assert config.tokens.max_context == 180_000
        assert config.bigquery.project == "auxia-reporting"
        assert config.agent.max_turns == 50

    def test_from_dict(self):
        raw = {
            "models": {"orchestrator": "claude-opus-4-6"},
            "tokens": {"max_context": 100_000},
            "bigquery": {"project": "test-project"},
        }
        config = AgentConfig._from_dict(raw)
        assert config.models.orchestrator == "claude-opus-4-6"
        assert config.tokens.max_context == 100_000
        assert config.bigquery.project == "test-project"
        # Defaults preserved for unspecified sections
        assert config.gcs.bucket == "holley-models-dev"
        assert config.agent.max_turns == 50

    def test_from_yaml(self, tmp_path):
        yaml_content = """
models:
  orchestrator: claude-opus-4-6
tokens:
  max_context: 200000
bigquery:
  project: my-project
  dataset: my_dataset
agent:
  max_turns: 100
  enable_fan_out: false
domain:
  company: TestCo
  key_tables:
    - table_a
    - table_b
"""
        config_file = tmp_path / "test_agent.yaml"
        config_file.write_text(yaml_content)

        config = AgentConfig.from_yaml(str(config_file))
        assert config.models.orchestrator == "claude-opus-4-6"
        assert config.tokens.max_context == 200_000
        assert config.bigquery.project == "my-project"
        assert config.bigquery.dataset == "my_dataset"
        assert config.agent.max_turns == 100
        assert config.agent.enable_fan_out is False
        assert config.domain.company == "TestCo"
        assert len(config.domain.key_tables) == 2

    def test_partial_yaml(self, tmp_path):
        yaml_content = """
models:
  orchestrator: claude-sonnet-4-6
"""
        config_file = tmp_path / "partial.yaml"
        config_file.write_text(yaml_content)

        config = AgentConfig.from_yaml(str(config_file))
        assert config.models.orchestrator == "claude-sonnet-4-6"
        # All other defaults should be intact
        assert config.tokens.max_output == 8192
        assert config.gcs.session_prefix == "agent/sessions/"

    def test_missing_yaml_raises(self):
        with pytest.raises(FileNotFoundError):
            AgentConfig.from_yaml("/nonexistent/path.yaml")


class TestModelConfig:
    def test_defaults(self):
        config = ModelConfig()
        assert config.orchestrator == "claude-sonnet-4-6"
        assert config.subagent == "claude-sonnet-4-6"
        assert config.classifier == "claude-haiku-4-5-20251001"


class TestTokenConfig:
    def test_compaction_threshold_below_max_context(self):
        config = TokenConfig()
        assert config.compaction_threshold < config.max_context


class TestBigQueryConfig:
    def test_max_bytes_is_10gb(self):
        config = BigQueryConfig()
        assert config.max_bytes_billed == 10 * 1024 * 1024 * 1024


class TestGCSConfig:
    def test_prefixes_end_with_slash(self):
        config = GCSConfig()
        assert config.session_prefix.endswith("/")
        assert config.artifact_prefix.endswith("/")
        assert config.memory_prefix.endswith("/")
