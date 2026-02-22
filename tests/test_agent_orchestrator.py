"""Tests for the agent orchestrator and plan manager."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.agent.config import AgentConfig
from src.agent.context import ContextEngine
from src.agent.orchestrator import Orchestrator, PlanManager, _build_plan_tools
from src.agent.tools import ToolDef


@pytest.fixture
def config():
    return AgentConfig.default()


@pytest.fixture
def context(config):
    return ContextEngine(config, session_id="orch-test", project_id="test-proj")


# ── Plan Manager ──────────────────────────────────────────────


class TestPlanManager:
    def test_make_plan(self, context):
        pm = PlanManager(context)
        plan = pm.make_plan("Analyze campaigns", ["Step 1", "Step 2", "Step 3"])

        assert plan["objective"] == "Analyze campaigns"
        assert plan["status"] == "active"
        assert len(plan["steps"]) == 3
        assert plan["steps"][0]["status"] == "pending"
        assert plan["steps"][0]["id"] == 1

    def test_get_current_step(self, context):
        pm = PlanManager(context)
        pm.make_plan("Test", ["A", "B", "C"])

        step = pm.get_current_step()
        assert step["id"] == 1
        assert step["description"] == "A"

    def test_get_current_step_skips_completed(self, context):
        pm = PlanManager(context)
        pm.make_plan("Test", ["A", "B", "C"])
        pm.update_step(1, "completed", "Done")

        step = pm.get_current_step()
        assert step["id"] == 2

    def test_update_step(self, context):
        pm = PlanManager(context)
        pm.make_plan("Test", ["Step 1"])
        pm.update_step(1, "in_progress")

        plan = context.get_plan()
        assert plan["steps"][0]["status"] == "in_progress"

        pm.update_step(1, "completed", "Found 100 results")
        plan = context.get_plan()
        assert plan["steps"][0]["status"] == "completed"
        assert plan["steps"][0]["result"] == "Found 100 results"

    def test_revise_plan(self, context):
        pm = PlanManager(context)
        pm.make_plan("Test", ["A", "B", "C"])
        pm.update_step(1, "completed", "Done A")

        plan = pm.revise_plan(["B revised", "D new", "E new"])
        assert len(plan["steps"]) == 4  # 1 completed + 3 new
        assert plan["steps"][0]["status"] == "completed"
        assert plan["steps"][1]["description"] == "B revised"
        assert plan["steps"][1]["status"] == "pending"

    def test_is_complete(self, context):
        pm = PlanManager(context)
        pm.make_plan("Test", ["A", "B"])

        assert not pm.is_complete()

        pm.update_step(1, "completed")
        assert not pm.is_complete()

        pm.update_step(2, "completed")
        assert pm.is_complete()

    def test_is_complete_with_failures(self, context):
        pm = PlanManager(context)
        pm.make_plan("Test", ["A", "B"])
        pm.update_step(1, "completed")
        pm.update_step(2, "failed")
        assert pm.is_complete()  # failed counts as done

    def test_format_for_prompt(self, context):
        pm = PlanManager(context)
        pm.make_plan("Analyze Q4", ["Query data", "Compute metrics", "Generate report"])
        pm.update_step(1, "completed", "Got 1000 rows")

        text = pm.format_for_prompt()
        assert "Analyze Q4" in text
        assert "[x]" in text  # completed
        assert "[ ]" in text  # pending
        assert "Got 1000 rows" in text

    def test_max_steps_enforced(self, context):
        pm = PlanManager(context, max_steps=3)
        plan = pm.make_plan("Test", [f"Step {i}" for i in range(10)])
        assert len(plan["steps"]) == 3

    def test_no_plan_returns_none(self, context):
        pm = PlanManager(context)
        assert pm.get_current_step() is None
        assert pm.is_complete() is True
        assert pm.format_for_prompt() == "No active plan."


# ── Plan Tools ────────────────────────────────────────────────


class TestPlanTools:
    def test_plan_tools_created(self, context):
        pm = PlanManager(context)
        tools = _build_plan_tools(pm)
        names = {t.name for t in tools}
        assert "make_plan" in names
        assert "update_plan_step" in names
        assert "revise_plan" in names

    def test_make_plan_tool_execution(self, context):
        pm = PlanManager(context)
        tools = _build_plan_tools(pm)
        make_plan = next(t for t in tools if t.name == "make_plan")

        result = make_plan.execute({
            "objective": "Test objective",
            "steps": ["Do thing 1", "Do thing 2"],
        })
        parsed = json.loads(result)
        assert parsed["objective"] == "Test objective"
        assert len(parsed["steps"]) == 2


# ── Orchestrator ──────────────────────────────────────────────


class TestOrchestrator:
    def test_construction(self, config, context):
        orch = Orchestrator(config, context=context)
        assert orch.context is context
        # Plan tools should be registered
        assert orch.registry.get("make_plan") is not None
        assert orch.registry.get("update_plan_step") is not None
        assert orch.registry.get("revise_plan") is not None

    def test_add_tools(self, config, context):
        orch = Orchestrator(config, context=context)
        custom_tool = ToolDef(
            name="custom",
            description="Custom tool",
            input_schema={"type": "object", "properties": {}},
            handler=lambda p: "custom result",
        )
        orch.add_tools([custom_tool])
        assert orch.registry.get("custom") is not None

    def test_build_system_prompt_generic(self, config, context):
        orch = Orchestrator(config, context=context)
        prompt = orch._build_system_prompt()
        # Should reference Auxia, not Holley
        assert "Auxia" in prompt
        assert "test-proj" in prompt
        assert "No active plan" in prompt
        # Should contain rules
        assert "Rules" in prompt
        assert "ALWAYS make a plan" in prompt
        assert "Do NOT loop on errors" in prompt

    def test_build_system_prompt_with_project_context(self, config, context):
        orch = Orchestrator(
            config,
            context=context,
            project_context="Surfaces: home_page, checkout\nTreatment types: Push, Email",
        )
        prompt = orch._build_system_prompt()
        assert "home_page" in prompt
        assert "Push" in prompt

    def test_build_system_prompt_with_plan(self, config, context):
        orch = Orchestrator(config, context=context)
        orch.plan_manager.make_plan("Analyze data", ["Step A", "Step B"])
        prompt = orch._build_system_prompt()
        assert "Analyze data" in prompt
        assert "Step A" in prompt

    def test_set_project_context(self, config, context):
        orch = Orchestrator(config, context=context)
        orch.set_project_context("Surfaces: home, checkout")
        prompt = orch._build_system_prompt()
        assert "home, checkout" in prompt

    def test_extract_text_from_string(self, config, context):
        orch = Orchestrator(config, context=context)
        assert orch._extract_text("hello") == "hello"

    def test_extract_text_from_blocks(self, config, context):
        orch = Orchestrator(config, context=context)
        blocks = [MagicMock(text="part 1"), MagicMock(text="part 2")]
        result = orch._extract_text(blocks)
        assert "part 1" in result
        assert "part 2" in result

    def test_extract_text_from_dicts(self, config, context):
        orch = Orchestrator(config, context=context)
        blocks = [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "name": "something"},
        ]
        result = orch._extract_text(blocks)
        assert "hello" in result

    @patch("src.agent.orchestrator.anthropic.Anthropic")
    def test_run_simple_end_turn(self, mock_anthropic_cls, config, context):
        """Test a simple run that immediately returns end_turn."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # Create a mock response with end_turn
        mock_text_block = MagicMock()
        mock_text_block.text = "Analysis complete: CTR is 2.5%"
        mock_text_block.type = "text"

        mock_response = MagicMock()
        mock_response.content = [mock_text_block]
        mock_response.stop_reason = "end_turn"

        mock_client.messages.create.return_value = mock_response

        orch = Orchestrator(config, context=context)
        orch.client = mock_client

        result = orch.run("What is the CTR?")
        assert "CTR is 2.5%" in result
        assert mock_client.messages.create.call_count == 1

    @patch("src.agent.orchestrator.anthropic.Anthropic")
    def test_run_with_tool_use(self, mock_anthropic_cls, config, context):
        """Test a run with one tool call followed by end_turn."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # First response: tool use
        mock_tool_block = MagicMock()
        mock_tool_block.type = "tool_use"
        mock_tool_block.name = "custom_tool"
        mock_tool_block.id = "tool_123"
        mock_tool_block.input = {"key": "value"}

        response1 = MagicMock()
        response1.content = [mock_tool_block]
        response1.stop_reason = "tool_use"

        # Second response: end_turn
        mock_text_block = MagicMock()
        mock_text_block.text = "Done with analysis"
        mock_text_block.type = "text"

        response2 = MagicMock()
        response2.content = [mock_text_block]
        response2.stop_reason = "end_turn"

        mock_client.messages.create.side_effect = [response1, response2]

        # Register a custom tool
        custom_tool = ToolDef(
            name="custom_tool",
            description="test",
            input_schema={"type": "object", "properties": {}},
            handler=lambda p: "tool result data",
        )

        orch = Orchestrator(config, context=context, tools=[custom_tool])
        orch.client = mock_client

        result = orch.run("Run analysis")
        assert "Done with analysis" in result
        assert mock_client.messages.create.call_count == 2

    @patch("src.agent.orchestrator.anthropic.Anthropic")
    def test_unknown_tool_returns_error(self, mock_anthropic_cls, config, context):
        """Test that unknown tool names return an error result."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # Tool use for unknown tool
        mock_tool_block = MagicMock()
        mock_tool_block.type = "tool_use"
        mock_tool_block.name = "nonexistent_tool"
        mock_tool_block.id = "tool_456"
        mock_tool_block.input = {}

        response1 = MagicMock()
        response1.content = [mock_tool_block]
        response1.stop_reason = "tool_use"

        # End turn
        mock_text_block = MagicMock()
        mock_text_block.text = "Handled error"
        mock_text_block.type = "text"

        response2 = MagicMock()
        response2.content = [mock_text_block]
        response2.stop_reason = "end_turn"

        mock_client.messages.create.side_effect = [response1, response2]

        orch = Orchestrator(config, context=context)
        orch.client = mock_client

        orch.run("test")
        # Verify the error tool result was sent
        messages_sent = context.get_messages()
        tool_results = [
            m for m in messages_sent
            if isinstance(m.get("content"), list)
            and any(r.get("is_error") for r in m["content"] if isinstance(r, dict))
        ]
        assert len(tool_results) == 1
