"""Tests for the Auxia Console tools."""

from unittest.mock import MagicMock, patch

import pytest

from src.agent.config import AuxiaConfig
from src.agent.tools.auxia import (
    AuxiaBFFClient,
    build_project_context,
    create_auxia_tools,
)


@pytest.fixture
def auxia_config():
    return AuxiaConfig(
        enabled=True,
        bff_base_url="https://console.auxia.io",
    )


class TestAuxiaBFFClient:
    def test_url_construction(self):
        client = AuxiaBFFClient(base_url="https://console.auxia.io/")
        assert client.base_url == "https://console.auxia.io"

    def test_url_no_trailing_slash(self):
        client = AuxiaBFFClient(base_url="https://console.auxia.io")
        assert client.base_url == "https://console.auxia.io"


class TestAuxiaTools:
    def test_tools_created(self, auxia_config):
        tools = create_auxia_tools(auxia_config, project_id="proj-1")
        names = {t.name for t in tools}
        assert "list_treatments" in names
        assert "get_treatment" in names
        assert "list_surfaces" in names
        assert "list_treatment_types" in names
        assert "list_objectives" in names
        assert "list_data_fields" in names

    def test_tool_count(self, auxia_config):
        tools = create_auxia_tools(auxia_config, project_id="proj-1")
        assert len(tools) == 6

    def test_tool_schemas_valid(self, auxia_config):
        tools = create_auxia_tools(auxia_config, project_id="proj-1")
        for tool in tools:
            schema = tool.to_api_schema()
            assert "name" in schema
            assert "description" in schema
            assert "input_schema" in schema
            assert schema["input_schema"]["type"] == "object"

    def test_get_treatment_requires_id(self, auxia_config):
        tools = create_auxia_tools(auxia_config, project_id="proj-1")
        get_treatment = next(t for t in tools if t.name == "get_treatment")
        assert "treatment_id" in get_treatment.input_schema.get("required", [])

    def test_list_treatments_has_filter_params(self, auxia_config):
        tools = create_auxia_tools(auxia_config, project_id="proj-1")
        list_treatments = next(t for t in tools if t.name == "list_treatments")
        props = list_treatments.input_schema["properties"]
        assert "surface" in props
        assert "treatment_type" in props
        assert "state" in props
        assert "search" in props


class TestBuildProjectContext:
    @patch("src.agent.tools.auxia.AuxiaBFFClient")
    def test_builds_context_string(self, mock_client_cls, auxia_config):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_client.get.side_effect = [
            [{"name": "home_page"}, {"name": "checkout"}],  # surfaces
            [{"name": "Push"}, {"name": "Email"}],  # treatment types
            [{"name": "increase_purchases"}],  # objectives
        ]

        context = build_project_context(auxia_config, "proj-1")
        assert "home_page" in context
        assert "checkout" in context
        assert "Push" in context
        assert "Email" in context
        assert "increase_purchases" in context

    @patch("src.agent.tools.auxia.AuxiaBFFClient")
    def test_handles_api_failures(self, mock_client_cls, auxia_config):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.get.side_effect = RuntimeError("connection failed")

        context = build_project_context(auxia_config, "proj-1")
        assert "not available" in context

    @patch("src.agent.tools.auxia.AuxiaBFFClient")
    def test_handles_empty_responses(self, mock_client_cls, auxia_config):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.get.return_value = []

        context = build_project_context(auxia_config, "proj-1")
        assert "not available" in context
