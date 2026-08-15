from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.tool_guard.call_guardrail import (
    record_tool_guard_observation as _record_tool_guard_observation,
)
from agent_py_agent.agent.agent_core.tool_guard.call_guardrail import (
    tool_guardrail_policy,
    tool_guardrail_records,
)
from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from agent_py_agent.agent.tooling import BaseTool, ToolHandlerOutcome
from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest
from agent_py_agent.agent.tooling.runtime_contracts import (
    ToolCall,
    ToolFailureFacts,
    ToolResult,
)
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_tools,
)


def _snapshot(agent: object, params: object):
    snapshot = getattr(params, "tool_runtime_snapshot", None)
    if snapshot is None:
        snapshot = runtime_snapshot_for_tools(agent.tools, run_id="run-1")
        params.tool_runtime_snapshot = snapshot
    return snapshot


def _canonical_call(agent: object, params: object, payload: dict[str, object]) -> ToolCall:
    arguments = dict(payload)
    tool_name = str(arguments.pop("tool"))
    arguments.pop("call_id", None)
    return canonical_test_call(_snapshot(agent, params), tool_name, arguments)


def record_tool_guard_observation(
    agent: object,
    params: object,
    payload: dict[str, object],
    outcome: ToolHandlerOutcome,
) -> str:
    call = _canonical_call(agent, params, payload)
    result = (
        ToolResult.succeeded(call, outcome.output)
        if outcome.ok
        else ToolResult.failed(
            call,
            outcome.output,
            error_code=outcome.error_code,
            failure_stage="execution",
            facts=ToolFailureFacts(handler_executed=True),
        )
    )
    return _record_tool_guard_observation(agent, params, call, result)


def _decision(agent: object, params: object, payload: dict[str, object]):
    call = _canonical_call(agent, params, payload)
    root = Path("/tmp/my-agent-workspace")
    return ActionPolicy().decide(
        ActionPolicyRequest(
            call=call,
            runtime_snapshot=_snapshot(agent, params),
            workspace_root=root,
            workspace_roots=(root,),
            write_boundary=write_boundary_with_runtime_ledger(agent, params),
        )
    )


def test_runtime_routes_repeated_read_only_successes_through_gate_pipeline() -> None:
    agent = _agent()
    params = _params(task_attributes={"readonly_no_progress_threshold": 1})
    payload = {"tool": "list_tools"}

    for _ in range(3):
        record_tool_guard_observation(
            agent, params, payload, ToolHandlerOutcome("list_tools", True, "same")
        )

    decision = _decision(agent, params, payload)

    assert decision.allowed is False
    assert "TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED" in decision.reason_codes


def test_runtime_no_progress_threshold_zero_is_unlimited() -> None:
    agent = _agent()
    params = _params(task_attributes={"readonly_no_progress_threshold": 0})
    payload = {"tool": "list_tools"}

    for _ in range(4):
        record_tool_guard_observation(
            agent, params, payload, ToolHandlerOutcome("list_tools", True, "same")
        )

    decision = _decision(agent, params, payload)

    assert decision.allowed is True


def test_runtime_repeated_read_guard_resets_after_local_progress() -> None:
    agent = _agent()
    params = _params(task_attributes={"readonly_no_progress_threshold": 1})
    read_payload = {"tool": "list_tools"}
    write_payload = {"tool": "write_file", "path": "outputs/source_index.json", "content": "{}"}

    for _ in range(3):
        record_tool_guard_observation(
            agent, params, read_payload, ToolHandlerOutcome("list_tools", True, "same")
        )
    blocked = _decision(agent, params, read_payload)
    assert blocked.allowed is False
    assert "TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED" in blocked.reason_codes

    record_tool_guard_observation(
        agent, params, write_payload, ToolHandlerOutcome("write_file", True, "{}")
    )

    assert _decision(agent, params, read_payload).allowed is True


def test_runtime_same_args_same_failure_warns_then_pipeline_blocks_next_call_only() -> None:
    agent = _agent()
    params = _params(task_attributes={"repeat_fail_threshold": 3})
    payload = {"tool": "web_search", "query": "same"}
    warnings: list[str] = []

    for _ in range(9):
        warning = record_tool_guard_observation(
            agent,
            params,
            payload,
            ToolHandlerOutcome("web_search", False, "timeout", error_code="TOOL_TIMEOUT"),
        )
        if warning:
            warnings.append(warning)

    assert len(warnings) == 2
    assert "3 times" in warnings[0] or "3 次" in warnings[0]
    assert "6 times" in warnings[1] or "6 次" in warnings[1]

    decision = _decision(agent, params, payload)

    assert decision.allowed is False
    assert "TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED" in decision.reason_codes


def test_runtime_repeat_fail_threshold_zero_is_unlimited_with_fixed_hints() -> None:
    agent = _agent()
    params = _params(task_attributes={"repeat_fail_threshold": 0})
    payload = {"tool": "web_search", "query": "same"}
    warnings: list[str] = []

    for _ in range(100):
        warning = record_tool_guard_observation(
            agent,
            params,
            payload,
            ToolHandlerOutcome("web_search", False, "timeout", error_code="TOOL_TIMEOUT"),
        )
        if warning:
            warnings.append(warning)

    assert len(warnings) == 2
    assert "50 times" in warnings[0] or "50 次" in warnings[0]
    assert "100 times" in warnings[1] or "100 次" in warnings[1]
    assert _decision(agent, params, payload).allowed is True


