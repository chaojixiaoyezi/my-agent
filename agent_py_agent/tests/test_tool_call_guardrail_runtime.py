from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    ToolOperation,
    ToolResult,
    ToolSuccessFacts,
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


def test_repeated_mutating_success_warns_without_changing_permission() -> None:
    agent = _agent()
    params = _params(task_attributes={"repeated_success_hint_threshold": 3})
    payload = {"tool": "write_file", "path": "outputs/result.txt", "content": "same"}
    warnings = [record_tool_guard_observation(
        agent, params, payload, ToolHandlerOutcome("write_file", True, '{"written":4}')
    ) for _ in range(24)]

    assert [index + 1 for index, warning in enumerate(warnings) if warning] == [3, 6, 12, 24]
    assert all(len(warning) <= 500 for warning in warnings)
    assert "结果相同不代表没有副作用" in warnings[2]
    decision = _decision(agent, params, payload)
    assert decision.allowed is True
    assert decision.resolved_effect == "mutating"


def test_shell_repeat_observation_uses_real_policy_without_executing_commands(tmp_path) -> None:
    from agent_py_agent.agent.tooling.models import tool_effect_for_runtime_policy
    from agent_py_agent.agent.tooling.shell import ShellTool

    shell = ShellTool(tmp_path)
    agent = _agent()
    agent.tools["run_command"] = shell
    params = _params(task_attributes={"repeated_success_hint_threshold": 5})
    snapshot = _snapshot(agent, params)
    arguments = {"command": "cd . && stat source.txt && grep -n marker source.txt | head -3"}
    assert tool_effect_for_runtime_policy(shell.runtime_policy, arguments) == "mutating"
    warnings = []
    for index in range(57):
        call = canonical_test_call(snapshot, "run_command", arguments, call_id=f"provider-{index}")
        result = ToolResult.succeeded(
            call, '{"return_code":0,"stdout":"same inspection result","stderr":""}',
            facts=ToolSuccessFacts(effect_outcome="confirmed"),
        )
        warnings.append(_record_tool_guard_observation(agent, params, call, result))
        record_tool_guard_observation(
            agent, params, {"tool": "read_artifact", "artifact_ref": str(index)},
            ToolHandlerOutcome("read_artifact", True, f"page {index}"),
        )
    assert [index + 1 for index, warning in enumerate(warnings) if warning] == [5, 10, 20, 40]
    assert tool_effect_for_runtime_policy(shell.runtime_policy, arguments) == "mutating"


def test_repeated_success_observation_survives_interleaved_reads() -> None:
    agent = _agent()
    params = _params(task_attributes={"repeated_success_hint_threshold": 3})
    payload = {"tool": "write_file", "path": "outputs/result.txt", "content": "same"}
    warnings = []
    for index in range(3):
        warnings.append(record_tool_guard_observation(
            agent, params, payload, ToolHandlerOutcome("write_file", True, "same result")
        ))
        record_tool_guard_observation(
            agent, params, {"tool": "read_artifact", "artifact_ref": str(index)},
            ToolHandlerOutcome("read_artifact", True, f"page {index}"),
        )
    assert [bool(warning) for warning in warnings] == [False, False, True]
    records = tool_guardrail_records(agent)
    assert len(records) == 6
    assert all(record["observed_success"] is True for record in records)
    assert all(record["result_hash"] for record in records)


@pytest.mark.parametrize("changed_field", ["arguments", "result"])
def test_repeated_success_different_parameters_or_results_do_not_compound(changed_field) -> None:
    agent = _agent()
    params = _params(task_attributes={"repeated_success_hint_threshold": 3})
    for index in range(10):
        payload = {"tool": "write_file", "path": "outputs/result.txt", "content": str(index) if changed_field == "arguments" else "same"}
        output = str(index) if changed_field == "result" else "same result"
        assert not record_tool_guard_observation(
            agent, params, payload, ToolHandlerOutcome("write_file", True, output)
        )


