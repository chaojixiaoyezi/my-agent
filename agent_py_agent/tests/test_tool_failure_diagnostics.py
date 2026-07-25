from __future__ import annotations

from pathlib import Path

import pytest

from agent_py_agent.agent.action_protocol import RunScope, ToolCallEnvelope
from agent_py_agent.agent.agent_core.tool_call_archive_record import (
    _attach_gate_and_refs,
)
from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
    _tool_event_payload,
)
from agent_py_agent.agent.tooling.models import (
    BaseTool,
    ToolAvailability,
    ToolExecutionResult,
    ToolFailureStage,
    ToolSpec,
)
from agent_py_agent.agent.tooling.registry_execution import (
    ExecuteRegistryCallParams,
    execute_registry_call,
)
from agent_py_agent.agent.tooling.tool_operation_coordinator import (
    ToolOperationExecutionRequest,
    execute_tool_operation,
)


class _DiagnosticTool(BaseTool):
    def __init__(
        self,
        *,
        result: ToolExecutionResult | None = None,
        available: bool = True,
        raises: bool = False,
    ) -> None:
        self.result = result
        self.available = available
        self.raises = raises
        self.calls = 0
        self.spec = ToolSpec(
            name="diagnostic",
            category="test",
            description="diagnostic test tool",
            use_cases=[],
            avoid_when=[],
            keywords=[],
            parameters={"value": "integer"},
            parameter_schema={"value": {"type": "integer"}},
            required_parameters=["value"],
            effect="read_only",
        )

    def availability(self) -> ToolAvailability:
        if self.available:
            return ToolAvailability.ready()
        return ToolAvailability.unavailable("dependency is not configured")

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        self.calls += 1
        if self.raises:
            raise RuntimeError("handler exploded")
        return self.result or ToolExecutionResult(
            self.spec.name,
            True,
            f"value={params['value']}",
        )


def _call(
    root: Path,
    tool: _DiagnosticTool,
    payload: object,
    *,
    allowed_tools: list[str] | None = None,
) -> ToolExecutionResult:
    return execute_registry_call(
        ExecuteRegistryCallParams(
            payload=payload,
            tools={tool.spec.name: tool},
            workspace_root=root,
            workspace_roots=[root],
            allowed_tools=allowed_tools,
        )
    )


def test_protocol_authorization_validation_and_runtime_gate_are_distinct(
    tmp_path: Path,
) -> None:
    protocol_tool = _DiagnosticTool()
    protocol = _call(tmp_path, protocol_tool, "not-an-object")
    assert protocol.failure_stage == ToolFailureStage.PROTOCOL.value
    assert protocol.handler_executed is False
    assert protocol_tool.calls == 0

    auth_tool = _DiagnosticTool()
    authorization = _call(
        tmp_path,
        auth_tool,
        {"tool": "diagnostic", "value": 1},
        allowed_tools=["list_tools"],
    )
    assert authorization.failure_stage == ToolFailureStage.AUTHORIZATION.value
    assert authorization.handler_executed is False
    assert auth_tool.calls == 0

    validation_tool = _DiagnosticTool()
    validation = _call(
        tmp_path,
        validation_tool,
        {"tool": "diagnostic"},
    )
    assert validation.failure_stage == ToolFailureStage.VALIDATION.value
    assert validation.handler_executed is False
    assert validation_tool.calls == 0

    unavailable_tool = _DiagnosticTool(available=False)
    unavailable = _call(
        tmp_path,
        unavailable_tool,
        {"tool": "diagnostic", "value": 1},
    )
    assert unavailable.failure_stage == ToolFailureStage.RUNTIME_GATE.value
    assert unavailable.handler_executed is False
    assert unavailable_tool.calls == 0


