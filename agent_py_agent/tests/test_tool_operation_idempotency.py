from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.contracts.idempotency import operation_idempotency_key
from agent_py_agent.agent.local_storage import (
    LocalStore,
    ToolOperationClaimRequest,
    ToolOperationCompletionRequest,
    ToolOperationHolder,
    ToolOperationOwnershipError,
    new_tool_operation_holder,
)
from agent_py_agent.agent.tooling.models import (
    BaseTool,
    ToolFailureStage,
    ToolHandlerOutcome,
    ToolOperationReconciliation,
)
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult, tool_arguments_hash
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
)


@dataclass(frozen=True)
class _CallSpec:
    tool_name: str
    arguments: dict[str, object]
    run_id: str
    call_id: str
    attempt_id: str = ""

    @property
    def operation_id(self) -> str:
        identifier = (
            f"{self.attempt_id}:{self.call_id}" if self.attempt_id else self.call_id
        )
        return f"tool_call:{identifier}"

    @property
    def idempotency_key(self) -> str:
        return operation_idempotency_key(
            self.attempt_id or self.run_id,
            self.operation_id,
        )


class _CountingTool(BaseTool):
    def __init__(
        self,
        *,
        result: ToolHandlerOutcome | None = None,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
    ):
        self.calls = 0
        self.result = result
        self.started = started
        self.release = release
        self.model_spec = make_test_model_spec(
            "counting_write",
            description="count one side effect",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        )
        self.runtime_policy = make_test_runtime_policy("mutating")

    def execute(self, params):
        self.calls += 1
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            assert self.release.wait(timeout=5)
        return self.result or ToolHandlerOutcome(
            "counting_write",
            True,
            f"completed:{params['value']}",
            result_envelope={"domain": {"value": params["value"]}},
        )


class _BusinessReconcilingTool(_CountingTool):
    def __init__(self, reconciliation: ToolOperationReconciliation):
        super().__init__(
            result=ToolHandlerOutcome(
                "counting_write",
                False,
                "provider timed out",
                error_code="TOOL_TIMEOUT",
            )
        )
        self.runtime_policy = replace(
            self.runtime_policy,
            idempotency_policy=replace(
                self.runtime_policy.idempotency_policy,
                scope="business",
            ),
        )
        self.reconciliation = reconciliation

    def business_idempotency_key(self, params):
        return f"business-count:{params['value']}"

    def reconcile_operation(self, params, context):
        _ = (params, context)
        return self.reconciliation

    def execute(self, params):
        self.calls += 1
        if self.calls == 1:
            return self.result
        return ToolHandlerOutcome(
            "counting_write",
            True,
            f"completed:{params['value']}",
        )


class _BusinessCountingTool(_CountingTool):
    def __init__(self) -> None:
        super().__init__()
        self.runtime_policy = replace(
            self.runtime_policy,
            idempotency_policy=replace(
                self.runtime_policy.idempotency_policy,
                scope="business",
            ),
        )

    def business_idempotency_key(self, params):
        return f"business-count:{params['value']}"


class _MixedActionTool(BaseTool):
    def __init__(self) -> None:
        self.calls = 0
        self.model_spec = make_test_model_spec(
            "mixed_action",
            description="one tool with read and write actions",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["inspect", "update"]},
                    "value": {"type": "integer"},
                },
                "required": ["action", "value"],
                "additionalProperties": False,
            },
        )
        self.runtime_policy = make_test_runtime_policy(
            "mutating",
            effect_by_parameter=((
                "action",
                (("inspect", "read_only"), ("update", "mutating")),
            ),),
        )

    def execute(self, params):
        self.calls += 1
        return ToolHandlerOutcome(
            "mixed_action",
            True,
            f"{params['action']}:{params['value']}",
        )


