"""Configuration loading utilities."""

import os
import re
from pathlib import Path
from typing import Any

import yaml


def load_config(config_path: str) -> dict[str, Any]:
    """Load configuration from YAML file with environment variable substitution.

    Args:
        config_path: Path to YAML config file.

    Returns:
        Configuration dictionary with env vars substituted.

    Example:
        Config file with `project_id: ${PROJECT_ID}` will substitute
        the PROJECT_ID environment variable.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        content = f.read()

    # Substitute environment variables
    content = _substitute_env_vars(content)

    config = yaml.safe_load(content)
    return config


def _substitute_env_vars(content: str) -> str:
    """Substitute ${VAR} patterns with environment variables.

    Args:
        content: String content with ${VAR} patterns.

    Returns:
        Content with environment variables substituted.
    """
    pattern = r'\$\{([^}]+)\}'

    def replacer(match):
        var_name = match.group(1)
        # Support default values: ${VAR:-default}
        if ":-" in var_name:
            var_name, default = var_name.split(":-", 1)
            return os.environ.get(var_name, default)
        return os.environ.get(var_name, match.group(0))

    return re.sub(pattern, replacer, content)


def get_project_root() -> Path:
    """Get the project root directory.

    Returns:
        Path to project root (directory containing pyproject.toml).
    """
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    return current


def load_eval_thresholds(config_path: str = None) -> dict[str, Any]:
    """Load evaluation thresholds.

    Args:
        config_path: Optional path to thresholds config.

    Returns:
        Thresholds dictionary.
    """
    if config_path is None:
        config_path = get_project_root() / "configs" / "eval" / "thresholds.yaml"

    return load_config(str(config_path))
