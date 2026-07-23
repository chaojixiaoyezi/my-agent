"""LLM: Tool discovery and execution must share one structured runtime scope.

These tests keep restricted main/sub-agent runs from learning or loading tools
outside their allowlist, even though the process-wide registry contains them.
"""

from __future__ import annotations

import json

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.models import (
    BaseTool,
    ToolAvailability,
    ToolExecutionResult,
    ToolSpec,
)


def _agent(tmp_path) -> SimpleAgent:
    return SimpleAgent(
        AgentConfig(
            enable_tools=True,
            enable_subagents=True,
            memory_path=str(tmp_path / "memory.jsonl"),
        ),
        str(tmp_path),
    )


def test_restricted_list_tools_only_reports_current_runtime_scope(tmp_path) -> None:
    """LLM: list_tools is a scoped discovery surface, not a process-wide registry dump."""
    agent = _agent(tmp_path)

    result = agent.tools.execute_call(
        {"tool": "list_tools"},
        allowed_tools=["list_tools", "read_file"],
    )

    assert result.ok, result.output
    names = {item["name"] for item in json.loads(result.output)["tools"]}
    assert names == {"list_tools", "read_file"}


def test_restricted_tool_search_cannot_load_ungranted_deferred_tools(tmp_path) -> None:
    """LLM: search narrows an existing grant and never creates a new tool grant."""
    agent = _agent(tmp_path)

    result = agent.tools.execute_call(
        {
            "tool": "tool_search",
            "query": "create_subagents 创建并管理子代理",
            "limit": 8,
        },
        allowed_tools=["tool_search"],
    )

    assert result.ok, result.output
    assert result.result_envelope["tool_search"]["loaded_tool_names"] == []
    assert json.loads(result.output)["tools"] == []


class _SwitchTool(BaseTool):
    """LLM: controllable readiness proves snapshot and live recheck semantics without external services."""

    spec = ToolSpec(
        name="switch_tool",
        category="system",
        effect="read_only",
        description="test-only readiness switch",
        use_cases=[],
        avoid_when=[],
        keywords=["switch"],
        parameters={},
    )

    def __init__(self, *, ready: bool) -> None:
        self.ready = ready
        self.executions = 0

    def availability(self) -> ToolAvailability:
        return (
            ToolAvailability.ready()
            if self.ready
            else ToolAvailability.unavailable("switch is off")
        )

    def execute(self, params) -> ToolExecutionResult:
        _ = params
        self.executions += 1
        return ToolExecutionResult(self.spec.name, True, "executed")


def test_unconfigured_optional_tools_are_absent_and_report_unavailable(tmp_path) -> None:
    """LLM: registered adapters are not advertised as usable before their structured config exists."""
    agent = _agent(tmp_path)

    names = {
        spec.name
        for spec in agent.tools.specs(include_orchestration=True)
    }
    assert "analyze_image" not in names
    assert "lsp" not in names
    assert agent.tools._lsp_manager.clients == {}

    result = agent.tools.execute_call(
        {"tool": "analyze_image", "image": "missing.png"}
    )
    assert not result.ok
    assert result.error_code == "TOOL_UNAVAILABLE"
    assert "vision" in result.output

    unauthorized = agent.tools.execute_call(
        {"tool": "analyze_image", "image": "missing.png"},
        allowed_tools=["list_tools"],
    )
    assert unauthorized.error_code == "TOOL_NOT_ALLOWED"
    assert "vision_api_base" not in unauthorized.output


def test_run_snapshot_does_not_expand_when_tool_becomes_ready_later(tmp_path) -> None:
    """LLM: a mid-run readiness change only affects the next run, like 会话运行时 StepContext."""
    agent = _agent(tmp_path)
    tool = _SwitchTool(ready=False)
    agent.tools.register(tool)
    snapshot = agent.tools.runtime_snapshot(allowed_tools=["switch_tool"])
    tool.ready = True
    assert agent.tools.specs(runtime_snapshot=snapshot) == []

    result = agent.tools.execute_call(
        {"tool": "switch_tool"},
        allowed_tools=["switch_tool"],
        runtime_snapshot=snapshot,
    )

    assert not result.ok
    assert result.error_code == "TOOL_UNAVAILABLE"
    assert tool.executions == 0


def test_run_snapshot_does_not_expand_when_tool_is_registered_later(tmp_path) -> None:
    """LLM: dynamic registration only affects a later run, never the current frozen tool universe."""
    agent = _agent(tmp_path)
    snapshot = agent.tools.runtime_snapshot(allowed_tools=["switch_tool"])
    tool = _SwitchTool(ready=True)
    agent.tools.register(tool)

    result = agent.tools.execute_call(
        {"tool": "switch_tool"},
        allowed_tools=["switch_tool"],
        runtime_snapshot=snapshot,
    )

    assert not result.ok
    assert result.error_code == "TOOL_UNAVAILABLE"
    assert "当前 run" in result.output
    assert tool.executions == 0


def test_execution_rechecks_tool_that_drops_after_snapshot(tmp_path) -> None:
    """LLM: a tool that disappears after model exposure fails before its implementation runs."""
    agent = _agent(tmp_path)
    tool = _SwitchTool(ready=True)
    agent.tools.register(tool)
    snapshot = agent.tools.runtime_snapshot(allowed_tools=["switch_tool"])
    tool.ready = False
    assert {
        spec.name
        for spec in agent.tools.model_visible_specs(runtime_snapshot=snapshot)
    } == {"switch_tool"}

    result = agent.tools.execute_call(
        {"tool": "switch_tool"},
        allowed_tools=["switch_tool"],
        runtime_snapshot=snapshot,
    )

    assert not result.ok
    assert result.error_code == "TOOL_UNAVAILABLE"
    assert tool.executions == 0


def test_list_tools_uses_resolved_remote_owner_type(tmp_path) -> None:
    """LLM: permission_mode comes from owner identity, not a hard-coded main-agent label."""
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=True,
            memory_path=str(tmp_path / "memory.jsonl"),
            my_agent_home=str(tmp_path / "home"),
            my_agent_owner_provider="feishu",
            my_agent_owner_kind="user",
            my_agent_owner_id="user-a",
        ),
        str(tmp_path / "repo"),
    )

    result = agent.tools.execute_call(
        {"tool": "list_tools"},
        allowed_tools=["list_tools"],
    )

    assert result.ok, result.output
    assert json.loads(result.output)["permission_mode"] == "owner_scoped"