def test_runtime_same_args_different_failure_class_does_not_compound() -> None:
    agent = _agent()
    params = _params(task_attributes={"repeat_fail_threshold": 3})
    payload = {"tool": "web_search", "query": "same"}

    for _ in range(3):
        record_tool_guard_observation(
            agent,
            params,
            payload,
            ToolHandlerOutcome("web_search", False, "timeout", error_code="TOOL_TIMEOUT"),
        )
    for _ in range(3):
        record_tool_guard_observation(
            agent,
            params,
            payload,
            ToolHandlerOutcome("web_search", False, "permission", error_code="WRITE_FORBIDDEN"),
        )

    assert _decision(agent, params, payload).allowed is True


def test_runtime_same_args_read_with_changing_results_is_progress() -> None:
    agent = _agent()
    params = _params(task_attributes={"repeat_fail_threshold": 3})
    payload = {"tool": "read_artifact", "artifact_ref": "large-source"}

    for index in range(9):
        record_tool_guard_observation(
            agent,
            params,
            payload,
            ToolHandlerOutcome(
                "read_artifact", True, f'{{"cursor_after": "{index}", "rows": [{index}]}}'
            ),
        )

    assert _decision(agent, params, payload).allowed is True


def test_runtime_boundary_carries_guardrail_records_and_policy() -> None:
    agent = _agent()
    params = _params(task_attributes={"repeat_fail_threshold": 7, "terminal_block_enabled": True})
    payload = {"tool": "web_search", "query": "same"}

    record_tool_guard_observation(
        agent,
        params,
        payload,
        ToolHandlerOutcome("web_search", False, "timeout", error_code="TOOL_TIMEOUT"),
    )

    boundary = write_boundary_with_runtime_ledger(agent, params)

    assert boundary is not None
    assert boundary["tool_guardrail_policy"] == tool_guardrail_policy(params)
    assert boundary["tool_guardrail_records"] == tool_guardrail_records(agent)


def test_runtime_guardrail_ignores_provider_call_id_for_same_tool_input() -> None:
    agent = _agent()
    params = _params(task_attributes={"readonly_no_progress_threshold": 1})

    for index in range(3):
        payload = {"tool": "list_tools", "call_id": f"provider-call-{index}"}
        record_tool_guard_observation(
            agent,
            params,
            payload,
            ToolHandlerOutcome("list_tools", True, "same"),
        )

    next_payload = {"tool": "list_tools", "call_id": "provider-call-next"}
    decision = _decision(agent, params, next_payload)

    assert decision.allowed is False
    assert "TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED" in decision.reason_codes


def test_runtime_task_progress_read_is_guarded_but_update_remains_mutating() -> None:
    agent = _agent()
    params = _params(task_attributes={"readonly_no_progress_threshold": 1})
    read_payload = {"tool": "task_progress", "action": "read"}

    for _ in range(3):
        record_tool_guard_observation(
            agent,
            params,
            read_payload,
            ToolHandlerOutcome("task_progress", True, '{"summary":"same"}'),
        )

    assert _decision(agent, params, read_payload).allowed is False

    update_payload = {
        "tool": "task_progress",
        "action": "update",
        "summary": "new checkpoint",
    }
    record_tool_guard_observation(
        agent,
        params,
        update_payload,
        ToolHandlerOutcome("task_progress", True, '{"summary":"new checkpoint"}'),
    )

    assert _decision(agent, params, read_payload).allowed is True


def _params(*, task_attributes: dict[str, object] | None = None):
    return SimpleNamespace(
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        task_attributes=task_attributes or {},
        write_boundary=None,
    )


def _agent():
    task_progress = _tool(
        "task_progress",
        "mutating",
        effect_by_parameter=(
            (
                "action",
                (("", "read_only"), ("read", "read_only"), ("update", "mutating")),
            ),
        ),
    )
    return SimpleNamespace(
        local_store=None,
        tools={
            "list_tools": _tool("list_tools", "read_only"),
            "web_search": _tool("web_search", "read_only"),
            "read_artifact": _tool("read_artifact", "read_only"),
            "write_file": _tool("write_file", "mutating"),
            "task_progress": task_progress,
        },
    )


def _tool(
    name: str,
    effect: str,
    *,
    effect_by_parameter: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (),
) -> BaseTool:
    return _FakeTool(name, effect, effect_by_parameter=effect_by_parameter)


class _FakeTool(BaseTool):
    def __init__(
        self,
        name: str,
        effect: str,
        *,
        effect_by_parameter: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (),
    ):
        properties = {
            "list_tools": {},
            "web_search": {"query": {"type": "string"}},
            "read_artifact": {"artifact_ref": {"type": "string"}},
            "write_file": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "task_progress": {
                "action": {"type": "string"},
                "summary": {"type": "string"},
            },
        }[name]
        self.model_spec = make_test_model_spec(
            name,
            description="test tool",
            input_schema={
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            },
        )
        self.runtime_policy = make_test_runtime_policy(
            effect,
            effect_by_parameter=effect_by_parameter,
        )

    def execute(self, params: dict):
        return ToolHandlerOutcome(self.model_spec.name, True, "{}")
