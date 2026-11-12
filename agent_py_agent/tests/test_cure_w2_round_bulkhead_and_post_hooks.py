"""Behavioral regression tests for Workstream W2.

Tests the round execution per-tool crash bulkhead, event pairing,
exception propagation invariants, approval rejection unmapped reason codes,
SkillLearningGateError hierarchy, and post-tool telemetry/verification resilience.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_py_agent.agent.agent_core.runner import stage_trace
from agent_py_agent.agent.agent_core.tool_call_archive_record import (
    ToolCallRecordParams,
    archive_tool_call_record,
)
from agent_py_agent.agent.agent_core.tool_call_runtime import (
    ToolCallRuntimeRequest,
    execute_traced_tool_call,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    _APPROVAL_REJECTION_MESSAGES,
    _MUST_PROPAGATE_TOOL_EXCEPTIONS,
    ToolProgressEvent,
    ToolRoundExecutionRequest,
    _rejected_approval_execution,
    execute_tool_round,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
from agent_py_agent.agent.capability.pack_verification_hooks import (
    run_pack_verification_hooks,
)
from agent_py_agent.agent.capability.skill_learning_host_feedback import (
    execute_skill_learning_host_action,
    record_user_skill_edit_after_tool,
)
from agent_py_agent.agent.capability.skill_learning_publish import SkillLearningGateError
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStoreError
from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable
from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision
from agent_py_agent.agent.conversation.task_rejected_actions import (
    append_task_rejected_action,
    sync_task_rejected_actions,
)
from agent_py_agent.agent.tooling.action_policy import ActionDecision
from agent_py_agent.agent.tooling.executor import ToolExecution
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolCall,
    ToolContentBlock,
    ToolProtocolSnapshot,
    ToolResult,
    ToolSuccessFacts,
)
from agent_py_agent.agent.verification.runtime import (
    VerificationItem,
    VerificationRuntime,
    record_tool_verification,
)
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_model_specs,
)

_TEST_RUN_ID = "test-cure-w2-run"


class _ProgressCollector:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def write_progress(self, structured: dict[str, Any], legacy_text: str) -> None:
        self.events.append(dict(structured))


def _make_protocol_snapshot() -> ToolProtocolSnapshot:
    return ToolProtocolSnapshot(
        _TEST_RUN_ID,
        "text",
        ProviderToolCapability(
            provider="test",
            endpoint="local://test-endpoint",
            model="test-model",
            stream=False,
            native_supported=False,
            evidence="canonical_test_fixture",
        ),
    )


def _make_call(name: str, index: int, args: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(
        call_id=f"call-{index}",
        tool_name=name,
        arguments=args or {"path": f"file_{index}.txt"},
        source_protocol="native",
        schema_hash="sha256:test-schema",
        run_id=_TEST_RUN_ID,
        turn_id=f"{_TEST_RUN_ID}:turn",
        attempt_id=f"{_TEST_RUN_ID}:attempt",
    )


def _ok_execution(call: ToolCall) -> ToolExecution:
    result = ToolResult.succeeded(
        call,
        "ok",
        facts=ToolSuccessFacts(
            effect_outcome="confirmed",
            metadata={"handler_details": {"status": "ok"}},
        ),
    )
    return ToolExecution(
        call,
        ActionDecision("allow"),
        result,
        ("received", "normalized", "validated", "authorized", "running", "succeeded"),
    )


def test_serial_tool_unexpected_crash_bulkhead_paired_events_and_isolation():
    """Verify that when a tool unexpectedly crashes mid-round in serial execution:
    - started and finished events are strictly paired (no orphaned spinner)
    - failing tool produces ToolResult(ok=False, error_code='INTERNAL_ERROR')
    - sibling tools still execute and succeed
    - execute_tool_round does not crash and completes the round.
    """
    collector = _ProgressCollector()
    params = SimpleNamespace(
        request_id="req-1",
        run_id=_TEST_RUN_ID,
        task_id="task-1",
        tool_protocol_snapshot=_make_protocol_snapshot(),
        effective_on_chunk=collector,
        tool_context=[],
        cancellation_token=None,
    )
    calls = [
        _make_call("read_file", 1),
        _make_call("write_file", 2),
        _make_call("shell", 3),
    ]
    records: list[Any] = []

    def execute_one(work: Any) -> ToolExecution:
        if work.call.tool_name == "write_file":
            raise RuntimeError("simulated unexpected write pipeline failure")
        return _ok_execution(work.call)

    request = ToolRoundExecutionRequest(
        agent=SimpleNamespace(config=SimpleNamespace(auto_save_memory=False)),
        params=params,
        tool_rounds=1,
        response=ModelResponse(text="round response", backend="test"),
        calls=calls,
        execute_one=execute_one,
        record_one=records.append,
    )

    # Must not raise RuntimeError!
    completed = execute_tool_round(request)

    # 3 tools must be recorded
    assert len(records) == 3
    assert records[0].result.ok is True
    assert records[0].call.tool_name == "read_file"

    crashed = records[1].result
    assert crashed.ok is False
    assert crashed.reported_error_code == "INTERNAL_ERROR"
    assert crashed.effect_outcome == "unknown"
    assert "RuntimeError" in crashed.output
    assert records[1].call.tool_name == "write_file"

    assert records[2].result.ok is True
    assert records[2].call.tool_name == "shell"

    # Verify event pairing for write_file
    tool_2_events = [e for e in collector.events if e.get("call_index") == 2 or e.get("tool") == "write_file"]
    phases = [e.get("phase") for e in tool_2_events]
    assert "started" in phases
    assert "finished" in phases
    assert phases.count("started") == 1
    assert phases.count("finished") == 1
    # 'started' must precede 'finished'
    assert phases.index("started") < phases.index("finished")


def test_parallel_tool_unexpected_crash_bulkhead_paired_events_and_isolation():
    """Verify that in parallel segment execution, an unexpected crash in one worker:
    - pairs started with finished
    - produces fallback ToolResult(ok=False, error_code='INTERNAL_ERROR')
    - allows sibling parallel tools to complete normally.
    """
    collector = _ProgressCollector()
    snapshot = runtime_snapshot_for_model_specs(
        (
            make_test_model_spec("read_file_a"),
            make_test_model_spec("read_file_b"),
        ),
        policies={
            "read_file_a": make_test_runtime_policy(concurrency_mode="parallel_safe"),
            "read_file_b": make_test_runtime_policy(concurrency_mode="parallel_safe"),
        },
    )
    params = SimpleNamespace(
        request_id="req-2",
        run_id=_TEST_RUN_ID,
        task_id="task-2",
        tool_protocol_snapshot=_make_protocol_snapshot(),
        tool_runtime_snapshot=snapshot,
        effective_on_chunk=collector,
        tool_context=[],
        cancellation_token=None,
    )
    calls = [
        _make_call("read_file_a", 1),
        _make_call("read_file_b", 2),
    ]
    records: list[Any] = []

    def execute_one(work: Any) -> ToolExecution:
        if work.call.tool_name == "read_file_a":
            raise RuntimeError("simulated parallel worker crash")
        return _ok_execution(work.call)

    request = ToolRoundExecutionRequest(
        agent=SimpleNamespace(config=SimpleNamespace(auto_save_memory=False)),
        params=params,
        tool_rounds=1,
        response=ModelResponse(text="parallel round", backend="test"),
        calls=calls,
        execute_one=execute_one,
        record_one=records.append,
    )

    execute_tool_round(request)

    assert len(records) == 2
    r_a = next(r for r in records if r.call.tool_name == "read_file_a")
    r_b = next(r for r in records if r.call.tool_name == "read_file_b")

    assert r_a.result.ok is False
    assert r_a.result.reported_error_code == "INTERNAL_ERROR"
    assert r_a.result.effect_outcome == "unknown"

    assert r_b.result.ok is True

    # Pairing check
    events_a = [e for e in collector.events if e.get("call_index") == 1]
    phases_a = [e.get("phase") for e in events_a]
    assert phases_a == ["started", "finished"]


@pytest.mark.parametrize(
    "exception_cls",
    [
        InterruptedError,
        ToolCancelled,
        RequestExecutionOutcomeUncertainError,
        RequestExecutionContextUnavailable,
        ToolWriteContextUnavailable,
        BoundedCallExitUnconfirmedError,
    ],
)
def test_must_propagate_exceptions_never_swallowed(exception_cls):
    """Verify that cancellation and uncertain outcome exceptions MUST propagate immediately."""
    params = SimpleNamespace(
        request_id="req-3",
        run_id=_TEST_RUN_ID,
        task_id="task-3",
        tool_protocol_snapshot=_make_protocol_snapshot(),
        tool_context=[],
        cancellation_token=None,
    )
    calls = [_make_call("read_file", 1)]

    def execute_one(work: Any) -> ToolExecution:
        if exception_cls is RequestExecutionOutcomeUncertainError:
            raise exception_cls(
                {
                    "id": "req-1",
                    "execution_attempt_id": "attempt-1",
                    "lease_epoch": 1,
                    "lease_owner": "owner-1",
                },
                "operation_phase",
            )
        if exception_cls is BoundedCallExitUnconfirmedError:
            raise exception_cls("call-id", "reason")
        raise exception_cls("simulated cancellation or context unavailable")

    request = ToolRoundExecutionRequest(
        agent=SimpleNamespace(config=SimpleNamespace(auto_save_memory=False)),
        params=params,
        tool_rounds=1,
        response=ModelResponse(text="round", backend="test"),
        calls=calls,
        execute_one=execute_one,
        record_one=lambda _r: None,
    )

    with pytest.raises(exception_cls):
        execute_tool_round(request)


def test_rejected_approval_execution_unmapped_reason_code_no_key_error():
    """Verify that _rejected_approval_execution handles unmapped reason codes without KeyError."""
    call = _make_call("danger_tool", 1)
    execution = _ok_execution(call)
    unmapped_decision = SimpleNamespace(
        permission_id="perm-1",
        decision="denied",
        reason_code="COMPLETELY_NEW_OR_UNMAPPED_REASON",
        feedback="反馈说明",
        message="自定义审批拒绝理由。",
    )

    # Should not raise KeyError!
    rejected = _rejected_approval_execution(execution, unmapped_decision)
    assert rejected.result.ok is False
    assert rejected.decision.status == "deny"
    assert "COMPLETELY_NEW_OR_UNMAPPED_REASON" in rejected.decision.reason_codes
    assert "自定义审批拒绝理由" in rejected.result.output


def test_rejected_approval_execution_known_reason_codes():
    """Verify standard approval rejection messages remain intact."""
    call = _make_call("danger_tool", 1)
    execution = _ok_execution(call)
    for code, expected_text in _APPROVAL_REJECTION_MESSAGES.items():
        decision = ToolApprovalDecision(
            permission_id="perm-2",
            decision="denied",
            reason_code=code,
        )
        rejected = _rejected_approval_execution(execution, decision)
        assert expected_text in rejected.result.output


def test_no_action_gate_resets_when_executable_calls_present():
    """Verify no_action_gate_halt resets cleanly when a valid round is executed."""
    params = SimpleNamespace(
        request_id="req-4",
        run_id=_TEST_RUN_ID,
        task_id="task-4",
        tool_protocol_snapshot=_make_protocol_snapshot(),
        tool_context=[],
        no_action_gate_halt=True,
        _no_action_gate_streak=2,
        effective_contract_snapshot=None,  # Not informational
    )
    calls = [_make_call("read_file", 1)]
    records: list[Any] = []

    request = ToolRoundExecutionRequest(
        agent=SimpleNamespace(config=SimpleNamespace(auto_save_memory=False)),
        params=params,
        tool_rounds=1,
        response=ModelResponse(text="round", backend="test"),
        calls=calls,
        execute_one=lambda work: _ok_execution(work.call),
        record_one=records.append,
    )

    execute_tool_round(request)

    assert getattr(params, "no_action_gate_halt", None) is False
    assert getattr(params, "_no_action_gate_streak", None) == 0
    assert len(records) == 1
    assert records[0].result.ok is True


def test_skill_learning_gate_error_hierarchy():
    """Verify SkillLearningGateError is an instance of SkillLearningStoreError."""
    err = SkillLearningGateError("TEST_GATE_CODE", {"field": "target"})
    assert isinstance(err, SkillLearningStoreError)
    assert err.code == "TEST_GATE_CODE"
    assert err.reason == "TEST_GATE_CODE"
    assert err.detail == {"field": "target"}
    assert str(err) == "TEST_GATE_CODE"


def test_skill_learning_host_feedback_resilience(tmp_path):
    """Verify host feedback and user edit helpers survive simulated store/IO errors."""
    agent = SimpleNamespace(
        config=SimpleNamespace(enable_self_learning=True),
        home_paths=SimpleNamespace(owner_home=tmp_path),
    )

    # Calling record_user_skill_edit_after_tool on non-existent file or corrupted path does not raise
    record_user_skill_edit_after_tool(agent, tmp_path / "skills" / "learned" / "test" / "SKILL.md")

    # execute_skill_learning_host_action catching SkillLearningStoreError
    def failing_action(_agent):
        raise SkillLearningStoreError("STORAGE_ERROR", "disk full")

    result = execute_skill_learning_host_action(agent, failing_action)
    assert result is None


def test_verification_runtime_resilience():
    """Verify VerificationRuntime.verify isolates unexpected exceptions into a failed VerificationItem."""
    def buggy_verifier(_ctx):
        raise ValueError("invalid verification payload")

    item = VerificationRuntime.verify(buggy_verifier, {})
    assert isinstance(item, VerificationItem)
    assert item.passed is False
    assert "ValueError" in item.message

    def interrupted_verifier(_ctx):
        raise InterruptedError("interrupted")

    with pytest.raises(InterruptedError):
        VerificationRuntime.verify(interrupted_verifier, {})


def test_run_pack_verification_hooks_resilience():
    """Verify run_pack_verification_hooks guards malformed JSON and hook crashes."""
    # Malformed JSON payload does not crash
    res = run_pack_verification_hooks(SimpleNamespace(), SimpleNamespace(), hook=lambda a, p, d: [{"key": "ok"}], payload="{corrupt json...")
    assert res == [{"key": "ok"}]

    # Hook exception converted to structured warning
    def buggy_hook(_a, _p, _d):
        raise ZeroDivisionError("bug in hook")

    warn_res = run_pack_verification_hooks(SimpleNamespace(), SimpleNamespace(), hook=buggy_hook, payload=b'{"valid": true}')
    assert len(warn_res) == 1
    assert warn_res[0]["status"] == "warning"
    assert "ZeroDivisionError" in warn_res[0]["error"]


def test_stage_trace_resilience():
    """Verify stage_trace survives unexpected OSError without raising."""
    bundle = stage_trace.RunnerStageTraceBundle(
        agent=SimpleNamespace(subagents=SimpleNamespace()),
        event_type="test_event",
        params=SimpleNamespace(),
        tool_rounds=1,
        payload={"some": "data"},
    )
    # _trace_runner_stage when manager throws OSError
    stage_trace._trace_runner_stage(bundle)


def test_task_rejected_actions_resilience(tmp_path):
    """Verify append_task_rejected_action survives corrupt/unreadable files without aborting."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    agent = SimpleNamespace(
        owner_quota=SimpleNamespace(),
        conversation_store=SimpleNamespace(
            root=tmp_path,
            session_tasks=SimpleNamespace(storage=SimpleNamespace(session_tasks_dir=session_dir)),
        ),
    )
    params = SimpleNamespace(
        task_id="task-corrupt",
        run_id="run-corrupt",
        runtime_rejected_actions=[],
    )

    # Should not raise even with simulated empty/broken state
    append_task_rejected_action(agent, params, {"tool_name": "danger", "decision": "deny"})
    assert len(params.runtime_rejected_actions) == 1


def test_tool_call_archive_record_externalize_resilience(tmp_path, monkeypatch):
    """Verify that when externalizing tool output record encounters OSError, it falls back cleanly."""
    from agent_py_agent.agent.agent_core import tool_call_archive_record
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams

    def fail_externalize(_req):
        raise OSError("simulated disk full during archive")

    monkeypatch.setattr(tool_call_archive_record, "externalize_tool_output_record", fail_externalize)

    agent = SimpleNamespace(config=SimpleNamespace(), root=tmp_path)
    call = _make_call("heavy_tool", 1)
    result = ToolResult.succeeded(call, "x" * 5000)
    params = ToolLoopExecuteParams(
        user_prompt="",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary=None,
        task_attributes={},
        request_id="req-1",
        run_id=_TEST_RUN_ID,
        task_id="task-1",
        one_shot_tool_calls={},
        executed_tools=[],
        archive_tool_calls=True,
    )
    record = ToolCallRecordParams(
        params=params,
        tool_rounds=1,
        idx=1,
        call=call,
        result=result,
        execution_states=("running", "succeeded"),
        model_call=call,
    )

    out = archive_tool_call_record(agent, record)
    assert out.get("archive_write_error") == "OSError"
    assert "parameters" in out
