from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.action_protocol import RunScope, ToolCallEnvelope
from agent_py_agent.agent.agent_core.tool_call_runtime import (
    _runtime_tool_call_id,
)
from agent_py_agent.agent.contracts.gates.tool_effects import args_hash_for_call
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
    ToolExecutionResult,
    ToolFailureStage,
    ToolOperationReconciliation,
    ToolSpec,
)
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.tooling.registry_resilience import (
    ResilientToolInvokeRequest,
    resilient_tool_invoke,
)


class _CountingTool(BaseTool):
    def __init__(
        self,
        *,
        result: ToolExecutionResult | None = None,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
    ):
        self.calls = 0
        self.result = result
        self.started = started
        self.release = release
        self.spec = ToolSpec(
            name="counting_write",
            category="test",
            description="count one side effect",
            use_cases=[],
            avoid_when=[],
            keywords=[],
            parameters={"value": "integer"},
            parameter_schema={"value": {"type": "integer"}},
            required_parameters=["value"],
            effect="mutating",
            idempotency_scope="operation",
        )

    def execute(self, params):
        self.calls += 1
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            assert self.release.wait(timeout=5)
        return self.result or ToolExecutionResult(
            "counting_write",
            True,
            f"completed:{params['value']}",
            result_envelope={"domain": {"value": params["value"]}},
        )


class _BusinessReconcilingTool(_CountingTool):
    def __init__(self, reconciliation: ToolOperationReconciliation):
        super().__init__(
            result=ToolExecutionResult(
                "counting_write",
                False,
                "provider timed out",
                error_code="TOOL_TIMEOUT",
            )
        )
        self.spec.idempotency_scope = "business"
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
        return ToolExecutionResult(
            "counting_write",
            True,
            f"completed:{params['value']}",
        )


class _BusinessCountingTool(_CountingTool):
    def __init__(self) -> None:
        super().__init__()
        self.spec.idempotency_scope = "business"

    def business_idempotency_key(self, params):
        return f"business-count:{params['value']}"


class _MixedActionTool(BaseTool):
    def __init__(self) -> None:
        self.calls = 0
        self.spec = ToolSpec(
            name="mixed_action",
            category="test",
            description="one tool with read and write actions",
            use_cases=[],
            avoid_when=[],
            keywords=[],
            parameters={"action": "string", "value": "integer"},
            parameter_schema={
                "action": {"type": "string", "enum": ["inspect", "update"]},
                "value": {"type": "integer"},
            },
            required_parameters=["action", "value"],
            effect="mutating",
            effect_by_parameter={
                "action": {
                    "inspect": "read_only",
                    "update": "mutating",
                }
            },
            idempotency_scope="operation",
        )

    def execute(self, params):
        self.calls += 1
        return ToolExecutionResult(
            "mixed_action",
            True,
            f"{params['action']}:{params['value']}",
        )