@pytest.mark.parametrize("raises", [False, True])
def test_handler_failures_are_distinguished_from_pre_handler_blocks(
    tmp_path: Path,
    raises: bool,
) -> None:
    explicit_failure = ToolExecutionResult(
        "diagnostic",
        False,
        "dependency failed",
        error_code="TOOL_ERROR",
    )
    tool = _DiagnosticTool(
        result=None if raises else explicit_failure,
        raises=raises,
    )

    result = _call(
        tmp_path,
        tool,
        ToolCallEnvelope(
            call_id="call-diagnostic",
            source="model_tool_call",
            tool_name="diagnostic",
            input={"value": 1},
            scope=RunScope(run_id="run-1", task_id="task-1"),
        ),
    )

    assert result.ok is False
    assert result.failure_stage == ToolFailureStage.EXECUTION.value
    assert result.handler_executed is True
    assert result.duration_ms >= 0
    assert result.result_envelope["tool_execution"] == {
        "handler_executed": True,
        "duration_ms": result.duration_ms,
        "failure_stage": "execution",
    }
    assert (
        result.result_envelope["tool_protocol_v2"]["metadata"]["tool_execution"]
        == result.result_envelope["tool_execution"]
    )


def test_success_records_handler_and_duration_without_failure_stage(
    tmp_path: Path,
) -> None:
    tool = _DiagnosticTool()

    result = _call(
        tmp_path,
        tool,
        {"tool": "diagnostic", "value": 7},
    )

    assert result.ok is True
    assert result.handler_executed is True
    assert result.failure_stage == ""
    assert result.duration_ms >= 0
    assert result.result_envelope["tool_execution"] == {
        "handler_executed": True,
        "duration_ms": result.duration_ms,
    }


def test_side_effect_store_failure_is_persistence_before_handler() -> None:
    invoked = False

    def invoke() -> ToolExecutionResult:
        nonlocal invoked
        invoked = True
        return ToolExecutionResult("write", True, "should not run")

    result = execute_tool_operation(
        ToolOperationExecutionRequest(
            store=None,
            store_required=True,
            owner_id="owner-a",
            run_id="run-1",
            task_id="task-1",
            operation_id="tool_call:call-1",
            tool_name="write",
            args_hash="sha256:test",
            idempotency_key="idem:test",
            idempotency_scope="operation",
            idempotency_namespace="write",
            timeout_seconds=30,
            invoke=invoke,
        )
    )

    assert result.ok is False
    assert result.failure_stage == ToolFailureStage.PERSISTENCE.value
    assert result.handler_executed is False
    assert invoked is False


def test_archive_and_runtime_event_keep_the_same_execution_facts() -> None:
    result = ToolExecutionResult(
        "diagnostic",
        False,
        "failed",
        error_code="TOOL_ERROR",
        handler_executed=True,
        failure_stage=ToolFailureStage.EXECUTION.value,
        duration_ms=17,
        result_envelope={
            "tool_execution": {
                "handler_executed": True,
                "failure_stage": "execution",
                "duration_ms": 17,
            }
        },
    )
    archive: dict[str, object] = {
        "tool": "diagnostic",
        "ok": False,
        "call_id": "call-1",
        "run_id": "run-1",
    }

    _attach_gate_and_refs(archive, result)
    event = _tool_event_payload(archive, {})

    assert archive["failure_stage"] == "execution"
    assert archive["handler_executed"] is True
    assert archive["duration_ms"] == 17
    assert archive["tool_result_envelope"]["tool_execution"] == {
        "handler_executed": True,
        "failure_stage": "execution",
        "duration_ms": 17,
    }
    assert event["failure_stage"] == "execution"
    assert event["handler_executed"] is True
    assert event["duration_ms"] == 17


def test_invalid_or_success_failure_stage_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid tool failure stage"):
        ToolExecutionResult("diagnostic", False, "bad", failure_stage="guessed")
    with pytest.raises(ValueError, match="successful tool result"):
        ToolExecutionResult(
            "diagnostic",
            True,
            "ok",
            failure_stage=ToolFailureStage.EXECUTION.value,
        )
