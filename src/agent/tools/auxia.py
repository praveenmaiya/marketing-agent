"""Auxia Console tools for the marketing agent.

Read-only tools wrapping the Auxia Console BFF API.
Phase 1: browse treatments, surfaces, treatment types, objectives, data fields.
Phase 2 (future): create/modify treatments.

Authentication uses the Auxia BFF session configured via environment or MCP.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.agent.config import AuxiaConfig
from src.agent.tools import ToolDef

logger = logging.getLogger(__name__)


class AuxiaBFFClient:
    """Minimal HTTP client for Auxia Console BFF API."""

    def __init__(self, base_url: str, session_cookie: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_cookie = session_cookie or os.environ.get(
            "AUXIA_SESSION_COOKIE", ""
        )

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        body: dict | None = None,
    ) -> dict[str, Any]:
        """Make an HTTP request to the BFF API."""
        url = f"{self.base_url}{path}"
        if params:
            filtered = {k: v for k, v in params.items() if v}
            if filtered:
                url = f"{url}?{urlencode(filtered)}"

        data = json.dumps(body).encode() if body else None
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.session_cookie:
            headers["Cookie"] = self.session_cookie

        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            body_text = e.read().decode() if e.fp else ""
            raise RuntimeError(
                f"Auxia BFF API error {e.code}: {body_text[:500]}"
            ) from e
        except URLError as e:
            raise RuntimeError(f"Auxia BFF connection error: {e}") from e

    def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params)


def create_auxia_tools(
    config: AuxiaConfig,
    project_id: str,
) -> list[ToolDef]:
    """Create read-only Auxia Console tools."""
    client = AuxiaBFFClient(base_url=config.bff_base_url)

    def list_treatments(params: dict) -> str:
        """List treatments with optional filtering."""
        query_params: dict[str, str] = {}
        if params.get("surface"):
            query_params["surface"] = params["surface"]
        if params.get("treatment_type"):
            query_params["treatmentType"] = params["treatment_type"]
        if params.get("state"):
            query_params["state"] = params["state"]
        if params.get("search"):
            query_params["search"] = params["search"]

        limit = params.get("limit", 50)
        offset = params.get("offset", 0)
        query_params["limit"] = str(limit)
        query_params["offset"] = str(offset)

        result = client.get(
            f"/api/projects/{project_id}/treatments",
            params=query_params,
        )
        return json.dumps(result, indent=2, default=str)

    def get_treatment(params: dict) -> str:
        """Get full details of a specific treatment."""
        treatment_id = params["treatment_id"]
        result = client.get(
            f"/api/projects/{project_id}/treatments/{treatment_id}"
        )
        return json.dumps(result, indent=2, default=str)

    def list_surfaces(params: dict) -> str:
        """List available surfaces (placement locations)."""
        result = client.get(f"/api/projects/{project_id}/surfaces")
        return json.dumps(result, indent=2, default=str)

    def list_treatment_types(params: dict) -> str:
        """List treatment type categories."""
        result = client.get(f"/api/projects/{project_id}/treatment-types")
        return json.dumps(result, indent=2, default=str)

    def list_objectives(params: dict) -> str:
        """List business objectives."""
        result = client.get(f"/api/projects/{project_id}/objectives")
        return json.dumps(result, indent=2, default=str)

    def list_data_fields(params: dict) -> str:
        """List user attributes and data fields."""
        query_params: dict[str, str] = {}
        limit = params.get("limit", 50)
        offset = params.get("offset", 0)
        query_params["limit"] = str(limit)
        query_params["offset"] = str(offset)

        result = client.get(
            f"/api/projects/{project_id}/data-fields",
            params=query_params,
        )
        return json.dumps(result, indent=2, default=str)

    return [
        ToolDef(
            name="list_treatments",
            description=(
                "List treatments in the Auxia project. "
                "Supports filtering by surface, treatment type, state (LIVE/DRAFT), and text search. "
                "Returns treatment summaries with IDs for further lookup."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "surface": {
                        "type": "string",
                        "description": "Filter by surface name (case-insensitive)",
                    },
                    "treatment_type": {
                        "type": "string",
                        "description": "Filter by treatment type name",
                    },
                    "state": {
                        "type": "string",
                        "enum": ["LIVE", "DRAFT"],
                        "description": "Filter by treatment state",
                    },
                    "search": {
                        "type": "string",
                        "description": "Text search across name, type, tags, objectives",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50)",
                        "default": 50,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Pagination offset (default 0)",
                        "default": 0,
                    },
                },
            },
            handler=list_treatments,
        ),
        ToolDef(
            name="get_treatment",
            description=(
                "Get full details of a specific treatment by ID. "
                "Returns content, targeting rules, objectives, and performance data."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "treatment_id": {
                        "type": "string",
                        "description": "The treatment ID to look up",
                    },
                },
                "required": ["treatment_id"],
            },
            handler=get_treatment,
        ),
        ToolDef(
            name="list_surfaces",
            description=(
                "List all surfaces (placement locations) in the project. "
                "Surfaces are where treatments can be displayed (e.g., home page, checkout)."
            ),
            input_schema={
                "type": "object",
                "properties": {},
            },
            handler=list_surfaces,
        ),
        ToolDef(
            name="list_treatment_types",
            description=(
                "List treatment type categories in the project. "
                "Examples: Push Notifications, In-App Messages, Email."
            ),
            input_schema={
                "type": "object",
                "properties": {},
            },
            handler=list_treatment_types,
        ),
        ToolDef(
            name="list_objectives",
            description=(
                "List business objectives configured for the project. "
                "Objectives define what treatments optimize for (e.g., purchases, engagement)."
            ),
            input_schema={
                "type": "object",
                "properties": {},
            },
            handler=list_objectives,
        ),
        ToolDef(
            name="list_data_fields",
            description=(
                "List user attributes and event-derived data fields. "
                "Data fields can be used in eligibility rules and targeting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50)",
                        "default": 50,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Pagination offset (default 0)",
                        "default": 0,
                    },
                },
            },
            handler=list_data_fields,
        ),
    ]


def build_project_context(
    config: AuxiaConfig,
    project_id: str,
) -> str:
    """Fetch project metadata from Auxia Console to build system prompt context.

    Returns a text block describing surfaces, treatment types, and objectives
    for injection into the system prompt.
    """
    client = AuxiaBFFClient(base_url=config.bff_base_url)
    parts = []

    try:
        surfaces = client.get(f"/api/projects/{project_id}/surfaces")
        if surfaces:
            names = [s.get("name", "unknown") for s in surfaces if isinstance(s, dict)]
            if names:
                parts.append(f"Surfaces: {', '.join(names)}")
    except Exception as e:
        logger.debug(f"Failed to fetch surfaces: {e}")

    try:
        types = client.get(f"/api/projects/{project_id}/treatment-types")
        if types:
            names = [t.get("name", "unknown") for t in types if isinstance(t, dict)]
            if names:
                parts.append(f"Treatment types: {', '.join(names)}")
    except Exception as e:
        logger.debug(f"Failed to fetch treatment types: {e}")

    try:
        objectives = client.get(f"/api/projects/{project_id}/objectives")
        if objectives:
            names = [o.get("name", "unknown") for o in objectives if isinstance(o, dict)]
            if names:
                parts.append(f"Objectives: {', '.join(names)}")
    except Exception as e:
        logger.debug(f"Failed to fetch objectives: {e}")

    if parts:
        return "\n".join(parts)
    return "Project context not available (API fetch failed or no data)."