def test_exact_operation_replays_saved_result_without_second_effect(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _CountingTool()
    registry = _registry(tmp_path, store, tool)
    call = _call("run-1", "call-1", 7)

    first = _execute(registry, call)
    reopened = LocalStore(tmp_path / "local.db", enable_fts=False)
    replay_tool = _CountingTool()
    replay_registry = _registry(tmp_path, reopened, replay_tool)
    replay = _execute(replay_registry, call)

    assert first.ok is True
    assert replay.ok is True
    assert replay.output == first.output
    assert replay.metadata["handler_details"]["domain"] == {"value": 7}
    assert replay.operation is not None and replay.operation.replayed is True
    assert first.operation is not None
    assert first.operation.result_ref == "tool-operation://run-1/tool_call:call-1"
    assert replay.operation.result_ref == first.operation.result_ref
    stored = store.get_tool_operation(
        owner_id="owner-a",
        run_id="run-1",
        operation_id="tool_call:call-1",
    )
    assert stored is not None
    assert stored.result_ref == first.operation.result_ref
    assert stored.result["result_ref"] == first.operation.result_ref
    assert first.handler_executed is True
    assert replay.handler_executed is False
    assert tool.calls == 1
    assert replay_tool.calls == 0


def test_same_operation_with_changed_input_is_rejected(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _CountingTool()
    registry = _registry(tmp_path, store, tool)

    first = _execute(registry, _call("run-1", "call-1", 1))
    changed = _execute(registry, _call("run-1", "call-1", 2))

    assert first.ok is True
    assert changed.ok is False
    assert changed.error_code == "TOOL_OPERATION_IDENTITY_CONFLICT"
    assert tool.calls == 1


def test_equal_arguments_in_new_operations_are_both_legal(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _CountingTool()
    registry = _registry(tmp_path, store, tool)

    first = _execute(registry, _call("run-1", "call-1", 1))
    second = _execute(registry, _call("run-1", "call-2", 1))
    other_run = _execute(registry, _call("run-2", "call-1", 1))

    assert first.ok and second.ok and other_run.ok
    assert tool.calls == 3


def test_provider_call_id_can_repeat_in_separate_runtime_attempts(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _CountingTool()
    registry = _registry(tmp_path, store, tool)

    first_call = _call(
        "run-1", "provider-call-1", 1, attempt_id="attempt-1"
    )
    second_call = _call(
        "run-1", "provider-call-1", 2, attempt_id="attempt-2"
    )
    first = _execute(registry, first_call)
    second = _execute(registry, second_call)

    assert first.ok and second.ok
    assert first_call.operation_id == "tool_call:attempt-1:provider-call-1"
    assert second_call.operation_id == "tool_call:attempt-2:provider-call-1"
    assert tool.calls == 2
    records = store.list_tool_operations(owner_id="owner-a", run_id="run-1")
    assert {record.operation_id for record in records} == {
        "tool_call:attempt-1:provider-call-1",
        "tool_call:attempt-2:provider-call-1",
    }


def test_same_provider_call_id_still_conflicts_inside_one_runtime_attempt(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _CountingTool()
    registry = _registry(tmp_path, store, tool)

    first = _execute(
        registry,
        _call("run-1", "provider-call-1", 1, attempt_id="attempt-1"),
    )
    changed = _execute(
        registry,
        _call("run-1", "provider-call-1", 2, attempt_id="attempt-1"),
    )

    assert first.ok is True
    assert changed.error_code == "TOOL_OPERATION_IDENTITY_CONFLICT"
    assert tool.calls == 1


def test_read_only_variant_of_mixed_action_tool_bypasses_effect_ledger(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _MixedActionTool()
    registry = _registry(tmp_path, store, tool)

    first = _execute(
        registry,
        _mixed_call("run-1", "provider-call-1", "inspect", 1),
    )
    second = _execute(
        registry,
        _mixed_call("run-1", "provider-call-1", "inspect", 2),
    )

    assert first.ok and second.ok
    assert tool.calls == 2
    assert store.list_tool_operations(owner_id="owner-a", run_id="run-1") == []


def test_explicit_boundary_cannot_lower_runtime_declared_effect(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _MixedActionTool()
    registry = _registry(tmp_path, store, tool)

    result = _execute(
        registry,
        _mixed_call("run-1", "provider-call-1", "update", 1),
        write_boundary={"tool_effects": {"mixed_action": "read_only"}},
    )

    assert result.ok is True
    assert result.metadata["action_decision"]["resolved_effect"] == "mutating"
    assert result.operation is not None
    assert tool.calls == 1


def test_concurrent_same_operation_never_enters_handler_twice(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    started = threading.Event()
    release = threading.Event()
    tool = _CountingTool(started=started, release=release)
    registry = _registry(tmp_path, store, tool)
    call = _call("run-1", "call-1", 1)
    results: list[ToolHandlerOutcome] = []

    first = threading.Thread(
        target=lambda: results.append(_execute(registry, call))
    )
    first.start()
    assert started.wait(timeout=5)
    second = _execute(registry, call)
    release.set()
    first.join(timeout=5)

    assert not first.is_alive()
    assert tool.calls == 1
    assert second.ok is False
    assert second.error_code == "TOOL_OPERATION_IN_FLIGHT"
    assert results[0].ok is True


def test_dead_holder_becomes_unknown_and_is_not_reexecuted(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    call = _call("run-1", "call-1", 1)
    store.claim_tool_operation(
        ToolOperationClaimRequest(
            owner_id="owner-a",
            run_id="run-1",
            task_id="run-1",
            operation_id=call.operation_id,
            tool="counting_write",
            args_hash=tool_arguments_hash({"value": 1}),
            idempotency_key=call.idempotency_key,
            idempotency_scope="operation",
            idempotency_namespace="counting_write",
            holder=ToolOperationHolder(
                holder_id="dead-holder",
                host=socket.gethostname(),
                pid=999_999_999,
                process_start_token="not-running",
            ),
            lease_expires_at=time.time() + 3600,
        )
    )
    tool = _CountingTool()

    registry = _registry(tmp_path, store, tool)
    result = _execute(registry, call)

    assert result.ok is False
    assert result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert tool.calls == 0
    record = store.get_tool_operation(
        owner_id="owner-a",
        run_id="run-1",
        operation_id=call.operation_id,
    )
    assert record is not None and record.status == "unknown"


def test_expired_lease_becomes_unknown_even_if_holder_process_is_live(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    holder = new_tool_operation_holder()
    first = _claim_request(holder=holder, now=100.0, lease_expires_at=200.0)
    assert store.claim_tool_operation(first).action == "execute"

    second = _claim_request(
        holder=new_tool_operation_holder(),
        now=201.0,
        lease_expires_at=400.0,
    )
    claim = store.claim_tool_operation(second)

    assert claim.action == "unknown"
    assert claim.record.status == "unknown"


def test_failed_result_is_replayed_exactly(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    failure = ToolHandlerOutcome(
        "counting_write",
        False,
        "provider unavailable",
        error_code="TOOL_RATE_LIMIT_EXCEEDED",
        effect_outcome="not_started",
    )
    tool = _CountingTool(result=failure)
    registry = _registry(tmp_path, store, tool)
    call = _call("run-1", "call-1", 1)

    first = _execute(registry, call)
    replay = _execute(registry, call)

    assert first.ok is False and replay.ok is False
    assert replay.error_code == first.error_code
    assert replay.retryable == first.retryable
    assert replay.recommended_action == first.recommended_action
    assert replay.output == first.output
    assert first.failure_stage == ToolFailureStage.EXECUTION.value
    assert first.handler_executed is True
    assert replay.failure_stage == ToolFailureStage.EFFECT_RECONCILIATION.value
    assert replay.handler_executed is False
    assert replay.metadata["handler_details"]["tool_operation"]["original_tool_execution"][
        "failure_stage"
    ] == ToolFailureStage.EXECUTION.value
    assert replay.metadata["handler_details"]["tool_operation"]["original_tool_execution"][
        "handler_executed"
    ] is True
    assert tool.calls == 1


def test_started_generic_side_effect_failure_is_unknown_without_not_started_proof(
    tmp_path,
):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    failure = ToolHandlerOutcome(
        "counting_write",
        False,
        "provider failed after accepting the request",
        error_code="TOOL_ERROR",
    )
    tool = _CountingTool(result=failure)
    registry = _registry(tmp_path, store, tool)
    call = _call("run-1", "call-generic-failure", 1)

    first = _execute(registry, call)
    replay = _execute(registry, call)

    assert first.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert first.reported_error_code == "TOOL_ERROR"
    assert first.effect_outcome == "unknown"
    assert first.handler_executed is True
    assert replay.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert replay.handler_executed is False
    assert replay.effect_outcome == "not_started"  # 拦截=本次未执行,不能算「本次结果未知」
    assert tool.calls == 1


def test_side_effect_timeout_is_persisted_unknown_and_not_retried(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    timeout = ToolHandlerOutcome(
        "counting_write",
        False,
        "provider timed out",
        error_code="TOOL_TIMEOUT",
    )
    tool = _CountingTool(result=timeout)
    registry = _registry(tmp_path, store, tool)
    call = _call("run-1", "call-1", 1)

    first = _execute(registry, call)
    second = _execute(registry, call)

    assert first.ok is False and second.ok is False
    assert first.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert first.reported_error_code == "TOOL_TIMEOUT"
    assert first.retryable is False
    assert first.operation is not None and first.operation.status == "unknown"
    assert first.failure_stage == ToolFailureStage.EFFECT_RECONCILIATION.value
    assert first.handler_executed is True
    assert second.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert second.failure_stage == ToolFailureStage.EFFECT_RECONCILIATION.value
    assert second.handler_executed is False
    assert second.effect_outcome == "not_started"  # 超时后的再次调用=拦截未执行
    assert tool.calls == 1
    record = store.get_tool_operation(
        owner_id="owner-a",
        run_id="run-1",
        operation_id=call.operation_id,
    )
    assert record is not None
    assert record.status == "unknown"
    assert record.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert record.unknown_reason == "effect_outcome_unknown:TOOL_TIMEOUT"


def test_started_side_effect_cancellation_is_unknown_and_not_retried(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    cancelled = ToolHandlerOutcome(
        "counting_write",
        False,
        "request cancelled after dispatch",
        error_code="CANCELLED",
    )
    tool = _CountingTool(result=cancelled)
    registry = _registry(tmp_path, store, tool)
    call = _call("run-1", "call-cancelled", 7)

    first = _execute(registry, call)
    replay = _execute(registry, call)

    assert first.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert first.reported_error_code == "CANCELLED"
    assert first.effect_outcome == "unknown"
    assert first.handler_executed is True
    assert first.operation is not None and first.operation.status == "unknown"
    assert replay.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert replay.handler_executed is False
    assert replay.effect_outcome == "not_started"  # 取消后的重放=拦截未执行
    assert tool.calls == 1


def test_equivalent_unknown_operation_with_new_call_id_is_not_retried(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    timeout = ToolHandlerOutcome(
        "counting_write",
        False,
        "provider timed out",
        error_code="TOOL_TIMEOUT",
    )
    tool = _CountingTool(result=timeout)
    registry = _registry(tmp_path, store, tool)

    first = _execute(registry, _call("run-1", "call-1", 1))
    second = _execute(registry, _call("run-1", "call-2", 1))

    assert first.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert second.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert second.handler_executed is False
    assert second.metadata["handler_details"]["tool_operation"]["diagnostic"].startswith(
        "equivalent_unknown_operation:tool_call:call-1"
    )
    assert tool.calls == 1
    records = store.list_tool_operations(owner_id="owner-a", run_id="run-1")
    assert [record.operation_id for record in records] == ["tool_call:call-1"]


def test_timeout_with_proof_not_started_remains_a_normal_failure(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    timeout = ToolHandlerOutcome(
        "counting_write",
        False,
        "queue wait timed out before dispatch",
        error_code="TOOL_TIMEOUT",
        effect_outcome="not_started",
        effect_source_ref="dispatch_queue:not_started",
    )
    tool = _CountingTool(result=timeout)
    registry = _registry(tmp_path, store, tool)
    call = _call("run-1", "call-1", 1)

    result = _execute(registry, call)

    assert result.error_code == "TOOL_TIMEOUT"
    assert result.operation is not None and result.operation.status == "failed"
    record = store.get_tool_operation(
        owner_id="owner-a",
        run_id="run-1",
        operation_id=call.operation_id,
    )
    assert record is not None and record.status == "failed"


def test_reconciliation_not_started_reopens_same_business_operation(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _BusinessReconcilingTool(
        ToolOperationReconciliation(
            outcome="not_started",
            source_ref="provider_lookup:req-7:not_found",
        )
    )
    registry = _registry(tmp_path, store, tool)

    first = _execute(registry, _call("run-1", "call-1", 7))
    second = _execute(registry, _call("run-2", "call-2", 7))

    assert first.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert second.ok is True
    assert tool.calls == 2
    assert second.metadata["handler_details"]["tool_operation"]["action"] == (
        "executed_after_reconciliation"
    )
    assert second.metadata["handler_details"]["tool_operation"][
        "reconciliation_source_ref"
    ] == (
        "provider_lookup:req-7:not_found"
    )
    record = store.get_tool_operation(
        owner_id="owner-a",
        run_id="run-1",
        operation_id="tool_call:call-1",
    )
    assert record is not None
    assert record.status == "succeeded"
    assert record.generation == 2
    assert record.result["effect_source_ref"] == "provider_lookup:req-7:not_found"
    assert (
        record.result["result_envelope"]["tool_operation"][
            "reconciliation_source_ref"
        ]
        == "provider_lookup:req-7:not_found"
    )

    replay = _execute(registry, _call("run-3", "call-3", 7))

    assert replay.ok is True
    assert tool.calls == 2
    assert replay.metadata["handler_details"]["tool_operation"]["action"] == "replay"
    assert replay.metadata["handler_details"]["tool_operation"][
        "reconciliation_source_ref"
    ] == "provider_lookup:req-7:not_found"


def test_reconciliation_safe_to_retry_reopens_same_business_operation(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _BusinessReconcilingTool(
        ToolOperationReconciliation(
            outcome="safe_to_retry",
            source_ref="provider_capability:stable-idempotency-key",
        )
    )
    registry = _registry(tmp_path, store, tool)

    first = _execute(registry, _call("run-1", "call-1", 17))
    second = _execute(registry, _call("run-2", "call-2", 17))

    assert first.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert second.ok is True
    assert tool.calls == 2
    assert second.metadata["handler_details"]["tool_operation"]["action"] == (
        "executed_after_reconciliation"
    )
    record = store.get_tool_operation(
        owner_id="owner-a",
        run_id="run-1",
        operation_id="tool_call:call-1",
    )
    assert record is not None
    assert record.status == "succeeded"
    assert record.generation == 2


def test_reconciliation_success_settles_without_second_effect(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    reconciled = ToolHandlerOutcome(
        "counting_write",
        True,
        "provider confirms committed",
    )
    tool = _BusinessReconcilingTool(
        ToolOperationReconciliation(
            outcome="succeeded",
            source_ref="provider_lookup:req-8:committed",
            result=reconciled,
        )
    )
    registry = _registry(tmp_path, store, tool)

    first = _execute(registry, _call("run-1", "call-1", 8))
    second = _execute(registry, _call("run-2", "call-2", 8))

    assert first.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert second.ok is True
    assert second.output == "provider confirms committed"
    assert second.metadata["handler_details"]["tool_operation"]["action"] == "reconciled"
    assert tool.calls == 1
    record = store.get_tool_operation(
        owner_id="owner-a",
        run_id="run-1",
        operation_id="tool_call:call-1",
    )
    assert record is not None
    assert record.result["effect_source_ref"] == "provider_lookup:req-8:committed"
    assert (
        record.result["result_envelope"]["tool_operation"][
            "reconciliation_source_ref"
        ]
        == "provider_lookup:req-8:committed"
    )


def test_reconciliation_failure_settles_without_second_effect(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    reconciled = ToolHandlerOutcome(
        "counting_write",
        False,
        "provider confirms rejection",
        error_code="TOOL_INVALID_ARGUMENTS",
        effect_outcome="not_started",
        effect_source_ref="provider_lookup:req-8:rejected",
    )
    tool = _BusinessReconcilingTool(
        ToolOperationReconciliation(
            outcome="failed",
            source_ref="provider_lookup:req-8:rejected",
            result=reconciled,
        )
    )
    registry = _registry(tmp_path, store, tool)

    first = _execute(registry, _call("run-1", "call-1", 8))
    second = _execute(registry, _call("run-2", "call-2", 8))

    assert first.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert second.error_code == "TOOL_INVALID_ARGUMENTS"
    assert second.metadata["handler_details"]["tool_operation"]["action"] == "reconciled"
    assert tool.calls == 1
    record = store.get_tool_operation(
        owner_id="owner-a",
        run_id="run-1",
        operation_id="tool_call:call-1",
    )
    assert record is not None and record.status == "failed"


def test_reconciliation_without_source_ref_stays_unknown(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _BusinessReconcilingTool(
        ToolOperationReconciliation(outcome="not_started")
    )
    registry = _registry(tmp_path, store, tool)

    _execute(registry, _call("run-1", "call-1", 9))
    second = _execute(registry, _call("run-2", "call-2", 9))

    assert second.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert second.metadata["handler_details"]["tool_operation"]["diagnostic"] == (
        "reconciliation_source_ref_missing"
    )
    assert tool.calls == 1


def test_concurrent_reconciliation_reopens_unknown_operation_only_once(tmp_path):
    retry_started = threading.Event()
    retry_release = threading.Event()

    class _BlockingReconcilingTool(_BusinessReconcilingTool):
        def execute(self, params):
            self.calls += 1
            if self.calls == 1:
                return self.result
            retry_started.set()
            assert retry_release.wait(timeout=5)
            return ToolHandlerOutcome(
                "counting_write",
                True,
                f"completed:{params['value']}",
            )

    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _BlockingReconcilingTool(
        ToolOperationReconciliation(
            outcome="not_started",
            source_ref="provider_lookup:req-10:not_found",
        )
    )
    registry = _registry(tmp_path, store, tool)
    first = _execute(registry, _call("run-1", "call-1", 10))
    results: list[ToolHandlerOutcome] = []
    retry = threading.Thread(
        target=lambda: results.append(
            _execute(registry, _call("run-2", "call-2", 10))
        )
    )
    retry.start()
    assert retry_started.wait(timeout=5)

    competing = _execute(registry, _call("run-3", "call-3", 10))
    retry_release.set()
    retry.join(timeout=5)

    assert first.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert not retry.is_alive()
    assert results[0].ok is True
    assert competing.error_code == "TOOL_OPERATION_IN_FLIGHT"
    assert tool.calls == 2


def test_business_scope_without_stable_key_fails_before_handler(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _CountingTool()
    tool.runtime_policy = replace(
        tool.runtime_policy,
        idempotency_policy=replace(
            tool.runtime_policy.idempotency_policy,
            scope="business",
        ),
    )

    registry = _registry(tmp_path, store, tool)
    result = _execute(registry, _call("run-1", "call-1", 1))

    assert result.error_code == "TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING"
    assert tool.calls == 0
    assert store.list_tool_operations(owner_id="owner-a") == []


def test_claim_store_failure_is_fail_closed_before_handler(tmp_path):
    class _UnavailableStore:
        def claim_tool_operation(self, request):
            raise OSError("disk unavailable")

        def finish_tool_operation(self, request):
            raise AssertionError("completion must not run")

    tool = _CountingTool()
    registry = _registry(tmp_path, _UnavailableStore(), tool)

    result = _execute(registry, _call("run-1", "call-1", 1))

    assert result.ok is False
    assert result.error_code == "TOOL_OPERATION_STORE_UNAVAILABLE"
    assert result.operation is not None and result.operation.status == "not_started"
    assert tool.calls == 0


def test_missing_store_is_fail_closed_by_default(tmp_path):
    tool = _CountingTool()
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
        )
    )
    registry.register(tool)

    result = _execute(registry, _call("run-1", "call-1", 1))

    assert result.ok is False
    assert result.error_code == "TOOL_OPERATION_STORE_UNAVAILABLE"
    assert tool.calls == 0


def test_completion_store_failure_does_not_invite_duplicate_retry(tmp_path):
    real_store = LocalStore(tmp_path / "local.db", enable_fts=False)

    class _FinishUnavailableStore:
        def claim_tool_operation(self, request):
            return real_store.claim_tool_operation(request)

        def finish_tool_operation(self, request):
            raise OSError("disk unavailable after effect")

    tool = _CountingTool()
    registry = _registry(
        tmp_path,
        _FinishUnavailableStore(),
        tool,
    )
    result = _execute(registry, _call("run-1", "call-1", 1))
    replay_attempt = _execute(registry, _call("run-1", "call-1", 1))

    assert result.ok is False
    assert result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert result.effect_outcome == "unknown"
    assert result.metadata["handler_details"]["reported_tool_result"]["ok"] is True
    assert result.operation is not None and result.operation.status == "unknown"
    assert result.metadata["handler_details"]["tool_operation"]["action"] == (
        "completion_persistence_failed"
    )
    assert replay_attempt.ok is False
    assert replay_attempt.error_code == "TOOL_OPERATION_IN_FLIGHT"
    assert tool.calls == 1


def test_wrong_holder_cannot_finish_an_operation(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    claim = store.claim_tool_operation(
        _claim_request(
            holder=new_tool_operation_holder(),
            now=100.0,
            lease_expires_at=200.0,
        )
    )

    with pytest.raises(ToolOperationOwnershipError):
        store.finish_tool_operation(
            ToolOperationCompletionRequest(
                owner_id="owner-a",
                run_id="run-1",
                operation_id="tool_call:call-1",
                holder_id="someone-else",
                generation=claim.record.generation,
                status="succeeded",
                result={"ok": True},
                now=150.0,
            )
        )


def test_terminal_completion_is_reentrant_only_for_same_holder_and_result(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    claim = store.claim_tool_operation(
        _claim_request(
            holder=new_tool_operation_holder(),
            now=100.0,
            lease_expires_at=200.0,
        )
    )
    completion = ToolOperationCompletionRequest(
        owner_id="owner-a",
        run_id="run-1",
        operation_id="tool_call:call-1",
        holder_id=claim.record.holder_id,
        generation=claim.record.generation,
        status="succeeded",
        result={"schema_version": "tool_execution_result.v1", "ok": True},
        now=150.0,
    )

    first = store.finish_tool_operation(completion)
    replayed_finish = store.finish_tool_operation(completion)

    assert first.status == "succeeded"
    assert replayed_finish.result == first.result
    with pytest.raises(ToolOperationOwnershipError):
        store.finish_tool_operation(
            ToolOperationCompletionRequest(
                **{
                    **completion.__dict__,
                    "result": {
                        "schema_version": "tool_execution_result.v1",
                        "ok": False,
                    },
                }
            )
        )
    with pytest.raises(ToolOperationOwnershipError):
        store.finish_tool_operation(
            ToolOperationCompletionRequest(
                **{
                    **completion.__dict__,
                    "holder_id": "different-holder",
                }
            )
        )


def test_business_key_can_replay_across_runs_only_when_explicit(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    first_holder = new_tool_operation_holder()
    first = store.claim_tool_operation(
        _claim_request(
            holder=first_holder,
            now=100.0,
            lease_expires_at=200.0,
            idempotency_scope="business",
            idempotency_key="external-order-7",
        )
    )
    store.finish_tool_operation(
        ToolOperationCompletionRequest(
            owner_id="owner-a",
            run_id="run-1",
            operation_id="tool_call:call-1",
            holder_id=first.record.holder_id,
            generation=first.record.generation,
            status="succeeded",
            result={
                "schema_version": "tool_execution_result.v1",
                "ok": True,
            },
            now=150.0,
        )
    )
    second = store.claim_tool_operation(
        ToolOperationClaimRequest(
            **{
                **_claim_request(
                    holder=new_tool_operation_holder(),
                    now=300.0,
                    lease_expires_at=400.0,
                    idempotency_scope="business",
                    idempotency_key="external-order-7",
                ).__dict__,
                "run_id": "run-2",
                "operation_id": "tool_call:call-2",
            }
        )
    )

    assert second.action == "replay"
    assert second.record.run_id == "run-1"


def test_business_key_reuse_with_changed_input_is_a_conflict(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    first_holder = new_tool_operation_holder()
    first_request = _claim_request(
        holder=first_holder,
        now=100.0,
        lease_expires_at=200.0,
        idempotency_scope="business",
        idempotency_key="external-order-7",
    )
    first = store.claim_tool_operation(first_request)
    store.finish_tool_operation(
        ToolOperationCompletionRequest(
            owner_id="owner-a",
            run_id="run-1",
            operation_id="tool_call:call-1",
            holder_id=first.record.holder_id,
            generation=first.record.generation,
            status="succeeded",
            result={
                "schema_version": "tool_execution_result.v1",
                "ok": True,
            },
            now=150.0,
        )
    )
    changed = store.claim_tool_operation(
        ToolOperationClaimRequest(
            **{
                **first_request.__dict__,
                "run_id": "run-2",
                "operation_id": "tool_call:call-2",
                "args_hash": tool_arguments_hash({"value": 2}),
                "holder": new_tool_operation_holder(),
                "now": 300.0,
                "lease_expires_at": 400.0,
            }
        )
    )

    assert changed.action == "conflict"
    assert "args_hash" in changed.reason


def test_remote_holder_is_in_flight_until_lease_then_unknown(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    first = _claim_request(
        holder=ToolOperationHolder(
            holder_id="remote-holder",
            host="remote-worker.example",
            pid=42,
            process_start_token="remote-start",
        ),
        now=100.0,
        lease_expires_at=200.0,
    )
    assert store.claim_tool_operation(first).action == "execute"

    active = store.claim_tool_operation(
        _claim_request(
            holder=new_tool_operation_holder(),
            now=150.0,
            lease_expires_at=300.0,
        )
    )
    expired = store.claim_tool_operation(
        _claim_request(
            holder=new_tool_operation_holder(),
            now=201.0,
            lease_expires_at=400.0,
        )
    )

    assert active.action == "in_flight"
    assert expired.action == "unknown"
    assert expired.record.status == "unknown"


def test_unreadable_terminal_result_never_reexecutes_handler(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _CountingTool()
    registry = _registry(tmp_path, store, tool)
    call = _call("run-1", "call-1", 1)
    assert _execute(registry, call).ok is True
    with store._connection() as conn:
        conn.execute(
            """
            UPDATE tool_operations
            SET result_json = ?
            WHERE owner_id = ? AND run_id = ? AND operation_id = ?
            """,
            ("{}", "owner-a", "run-1", call.operation_id),
        )
        conn.commit()

    replay = _execute(registry, call)

    assert replay.ok is False
    assert replay.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert replay.operation is not None and replay.operation.status == "unknown"
    assert replay.metadata["handler_details"]["tool_operation"]["action"] == "reconcile"
    assert replay.operation.replayed is False
    assert tool.calls == 1


def test_operation_identity_is_owner_scoped(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool_a = _CountingTool()
    tool_b = _CountingTool()
    call = _call("run-1", "call-1", 1)

    registry_a = _registry(
        tmp_path,
        store,
        tool_a,
        owner_id="owner-a",
    )
    registry_b = _registry(
        tmp_path,
        store,
        tool_b,
        owner_id="owner-b",
    )
    result_a = _execute(registry_a, call)
    result_b = _execute(registry_b, call)

    assert result_a.ok and result_b.ok
    assert tool_a.calls == 1 and tool_b.calls == 1


def test_business_identity_is_owner_scoped(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool_a = _BusinessCountingTool()
    tool_b = _BusinessCountingTool()

    registry_a = _registry(
        tmp_path,
        store,
        tool_a,
        owner_id="owner-a",
    )
    registry_b = _registry(
        tmp_path,
        store,
        tool_b,
        owner_id="owner-b",
    )
    result_a = _execute(registry_a, _call("run-1", "call-1", 1))
    result_b = _execute(registry_b, _call("run-2", "call-2", 1))

    assert result_a.ok and result_b.ok
    assert tool_a.calls == 1 and tool_b.calls == 1


def test_success_cannot_claim_incomplete_effect_outcome():
    with pytest.raises(ValueError, match="successful tool result"):
        ToolHandlerOutcome(
            "counting_write",
            True,
            "contradictory",
            effect_outcome="unknown",
        )


def test_resilience_retries_read_only_but_never_side_effect(tmp_path):
    class _RetryTool(BaseTool):
        def __init__(self, name: str, effect: str) -> None:
            self.calls = 0
            self.effect = effect
            self.model_spec = make_test_model_spec(name, description=name)
            self.runtime_policy = make_test_runtime_policy(effect)

        def execute(self, params):
            self.calls += 1
            if self.effect == "read_only" and self.calls > 1:
                return ToolHandlerOutcome(self.model_spec.name, True, "ok")
            return ToolHandlerOutcome(
                self.model_spec.name,
                False,
                "try again",
                error_code="TOOL_RATE_LIMIT_EXCEEDED",
            )

    read_tool = _RetryTool("read", "read_only")
    write_tool = _RetryTool("write", "mutating")
    read = execute_canonical_test_call(
        tmp_path,
        tools={"read": read_tool},
        tool_name="read",
        arguments={},
    ).result
    write = execute_canonical_test_call(
        tmp_path,
        tools={"write": write_tool},
        tool_name="write",
        arguments={},
        operation_store_required=False,
    ).result

    assert read.ok is True and read_tool.calls == 2
    assert read.metadata["handler_details"]["tool_resilience"]["retry_attempts"] == 1
    assert write.ok is False and write_tool.calls == 1


def _registry(
    root: Path,
    store: object,
    tool: BaseTool,
    *,
    owner_id: str = "owner-a",
) -> ToolRegistry:
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=root,
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            operation_store=store,
            operation_store_required=True,
            operation_owner_id=owner_id,
        )
    )
    registry.register(tool)
    return registry


def _call(
    run_id: str,
    call_id: str,
    value: int,
    *,
    attempt_id: str = "",
) -> _CallSpec:
    return _CallSpec(
        tool_name="counting_write",
        arguments={"value": value},
        run_id=run_id,
        call_id=call_id,
        attempt_id=attempt_id,
    )


def _mixed_call(
    run_id: str,
    call_id: str,
    action: str,
    value: int,
) -> _CallSpec:
    return _CallSpec(
        tool_name="mixed_action",
        arguments={"action": action, "value": value},
        run_id=run_id,
        call_id=call_id,
    )


def _execute(
    registry: ToolRegistry,
    spec: _CallSpec,
    *,
    write_boundary: dict[str, object] | None = None,
) -> ToolResult:
    snapshot = registry.runtime_snapshot(run_id=spec.run_id)
    call = canonical_test_call(
        snapshot,
        spec.tool_name,
        spec.arguments,
        call_id=spec.call_id,
        attempt_id=spec.attempt_id or f"attempt:{spec.run_id}",
        operation_id=spec.operation_id,
        idempotency_key=spec.idempotency_key,
    )
    return registry.execute_tool(
        call,
        write_boundary=write_boundary,
        runtime_snapshot=snapshot,
        trusted_run_context={
            "run_scope": {
                "request_id": f"request-{spec.run_id}",
                "task_id": spec.run_id,
                "owner_type": "user",
                "owner_id": "owner-a",
            }
        },
    ).result


def _claim_request(
    *,
    holder: ToolOperationHolder,
    now: float,
    lease_expires_at: float,
    idempotency_scope: str = "operation",
    idempotency_key: str = "idem:operation:test",
) -> ToolOperationClaimRequest:
    return ToolOperationClaimRequest(
        owner_id="owner-a",
        run_id="run-1",
        task_id="run-1",
        operation_id="tool_call:call-1",
        tool="counting_write",
        args_hash=tool_arguments_hash({"value": 1}),
        idempotency_key=idempotency_key,
        idempotency_scope=idempotency_scope,
        idempotency_namespace="counting_write",
        holder=holder,
        lease_expires_at=lease_expires_at,
        now=now,
    )