def test_exact_operation_replays_saved_result_without_second_effect(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _CountingTool()
    registry = _registry(tmp_path, store, tool)
    envelope = _envelope("run-1", "call-1", 7)

    first = registry.execute_call(envelope)
    reopened = LocalStore(tmp_path / "local.db", enable_fts=False)
    replay_tool = _CountingTool()
    replay = _registry(tmp_path, reopened, replay_tool).execute_call(envelope)

    assert first.ok is True
    assert replay.ok is True
    assert replay.output == first.output
    assert replay.result_envelope["domain"] == {"value": 7}
    assert replay.result_envelope["tool_operation"]["replayed"] is True
    assert first.handler_executed is True
    assert replay.handler_executed is False
    assert tool.calls == 1
    assert replay_tool.calls == 0


def test_same_operation_with_changed_input_is_rejected(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _CountingTool()
    registry = _registry(tmp_path, store, tool)

    first = registry.execute_call(_envelope("run-1", "call-1", 1))
    changed = registry.execute_call(_envelope("run-1", "call-1", 2))

    assert first.ok is True
    assert changed.ok is False
    assert changed.error_code == "TOOL_OPERATION_IDENTITY_CONFLICT"
    assert tool.calls == 1


def test_equal_arguments_in_new_operations_are_both_legal(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _CountingTool()
    registry = _registry(tmp_path, store, tool)

    first = registry.execute_call(_envelope("run-1", "call-1", 1))
    second = registry.execute_call(_envelope("run-1", "call-2", 1))
    other_run = registry.execute_call(_envelope("run-2", "call-1", 1))

    assert first.ok and second.ok and other_run.ok
    assert tool.calls == 3


def test_provider_call_id_can_repeat_in_separate_runtime_attempts(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _CountingTool()
    registry = _registry(tmp_path, store, tool)

    first_envelope = _envelope(
        "run-1", "provider-call-1", 1, attempt_id="attempt-1"
    )
    second_envelope = _envelope(
        "run-1", "provider-call-1", 2, attempt_id="attempt-2"
    )
    first = registry.execute_call(first_envelope)
    second = registry.execute_call(second_envelope)

    assert first.ok and second.ok
    assert first_envelope.operation_id == "tool_call:attempt-1:provider-call-1"
    assert second_envelope.operation_id == "tool_call:attempt-2:provider-call-1"
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

    first = registry.execute_call(
        _envelope("run-1", "provider-call-1", 1, attempt_id="attempt-1")
    )
    changed = registry.execute_call(
        _envelope("run-1", "provider-call-1", 2, attempt_id="attempt-1")
    )

    assert first.ok is True
    assert changed.error_code == "TOOL_OPERATION_IDENTITY_CONFLICT"
    assert tool.calls == 1


def test_read_only_variant_of_mixed_action_tool_bypasses_effect_ledger(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _MixedActionTool()
    registry = _registry(tmp_path, store, tool)

    first = registry.execute_call(
        _mixed_envelope("run-1", "provider-call-1", "inspect", 1)
    )
    second = registry.execute_call(
        _mixed_envelope("run-1", "provider-call-1", "inspect", 2)
    )

    assert first.ok and second.ok
    assert tool.calls == 2
    assert store.list_tool_operations(owner_id="owner-a", run_id="run-1") == []


def test_explicit_boundary_cannot_lower_or_bypass_dangerous_effect(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool = _MixedActionTool()
    registry = _registry(tmp_path, store, tool)

    result = registry.execute_call(
        _mixed_envelope("run-1", "provider-call-1", "inspect", 1),
        write_boundary={"tool_effects": {"mixed_action": "dangerous"}},
    )

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["status"] == "NEED_APPROVAL"
    assert tool.calls == 0


def test_concurrent_same_operation_never_enters_handler_twice(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    started = threading.Event()
    release = threading.Event()
    tool = _CountingTool(started=started, release=release)
    registry = _registry(tmp_path, store, tool)
    envelope = _envelope("run-1", "call-1", 1)
    results: list[ToolExecutionResult] = []

    first = threading.Thread(
        target=lambda: results.append(registry.execute_call(envelope))
    )
    first.start()
    assert started.wait(timeout=5)
    second = registry.execute_call(envelope)
    release.set()
    first.join(timeout=5)

    assert not first.is_alive()
    assert tool.calls == 1
    assert second.ok is False
    assert second.error_code == "TOOL_OPERATION_IN_FLIGHT"
    assert results[0].ok is True


def test_dead_holder_becomes_unknown_and_is_not_reexecuted(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    envelope = _envelope("run-1", "call-1", 1)
    store.claim_tool_operation(
        ToolOperationClaimRequest(
            owner_id="owner-a",
            run_id="run-1",
            task_id="run-1",
            operation_id=envelope.operation_id,
            tool="counting_write",
            args_hash=args_hash_for_call({"value": 1}),
            idempotency_key=envelope.idempotency_key,
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

    result = _registry(tmp_path, store, tool).execute_call(envelope)

    assert result.ok is False
    assert result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert tool.calls == 0
    record = store.get_tool_operation(
        owner_id="owner-a",
        run_id="run-1",
        operation_id=envelope.operation_id,
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
    failure = ToolExecutionResult(
        "counting_write",
        False,
        "provider unavailable",
        error_code="TOOL_RATE_LIMIT_EXCEEDED",
    )
    tool = _CountingTool(result=failure)
    registry = _registry(tmp_path, store, tool)
    envelope = _envelope("run-1", "call-1", 1)

    first = registry.execute_call(envelope)
    replay = registry.execute_call(envelope)

    assert first.ok is False and replay.ok is False
    assert replay.error_code == first.error_code
    assert replay.retryable == first.retryable
    assert replay.recommended_action == first.recommended_action
    assert replay.output == first.output
    assert first.failure_stage == ToolFailureStage.EXECUTION.value
    assert first.handler_executed is True
    assert replay.failure_stage == ToolFailureStage.EFFECT_RECONCILIATION.value
    assert replay.handler_executed is False
    assert replay.result_envelope["tool_operation"]["original_tool_execution"][
        "failure_stage"
    ] == ToolFailureStage.EXECUTION.value
    assert replay.result_envelope["tool_operation"]["original_tool_execution"][
        "handler_executed"
    ] is True
    assert tool.calls == 1


def test_side_effect_timeout_is_persisted_unknown_and_not_retried(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    timeout = ToolExecutionResult(
        "counting_write",
        False,
        "provider timed out",
        error_code="TOOL_TIMEOUT",
    )
    tool = _CountingTool(result=timeout)
    registry = _registry(tmp_path, store, tool)
    envelope = _envelope("run-1", "call-1", 1)

    first = registry.execute_call(envelope)
    second = registry.execute_call(envelope)

    assert first.ok is False and second.ok is False
    assert first.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert first.reported_error_code == "TOOL_TIMEOUT"
    assert first.retryable is False
    assert first.result_envelope["tool_operation"]["status"] == "unknown"
    assert first.failure_stage == ToolFailureStage.EFFECT_RECONCILIATION.value
    assert first.handler_executed is True
    assert second.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert second.failure_stage == ToolFailureStage.EFFECT_RECONCILIATION.value
    assert second.handler_executed is False
    assert tool.calls == 1
    record = store.get_tool_operation(
        owner_id="owner-a",
        run_id="run-1",
        operation_id=envelope.operation_id,
    )
    assert record is not None
    assert record.status == "unknown"
    assert record.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert record.unknown_reason == "effect_outcome_unknown:TOOL_TIMEOUT"


def test_equivalent_unknown_operation_with_new_call_id_is_not_retried(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    timeout = ToolExecutionResult(
        "counting_write",
        False,
        "provider timed out",
        error_code="TOOL_TIMEOUT",
    )
    tool = _CountingTool(result=timeout)
    registry = _registry(tmp_path, store, tool)

    first = registry.execute_call(_envelope("run-1", "call-1", 1))
    second = registry.execute_call(_envelope("run-1", "call-2", 1))

    assert first.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert second.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert second.handler_executed is False
    assert second.result_envelope["tool_operation"]["diagnostic"].startswith(
        "equivalent_unknown_operation:tool_call:call-1"
    )
    assert tool.calls == 1
    records = store.list_tool_operations(owner_id="owner-a", run_id="run-1")
    assert [record.operation_id for record in records] == ["tool_call:call-1"]


def test_timeout_with_proof_not_started_remains_a_normal_failure(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    timeout = ToolExecutionResult(
        "counting_write",
        False,
        "queue wait timed out before dispatch",
        error_code="TOOL_TIMEOUT",
        effect_outcome="not_started",
        effect_source_ref="dispatch_queue:not_started",
    )
    tool = _CountingTool(result=timeout)
    registry = _registry(tmp_path, store, tool)
    envelope = _envelope("run-1", "call-1", 1)

    result = registry.execute_call(envelope)

    assert result.error_code == "TOOL_TIMEOUT"
    assert result.result_envelope["tool_operation"]["status"] == "failed"
    record = store.get_tool_operation(
        owner_id="owner-a",
        run_id="run-1",
        operation_id=envelope.operation_id,
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

    first = registry.execute_call(_envelope("run-1", "call-1", 7))
    second = registry.execute_call(_envelope("run-2", "call-2", 7))

    assert first.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert second.ok is True
    assert tool.calls == 2
    assert second.result_envelope["tool_operation"]["action"] == (
        "executed_after_reconciliation"
    )
    assert second.result_envelope["tool_operation"]["reconciliation_source_ref"] == (
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

    replay = registry.execute_call(_envelope("run-3", "call-3", 7))

    assert replay.ok is True
    assert tool.calls == 2
    assert replay.result_envelope["tool_operation"]["action"] == "replay"
    assert replay.result_envelope["tool_operation"][
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

    first = registry.execute_call(_envelope("run-1", "call-1", 17))
    second = registry.execute_call(_envelope("run-2", "call-2", 17))

    assert first.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert second.ok is True
    assert tool.calls == 2
    assert second.result_envelope["tool_operation"]["action"] == (
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
    reconciled = ToolExecutionResult(
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

    first = registry.execute_call(_envelope("run-1", "call-1", 8))
    second = registry.execute_call(_envelope("run-2", "call-2", 8))

    assert first.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert second.ok is True
    assert second.output == "provider confirms committed"
    assert second.result_envelope["tool_operation"]["action"] == "reconciled"
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
    reconciled = ToolExecutionResult(
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

    first = registry.execute_call(_envelope("run-1", "call-1", 8))
    second = registry.execute_call(_envelope("run-2", "call-2", 8))

    assert first.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert second.error_code == "TOOL_INVALID_ARGUMENTS"
    assert second.result_envelope["tool_operation"]["action"] == "reconciled"
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

    registry.execute_call(_envelope("run-1", "call-1", 9))
    second = registry.execute_call(_envelope("run-2", "call-2", 9))

    assert second.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert second.result_envelope["tool_operation"]["diagnostic"] == (
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
            return ToolExecutionResult(
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
    first = registry.execute_call(_envelope("run-1", "call-1", 10))
    results: list[ToolExecutionResult] = []
    retry = threading.Thread(
        target=lambda: results.append(
            registry.execute_call(_envelope("run-2", "call-2", 10))
        )
    )
    retry.start()
    assert retry_started.wait(timeout=5)

    competing = registry.execute_call(_envelope("run-3", "call-3", 10))
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
    tool.spec.idempotency_scope = "business"

    result = _registry(tmp_path, store, tool).execute_call(
        _envelope("run-1", "call-1", 1)
    )

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

    result = registry.execute_call(_envelope("run-1", "call-1", 1))

    assert result.ok is False
    assert result.error_code == "TOOL_OPERATION_STORE_UNAVAILABLE"
    assert result.result_envelope["tool_operation"]["status"] == "not_started"
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

    result = registry.execute_call(_envelope("run-1", "call-1", 1))

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
    result = registry.execute_call(_envelope("run-1", "call-1", 1))
    replay_attempt = registry.execute_call(_envelope("run-1", "call-1", 1))

    assert result.ok is False
    assert result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert result.effect_outcome == "unknown"
    assert result.result_envelope["reported_tool_result"]["ok"] is True
    assert result.result_envelope["tool_operation"]["status"] == "unknown"
    assert result.result_envelope["tool_operation"]["action"] == (
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
                "args_hash": args_hash_for_call({"value": 2}),
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
    envelope = _envelope("run-1", "call-1", 1)
    assert registry.execute_call(envelope).ok is True
    with store._connection() as conn:
        conn.execute(
            """
            UPDATE tool_operations
            SET result_json = ?
            WHERE owner_id = ? AND run_id = ? AND operation_id = ?
            """,
            ("{}", "owner-a", "run-1", envelope.operation_id),
        )
        conn.commit()

    replay = registry.execute_call(envelope)

    assert replay.ok is False
    assert replay.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert replay.result_envelope["tool_operation"]["status"] == "unknown"
    assert replay.result_envelope["tool_operation"]["action"] == "reconcile"
    assert replay.result_envelope["tool_operation"]["replayed"] is False
    assert tool.calls == 1


def test_operation_identity_is_owner_scoped(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool_a = _CountingTool()
    tool_b = _CountingTool()
    envelope = _envelope("run-1", "call-1", 1)

    result_a = _registry(
        tmp_path,
        store,
        tool_a,
        owner_id="owner-a",
    ).execute_call(envelope)
    result_b = _registry(
        tmp_path,
        store,
        tool_b,
        owner_id="owner-b",
    ).execute_call(envelope)

    assert result_a.ok and result_b.ok
    assert tool_a.calls == 1 and tool_b.calls == 1


def test_business_identity_is_owner_scoped(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tool_a = _BusinessCountingTool()
    tool_b = _BusinessCountingTool()

    result_a = _registry(
        tmp_path,
        store,
        tool_a,
        owner_id="owner-a",
    ).execute_call(_envelope("run-1", "call-1", 1))
    result_b = _registry(
        tmp_path,
        store,
        tool_b,
        owner_id="owner-b",
    ).execute_call(_envelope("run-2", "call-2", 1))

    assert result_a.ok and result_b.ok
    assert tool_a.calls == 1 and tool_b.calls == 1


def test_success_cannot_claim_incomplete_effect_outcome():
    with pytest.raises(ValueError, match="successful tool result"):
        ToolExecutionResult(
            "counting_write",
            True,
            "contradictory",
            effect_outcome="unknown",
        )


def test_native_provider_call_id_is_used_as_operation_identity():
    runtime_request = SimpleNamespace(
        payload={"tool": "counting_write", "call_id": "provider-call-42"},
        trace_request=SimpleNamespace(tool_rounds=9, idx=3),
    )
    text_request = SimpleNamespace(
        payload={"tool": "counting_write"},
        trace_request=SimpleNamespace(tool_rounds=9, idx=3),
    )

    assert _runtime_tool_call_id(runtime_request) == "provider-call-42"
    assert _runtime_tool_call_id(text_request) == "round-9-tool-3"


def test_resilience_retries_read_only_but_never_side_effect(tmp_path):
    read_calls = 0
    write_calls = 0

    def read_invoke():
        nonlocal read_calls
        read_calls += 1
        if read_calls == 1:
            return ToolExecutionResult(
                "read",
                False,
                "try again",
                error_code="TOOL_RATE_LIMIT_EXCEEDED",
            )
        return ToolExecutionResult("read", True, "ok")

    def write_invoke():
        nonlocal write_calls
        write_calls += 1
        return ToolExecutionResult(
            "write",
            False,
            "try again",
            error_code="TOOL_RATE_LIMIT_EXCEEDED",
        )

    read = resilient_tool_invoke(
        ResilientToolInvokeRequest(
            invoke=read_invoke,
            spec=_spec("read", "read_only"),
        )
    )
    write = resilient_tool_invoke(
        ResilientToolInvokeRequest(
            invoke=write_invoke,
            spec=_spec("write", "mutating"),
        )
    )

    assert read.ok is True and read_calls == 2
    assert write.ok is False and write_calls == 1
    assert write.result_envelope["tool_resilience"]["retry_attempts"] == 0


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


def _envelope(
    run_id: str,
    call_id: str,
    value: int,
    *,
    attempt_id: str = "",
) -> ToolCallEnvelope:
    return ToolCallEnvelope(
        call_id=call_id,
        source="model_tool_call",
        tool_name="counting_write",
        input={"value": value},
        scope=RunScope(
            request_id=f"request-{run_id}",
            attempt_id=attempt_id,
            task_id=run_id,
            run_id=run_id,
            owner_type="user",
            owner_id="owner-a",
        ),
    )


def _mixed_envelope(
    run_id: str,
    call_id: str,
    action: str,
    value: int,
) -> ToolCallEnvelope:
    return ToolCallEnvelope(
        call_id=call_id,
        source="model_tool_call",
        tool_name="mixed_action",
        input={"action": action, "value": value},
        scope=RunScope(
            request_id=f"request-{run_id}",
            task_id=run_id,
            run_id=run_id,
            owner_type="user",
            owner_id="owner-a",
        ),
    )


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
        args_hash=args_hash_for_call({"value": 1}),
        idempotency_key=idempotency_key,
        idempotency_scope=idempotency_scope,
        idempotency_namespace="counting_write",
        holder=holder,
        lease_expires_at=lease_expires_at,
        now=now,
    )


def _spec(name: str, effect: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        category="test",
        description=name,
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={},
        effect=effect,
        idempotency_scope="operation" if effect != "read_only" else "",
    )
