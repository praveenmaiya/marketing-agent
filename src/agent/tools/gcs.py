"""GCS tools for the marketing agent.

Wraps the existing gcs_utils to provide tool-calling interface for the LLM.
Includes path validation: only allowed prefixes can be accessed.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from src.agent.config import GCSConfig
from src.agent.tools import ToolDef
from src.gcs_utils import download_blob, list_blobs, upload_blob

logger = logging.getLogger(__name__)


def _validate_gcs_path(path: str, config: GCSConfig) -> str:
    """Ensure the path falls under an allowed prefix and has no traversal."""
    allowed = (config.session_prefix, config.artifact_prefix, config.memory_prefix)
    if ".." in path:
        raise ValueError("Path traversal (..) is not allowed.")
    if not any(path.startswith(prefix) for prefix in allowed):
        raise ValueError(f"Path must start with one of: {allowed}. Got: {path}")
    return path


def create_gcs_tools(config: GCSConfig) -> list[ToolDef]:
    """Create GCS tools for reading/writing artifacts."""

    def read_gcs(params: dict) -> str:
        """Read a text file from GCS."""
        blob_name = _validate_gcs_path(params["path"], config)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            tmp_path = f.name
        try:
            download_blob(config.bucket, blob_name, tmp_path)
            content = Path(tmp_path).read_text()
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        max_chars = params.get("max_chars", 50_000)
        if len(content) > max_chars:
            return content[:max_chars] + f"\n\n... truncated ({len(content)} total chars)"
        return content

    def write_gcs(params: dict) -> str:
        """Write content to a GCS blob."""
        blob_name = _validate_gcs_path(params["path"], config)
        content = params["content"]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            tmp_path = f.name
        try:
            uri = upload_blob(tmp_path, config.bucket, blob_name)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        return f"Written to {uri} ({len(content)} chars)"

    def list_gcs(params: dict) -> str:
        """List blobs under a GCS prefix."""
        prefix = params.get("prefix", "")
        # Validate prefix if provided
        if prefix:
            _validate_gcs_path(prefix, config)
        blobs = list_blobs(config.bucket, prefix=prefix)
        if not blobs:
            return f"No blobs found under gs://{config.bucket}/{prefix}"
        return json.dumps(blobs[:200], indent=2)  # Cap at 200 entries

    return [
        ToolDef(
            name="read_gcs",
            description="Read a text file from Google Cloud Storage (agent prefixes only).",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Blob path within the bucket (must start with agent/ prefix)",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max characters to return (default 50000)",
                        "default": 50000,
                    },
                },
                "required": ["path"],
            },
            handler=read_gcs,
        ),
        ToolDef(
            name="write_gcs",
            description="Write text content to Google Cloud Storage (agent prefixes only).",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Blob path within the bucket (must start with agent/ prefix)",
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to write",
                    },
                },
                "required": ["path", "content"],
            },
            handler=write_gcs,
        ),
        ToolDef(
            name="list_gcs",
            description="List files under a GCS prefix (agent prefixes only).",
            input_schema={
                "type": "object",
                "properties": {
                    "prefix": {
                        "type": "string",
                        "description": "Path prefix to filter (must start with agent/ prefix)",
                        "default": "",
                    },
                },
            },
            handler=list_gcs,
        ),
    ]
