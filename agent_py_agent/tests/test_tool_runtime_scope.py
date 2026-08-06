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
    ToolHandlerOutcome,
)
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
)


def _execute(agent, tool_name, arguments, *, allowed_tools=None, snapshot=None):
    runtime_snapshot = snapshot or agent.tools.runtime_snapshot(
        allowed_tools=allowed_tools,
        run_id="scope-test-run",
    )
    call = canonical_test_call(runtime_snapshot, tool_name, arguments)
    return agent.tools.execute_tool(
        call,
        write_boundary=None,
        runtime_snapshot=runtime_snapshot,
    ).result


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

    result = _execute(
        agent,
        "list_tools",
        {},
        allowed_tools=["list_tools", "read_file"],
    )

    assert result.ok, result.output
    payload = json.loads(
        result.metadata["handler_details"]["tool_output_policy"]["live_prompt_output"]
    )
    names = {item["name"] for item in payload["tools"]}
    assert names == {"list_tools", "read_file"}


def test_restricted_tool_search_cannot_load_ungranted_deferred_tools(tmp_path) -> None:
    """LLM: search narrows an existing grant and never creates a new tool grant."""
    agent = _agent(tmp_path)

    result = _execute(
        agent,
        "tool_search",
        {
            "query": "create_subagents 创建并管理子代理",
            "load_names": ["create_subagents"],
        },
        allowed_tools=["tool_search"],
    )

    assert result.ok, result.output
    assert result.metadata["handler_details"]["tool_search"]["loaded_tool_names"] == []
    payload = json.loads(result.output)
    assert payload["loaded_for_next_model_call"] == []
    assert payload["not_loaded"] == ["create_subagents"]


class _SwitchTool(BaseTool):
    """LLM: controllable readiness proves snapshot and live recheck semantics without external services."""

    model_spec = make_test_model_spec(
        "switch_tool",
        category="system",
        description="test-only readiness switch",
        keywords=("switch",),
    )
    runtime_policy = make_test_runtime_policy("read_only")

    def __init__(self, *, ready: bool) -> None:
        self.ready = ready
        self.executions = 0

    def availability(self) -> ToolAvailability:
        return (
            ToolAvailability.ready()
            if self.ready
            else ToolAvailability.unavailable("switch is off")
        )

    def execute(self, params) -> ToolHandlerOutcome:
        _ = params
        self.executions += 1
        return ToolHandlerOutcome(self.model_spec.name, True, "executed")


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

    result = _execute(
        agent,
        "analyze_image",
        {"image": "missing.png"},
    )
    assert not result.ok
    assert result.error_code == "TOOL_NOT_IN_RUNTIME_SNAPSHOT"

    unauthorized = _execute(
        agent,
        "analyze_image",
        {"image": "missing.png"},
        allowed_tools=["list_tools"],
    )
    assert unauthorized.error_code == "TOOL_NOT_IN_RUNTIME_SNAPSHOT"
    assert "vision_api_base" not in unauthorized.output


def test_run_snapshot_does_not_expand_when_tool_becomes_ready_later(tmp_path) -> None:
    """LLM: a mid-run readiness change only affects the next run, like 会话运行时 StepContext."""
    agent = _agent(tmp_path)
    tool = _SwitchTool(ready=False)
    agent.tools.register(tool)
    snapshot = agent.tools.runtime_snapshot(
        allowed_tools=["switch_tool"],
        run_id="switch-readiness-run",
    )
    tool.ready = True
    assert agent.tools.specs(runtime_snapshot=snapshot) == []

    result = _execute(
        agent,
        "switch_tool",
        {},
        allowed_tools=["switch_tool"],
        snapshot=snapshot,
    )

    assert not result.ok
    assert result.error_code == "TOOL_NOT_IN_RUNTIME_SNAPSHOT"
    assert tool.executions == 0


def test_run_snapshot_does_not_expand_when_tool_is_registered_later(tmp_path) -> None:
    """LLM: dynamic registration only affects a later run, never the current frozen tool universe."""
    agent = _agent(tmp_path)
    snapshot = agent.tools.runtime_snapshot(
        allowed_tools=["switch_tool"],
        run_id="switch-registration-run",
    )
    tool = _SwitchTool(ready=True)
    agent.tools.register(tool)

    result = _execute(
        agent,
        "switch_tool",
        {},
        allowed_tools=["switch_tool"],
        snapshot=snapshot,
    )

    assert not result.ok
    assert result.error_code == "TOOL_NOT_IN_RUNTIME_SNAPSHOT"
    assert tool.executions == 0


def test_execution_uses_same_frozen_availability_snapshot_seen_by_model(tmp_path) -> None:
    """A mid-run probe change cannot rewrite the already exposed run snapshot."""
    agent = _agent(tmp_path)
    tool = _SwitchTool(ready=True)
    agent.tools.register(tool)
    snapshot = agent.tools.runtime_snapshot(
        allowed_tools=["switch_tool"],
        run_id="switch-frozen-run",
    )
    tool.ready = False
    assert {
        spec.name
        for spec in agent.tools.model_visible_specs(runtime_snapshot=snapshot)
    } == {"switch_tool"}

    result = _execute(
        agent,
        "switch_tool",
        {},
        allowed_tools=["switch_tool"],
        snapshot=snapshot,
    )

    assert result.ok
    assert tool.executions == 1


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

    result = _execute(
        agent,
        "list_tools",
        {},
        allowed_tools=["list_tools"],
    )

    assert result.ok, result.output
    payload = json.loads(
        result.metadata["handler_details"]["tool_output_policy"]["live_prompt_output"]
    )
    assert payload["permission_mode"] == "owner_scoped"
