from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_call_archive_record import (
    _attach_gate_and_refs,
)
from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
    _tool_event_payload,
)
from agent_py_agent.agent.backends.tool_protocol_adapter import (
    ProviderToolCallRequest,
    canonical_tool_calls_from_response,
)
from agent_py_agent.agent.tooling.models import (
    BaseTool,
    ToolAvailability,
    ToolFailureStage,
    ToolHandlerOutcome,
)
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolFailureFacts,
    ToolProtocolSnapshot,
    ToolResult,
)
from agent_py_agent.agent.tooling.tool_operation_coordinator import (
    ToolOperationExecutionRequest,
    execute_tool_operation,
)
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_tools,
)


class _DiagnosticTool(BaseTool):
    def __init__(
        self,
        *,
        result: ToolHandlerOutcome | None = None,
        available: bool = True,
        raises: bool = False,
    ) -> None:
        self.result = result
        self.available = available
        self.raises = raises
        self.calls = 0
        self.model_spec = make_test_model_spec(
            "diagnostic",
            description="diagnostic test tool",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        )
        self.runtime_policy = make_test_runtime_policy("read_only")

    def availability(self) -> ToolAvailability:
        if self.available:
            return ToolAvailability.ready()
        return ToolAvailability.unavailable("dependency is not configured")

    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        self.calls += 1
        if self.raises:
            raise RuntimeError("handler exploded")
        return self.result or ToolHandlerOutcome(
            self.model_spec.name,
            True,
            f"value={params['value']}",
        )


def _call(
    root: Path,
    tool: _DiagnosticTool,
    arguments: dict[str, object],
    *,
    allowed_tools: list[str] | None = None,
):
    return execute_canonical_test_call(
        root,
        tools={tool.model_spec.name: tool},
        tool_name=tool.model_spec.name,
        arguments=arguments,
        allowed_tools=allowed_tools,
    ).result


def test_protocol_authorization_validation_and_runtime_gate_are_distinct(
    tmp_path: Path,
) -> None:
    protocol_tool = _DiagnosticTool()
    snapshot = runtime_snapshot_for_tools({protocol_tool.model_spec.name: protocol_tool})
    protocol = canonical_tool_calls_from_response(
        ProviderToolCallRequest(
            response=SimpleNamespace(
                text="",
                tool_use_blocks=[
                    {
                        "id": "protocol-call",
                        "name": "diagnostic",
                        "input": "not-an-object",
                    }
                ],
            ),
            protocol=ToolProtocolSnapshot(
                run_id=snapshot.run_id,
                source_protocol="native",
                capability=ProviderToolCapability(
                    provider="test",
                    endpoint="local://test",
                    model="test-model",
                    stream=False,
                    native_supported=True,
                    evidence="test-contract",
                ),
            ),
            runtime_snapshot=snapshot,
            turn_id="test-turn",
            attempt_id="test-attempt",
        )
    )
    assert protocol.calls == ()
    assert protocol.violations[0].code == "TOOL_INVALID_ARGUMENTS"
    assert protocol_tool.calls == 0

    auth_tool = _DiagnosticTool()
    authorization = _call(
        tmp_path,
        auth_tool,
        {"value": 1},
        allowed_tools=["list_tools"],
    )
    assert authorization.failure_stage == ToolFailureStage.AUTHORIZATION.value
    assert authorization.handler_executed is False
    assert auth_tool.calls == 0

    validation_tool = _DiagnosticTool()
    validation = _call(
        tmp_path,
        validation_tool,
        {},
    )
    assert validation.failure_stage == ToolFailureStage.VALIDATION.value
    assert validation.handler_executed is False
    assert validation_tool.calls == 0

    unavailable_tool = _DiagnosticTool(available=False)
    unavailable = _call(
        tmp_path,
        unavailable_tool,
        {"value": 1},
    )
    assert unavailable.failure_stage == ToolFailureStage.RUNTIME_GATE.value
    assert unavailable.handler_executed is False
    assert unavailable_tool.calls == 0


@pytest.mark.parametrize("raises", [False, True])
def test_handler_failures_are_distinguished_from_pre_handler_blocks(
    tmp_path: Path,
    raises: bool,
) -> None:
    explicit_failure = ToolHandlerOutcome(
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
        {"value": 1},
    )

    assert result.ok is False
    assert result.failure_stage == ToolFailureStage.EXECUTION.value
    assert result.handler_executed is True
    assert result.duration_ms >= 0
    assert result.status == "failed"


def test_success_records_handler_and_duration_without_failure_stage(
    tmp_path: Path,
) -> None:
    tool = _DiagnosticTool()

    result = _call(
        tmp_path,
        tool,
        {"value": 7},
    )

    assert result.ok is True
    assert result.handler_executed is True
    assert result.failure_stage == ""
    assert result.duration_ms >= 0
    assert result.status == "succeeded"


def test_side_effect_store_failure_is_persistence_before_handler() -> None:
    invoked = False

    def invoke() -> ToolHandlerOutcome:
        nonlocal invoked
        invoked = True
        return ToolHandlerOutcome("write", True, "should not run")

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
    call = canonical_history_call("diagnostic", {}, call_id="call-1")
    result = ToolResult.failed(
        call,
        "failed",
        error_code="TOOL_ERROR",
        failure_stage=ToolFailureStage.EXECUTION.value,
        facts=ToolFailureFacts(handler_executed=True, duration_ms=17),
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
    assert "tool_result_envelope" not in archive
    assert event["failure_stage"] == "execution"
    assert event["handler_executed"] is True
    assert event["duration_ms"] == 17


def test_invalid_or_success_failure_stage_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid tool failure stage"):
        ToolHandlerOutcome("diagnostic", False, "bad", failure_stage="guessed")
    with pytest.raises(ValueError, match="successful tool result"):
        ToolHandlerOutcome(
            "diagnostic",
            True,
            "ok",
            failure_stage=ToolFailureStage.EXECUTION.value,
        )
