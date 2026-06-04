from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.tool_guard.call_guardrail import (
    record_tool_guard_observation,
    tool_guardrail_policy,
    tool_guardrail_records,
)
from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from agent_py_agent.agent.tooling import BaseTool, ToolExecutionResult, ToolSpec
from agent_py_agent.agent.tooling.registry_runtime_gate_pipeline import tool_call_gate_decision


def test_runtime_routes_repeated_read_only_successes_through_gate_pipeline() -> None:
    agent = SimpleNamespace()
    params = _params(task_attributes={"readonly_no_progress_threshold": 1})
    payload = {"tool": "list_tools"}

    for _ in range(3):
        record_tool_guard_observation(agent, params, payload, ToolExecutionResult("list_tools", True, "same"))

    decision = tool_call_gate_decision(payload, _call(agent, params))

    assert decision.allowed is False
    assert "TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED" in decision.finding_codes


def test_runtime_no_progress_threshold_zero_is_unlimited() -> None:
    agent = SimpleNamespace()
    params = _params(task_attributes={"readonly_no_progress_threshold": 0})
    payload = {"tool": "list_tools"}

    for _ in range(4):
        record_tool_guard_observation(agent, params, payload, ToolExecutionResult("list_tools", True, "same"))

    decision = tool_call_gate_decision(payload, _call(agent, params))

    assert decision.allowed is True


def test_runtime_repeated_read_guard_resets_after_local_progress() -> None:
    agent = SimpleNamespace()
    params = _params(task_attributes={"readonly_no_progress_threshold": 1})
    read_payload = {"tool": "list_tools"}
    write_payload = {"tool": "write_file", "path": "outputs/source_index.json", "content": "{}"}

    for _ in range(3):
        record_tool_guard_observation(agent, params, read_payload, ToolExecutionResult("list_tools", True, "same"))
    assert tool_call_gate_decision(read_payload, _call(agent, params)).allowed is False

    record_tool_guard_observation(agent, params, write_payload, ToolExecutionResult("write_file", True, "{}"))

    assert tool_call_gate_decision(read_payload, _call(agent, params)).allowed is True


def test_runtime_same_args_same_failure_warns_then_pipeline_blocks_next_call_only() -> None:
    agent = SimpleNamespace()
    params = _params(task_attributes={"repeat_fail_threshold": 3})
    payload = {"tool": "web_search", "query": "same"}
    warnings: list[str] = []

    for _ in range(9):
        warning = record_tool_guard_observation(
            agent,
            params,
            payload,
            ToolExecutionResult("web_search", False, "timeout", error_code="TOOL_TIMEOUT"),
        )
        if warning:
            warnings.append(warning)

    assert len(warnings) == 2
    assert "3 times" in warnings[0] or "3 次" in warnings[0]
    assert "6 times" in warnings[1] or "6 次" in warnings[1]

    decision = tool_call_gate_decision(payload, _call(agent, params))

    assert decision.allowed is False
    assert "TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED" in decision.finding_codes
    assert "change" in decision.recommended_action


def test_runtime_repeat_fail_threshold_zero_is_unlimited_with_fixed_hints() -> None:
    agent = SimpleNamespace()
    params = _params(task_attributes={"repeat_fail_threshold": 0})
    payload = {"tool": "web_search", "query": "same"}
    warnings: list[str] = []

    for _ in range(100):
        warning = record_tool_guard_observation(
            agent,
            params,
            payload,
            ToolExecutionResult("web_search", False, "timeout", error_code="TOOL_TIMEOUT"),
        )
        if warning:
            warnings.append(warning)

    assert len(warnings) == 2
    assert "50 times" in warnings[0] or "50 次" in warnings[0]
    assert "100 times" in warnings[1] or "100 次" in warnings[1]
    assert tool_call_gate_decision(payload, _call(agent, params)).allowed is True


def test_runtime_same_args_different_failure_class_does_not_compound() -> None:
    agent = SimpleNamespace()
    params = _params(task_attributes={"repeat_fail_threshold": 3})
    payload = {"tool": "web_search", "query": "same"}

    for _ in range(3):
        record_tool_guard_observation(
            agent,
            params,
            payload,
            ToolExecutionResult("web_search", False, "timeout", error_code="TOOL_TIMEOUT"),
        )
    for _ in range(3):
        record_tool_guard_observation(
            agent,
            params,
            payload,
            ToolExecutionResult("web_search", False, "permission", error_code="WRITE_FORBIDDEN"),
        )

    assert tool_call_gate_decision(payload, _call(agent, params)).allowed is True


def test_runtime_same_args_read_with_changing_results_is_progress() -> None:
    agent = SimpleNamespace()
    params = _params(task_attributes={"repeat_fail_threshold": 3})
    payload = {"tool": "read_artifact", "artifact_ref": "large-source"}

    for index in range(9):
        record_tool_guard_observation(
            agent,
            params,
            payload,
            ToolExecutionResult("read_artifact", True, f'{{"cursor_after": "{index}", "rows": [{index}]}}'),
        )

    assert tool_call_gate_decision(payload, _call(agent, params)).allowed is True


def test_runtime_boundary_carries_guardrail_records_and_policy() -> None:
    agent = SimpleNamespace(local_store=None)
    params = _params(task_attributes={"repeat_fail_threshold": 7, "terminal_block_enabled": True})
    payload = {"tool": "web_search", "query": "same"}

    record_tool_guard_observation(
        agent,
        params,
        payload,
        ToolExecutionResult("web_search", False, "timeout", error_code="TOOL_TIMEOUT"),
    )

    boundary = write_boundary_with_runtime_ledger(agent, params)

    assert boundary is not None
    assert boundary["tool_guardrail_policy"] == tool_guardrail_policy(params)
    assert boundary["tool_guardrail_records"] == tool_guardrail_records(agent)


def _params(*, task_attributes: dict[str, object] | None = None):
    return SimpleNamespace(
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        task_attributes=task_attributes or {},
        write_boundary=None,
    )


def _call(agent: object, params: object):
    return SimpleNamespace(
        tools={
            "list_tools": _tool("list_tools", "read_only"),
            "web_search": _tool("web_search", "read_only"),
            "read_artifact": _tool("read_artifact", "read_only"),
            "write_file": _tool("write_file", "mutating"),
        },
        workspace_root=Path("/tmp/my-agent-workspace"),
        workspace_roots=[Path("/tmp/my-agent-workspace")],
        path_access_mode="normal",
        path_dangerous_roots=(),
        allowed_tools=None,
        write_boundary=write_boundary_with_runtime_ledger(agent, params),
    )


def _tool(name: str, effect: str) -> BaseTool:
    return _FakeTool(name, effect)


class _FakeTool(BaseTool):
    def __init__(self, name: str, effect: str):
        self.spec = ToolSpec(
            name=name,
            category="test",
            description="test tool",
            use_cases=[],
            avoid_when=[],
            keywords=[],
            parameters={},
            effect=effect,
        )

    def execute(self, params: dict):
        return ToolExecutionResult(self.spec.name, True, "{}")