@pytest.mark.parametrize("reset_kind", ["changed_result", "failure", "unknown", "not_executed"])
def test_repeated_success_segment_resets_on_new_result_or_unconfirmed_execution(reset_kind) -> None:
    agent = _agent()
    params = _params(task_attributes={"repeated_success_hint_threshold": 3})
    payload = {"tool": "write_file", "path": "outputs/result.txt", "content": "same"}
    for _ in range(2):
        assert not record_tool_guard_observation(
            agent, params, payload, ToolHandlerOutcome("write_file", True, "same result")
        )
    call = _canonical_call(agent, params, payload)
    if reset_kind == "failure":
        result = ToolResult.failed(call, "same result", error_code="TOOL_TIMEOUT", failure_stage="execution")
    else:
        result = ToolResult.succeeded(
            call, "new result" if reset_kind == "changed_result" else "same result",
            facts=ToolSuccessFacts(
                effect_outcome="unknown" if reset_kind == "unknown" else "confirmed",
                handler_executed=reset_kind != "not_executed",
            ),
        )
    assert not _record_tool_guard_observation(agent, params, call, result)
    for _ in range(2):
        assert not record_tool_guard_observation(
            agent, params, payload, ToolHandlerOutcome("write_file", True, "same result")
        )
    assert record_tool_guard_observation(
        agent, params, payload, ToolHandlerOutcome("write_file", True, "same result")
    )


def test_repeated_success_zero_disables_only_new_observation() -> None:
    agent = _agent()
    params = _params(task_attributes={"repeated_success_hint_threshold": 0})
    payload = {"tool": "write_file", "path": "outputs/result.txt", "content": "same"}
    for _ in range(20):
        assert not record_tool_guard_observation(
            agent, params, payload, ToolHandlerOutcome("write_file", True, "same result")
        )
    assert _decision(agent, params, payload).allowed is True


@pytest.mark.parametrize("threshold", [1, 5])
def test_repeated_success_records_remain_bounded(threshold) -> None:
    agent = _agent()
    params = _params(task_attributes={"repeated_success_hint_threshold": threshold})
    warnings = []
    for index in range(400):
        warnings.append(record_tool_guard_observation(
            agent, params,
            {"tool": "write_file", "path": "outputs/result.txt", "content": "same"},
            ToolHandlerOutcome("write_file", True, "same result"),
        ))
    assert len(tool_guardrail_records(agent)) == 256
    assert sum(bool(warning) for warning in warnings) <= 9


def test_repeated_success_observation_is_run_scoped_even_when_agent_reused() -> None:
    agent = _agent()
    params = _params(task_attributes={"repeated_success_hint_threshold": 3})
    payload = {"tool": "write_file", "path": "outputs/result.txt", "content": "same"}
    call = _canonical_call(agent, params, payload)
    for run_id in ["one", "one", "two", "two"]:
        scoped = replace(call, run_id=run_id)
        result = ToolResult.succeeded(scoped, "same result")
        assert not _record_tool_guard_observation(agent, params, scoped, result)
    scoped = replace(call, run_id="two")
    assert _record_tool_guard_observation(agent, params, scoped, ToolResult.succeeded(scoped, "same result"))


@pytest.mark.parametrize("operation_status,replayed,effect", [
    ("unknown", False, "unknown"), ("succeeded", True, "confirmed"),
])
def test_repeated_success_excludes_unknown_and_replayed_operation(operation_status, replayed, effect) -> None:
    agent = _agent()
    params = _params(task_attributes={"repeated_success_hint_threshold": 2})
    payload = {"tool": "write_file", "path": "outputs/result.txt", "content": "same"}
    call = _canonical_call(agent, params, payload)
    operation = ToolOperation(
        operation_id=call.operation_id, idempotency_key=call.idempotency_key,
        args_hash=call.args_hash, status=operation_status, handler_executed=True,
        effect_outcome=effect, replayed=replayed,
    )
    result = ToolResult.succeeded(call, "same result", facts=ToolSuccessFacts(operation=operation))
    for _ in range(5):
        assert not _record_tool_guard_observation(agent, params, call, result)
    assert all(record["observed_success"] is False for record in tool_guardrail_records(agent))


def test_repeated_success_warning_reaches_native_guidance_and_ledger_projection() -> None:
    from agent_py_agent.agent.agent_core.tool_ir_guidance import unforwarded_runtime_guidance

    agent = _agent()
    params = _params(task_attributes={"repeated_success_hint_threshold": 2})
    payload = {"tool": "write_file", "path": "outputs/result.txt", "content": "same"}
    for _ in range(2):
        hint = record_tool_guard_observation(
            agent, params, payload, ToolHandlerOutcome("write_file", True, "same result")
        )
    context = [f"[tool-loop-guardrail-hint]\n{hint}"]
    seen = set()
    assert unforwarded_runtime_guidance(context, seen) == context
    assert unforwarded_runtime_guidance(context, seen) == []
    boundary = write_boundary_with_runtime_ledger(agent, params)
    assert boundary["tool_guardrail_records"] == tool_guardrail_records(agent)
    assert boundary["tool_guardrail_records"][-1]["repeated_success_hint_count"] == 2
    assert boundary["tool_guardrail_policy"]["repeated_success_hint_threshold"] == 2


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
