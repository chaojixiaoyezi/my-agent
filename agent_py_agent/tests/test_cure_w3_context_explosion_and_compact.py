"""Behavioral tests for Workstream W3: context explosion control and compact resilience."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.agent_tree.status import (
    SubagentKernelQuery,
    _kernel_query,
)
from agent_py_agent.agent.agent_core.runtime.loop_support import (
    _persist_partial_native_turn,
    _replayable_turn_history,
)
from agent_py_agent.agent.agent_core.runtime.turn_trigger import TurnTrigger
from agent_py_agent.agent.backends.errors import (
    ProviderQuotaExhaustedError,
    ProviderTimeoutError,
    ProviderTransientError,
)
from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn, UserTurn
from agent_py_agent.agent.conversation.background_context import (
    BackgroundContextLoad,
    _agent_tree_payload,
    _recovery_snapshot,
)
from agent_py_agent.agent.conversation.compact import (
    _sanitize_failed_turn_projections,
)
from agent_py_agent.agent.conversation.compact_guard import (
    ConversationCompactError,
    compact_degradation_decision,
    compact_failure_is_degradable,
    is_compact_transient_provider_failure,
)
from agent_py_agent.agent.conversation.context_budget import (
    BackgroundContextBudget,
    _bounded_value,
    _reference_skeleton,
)
from agent_py_agent.agent.conversation.models import MessageLogEntry
from agent_py_agent.agent.conversation.native_history import (
    CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
)
from agent_py_agent.agent.conversation.wake_poison import (
    WAKE_VERDICT_NEUTRAL,
    _is_transient,
    verdict_for_error,
)
from agent_py_agent.cli.gateway_lane_retry import BackgroundLaneRetry
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot


def _make_test_params(
    *,
    executed_tools: list[str] | None = None,
    archive_tool_calls: list[Any] | None = None,
    tool_ir: list[Any] | None = None,
) -> ToolLoopExecuteParams:
    ir = list(tool_ir) if tool_ir is not None else [UserTurn("# User Task\ntest task")]
    return ToolLoopExecuteParams(
        user_prompt="test",
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
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=list(executed_tools or []),
        archive_tool_calls=list(archive_tool_calls or []),
        save=False,
        delivery_contract={},
        tool_ir_history=ir,
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id="run-1", source_protocol="native"
        ),
    )


def test_agent_tree_thread_scope_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify thread scope fallback when _current_run_params is unset."""
    agent = SimpleNamespace()
    params = {"conversation_thread_id": "thread-456"}
    query = _kernel_query(agent, params)
    assert query.scope == "conversation_thread"
    assert query.conversation_thread_id == "thread-456"

    recorded_tree_params: dict[str, Any] = {}

    def fake_status_payload(_ag: object, p: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal recorded_tree_params
        recorded_tree_params = p or {}
        return {"nodes": []}

    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.background_context.agent_tree_status_payload",
        fake_status_payload,
    )

    load_state = BackgroundContextLoad(
        agent=SimpleNamespace(),
        store=SimpleNamespace(),
        thread=SimpleNamespace(thread_id="thread-test-789"),
        task_id="task-1",
        config=SimpleNamespace(),
        policy_request=None,
        load_errors=[],
    )
    _agent_tree_payload(load_state, ["run-1"])
    assert recorded_tree_params.get("conversation_thread_id") == "thread-test-789"

    recorded_recovery_params: dict[str, Any] = {}

    def fake_recovery_status(_ag: object, p: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal recorded_recovery_params
        recorded_recovery_params = p or {}
        return {"nodes": []}

    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.background_context.agent_tree_status_payload",
        fake_recovery_status,
    )
    fake_store = SimpleNamespace(claims=SimpleNamespace(load=lambda _tid: {}))
    _recovery_snapshot(SimpleNamespace(), fake_store, "thread-recovery-42", ["run-2"])
    assert recorded_recovery_params.get("conversation_thread_id") == "thread-recovery-42"


def test_context_budget_bounded_reference_skeleton() -> None:
    """Verify bounded recursion over large collections under active budget."""
    items = [
        {
            "run_id": f"run-{i}",
            "task_id": f"task-{i}",
            "summary": f"Deterministic execution task record {i}" * 4,
        }
        for i in range(2500)
    ]
    budget = BackgroundContextBudget(max_list_items=16, max_dict_items=32, max_depth=4)
    bounded = _bounded_value(items, budget)
    assert isinstance(bounded, list)
    assert len(bounded) <= 17
    last = bounded[-1]
    assert isinstance(last, dict)
    assert (
        last.get("truncated") is True
        or "_truncated_ref_items" in last
        or "omitted_items" in last
    )

    skeleton = _reference_skeleton(items, budget)
    assert isinstance(skeleton, list)
    assert len(skeleton) <= 17


def test_turn_history_and_partial_native_turn_preservation() -> None:
    """Verify selective runtime injection stripping and pre-execution turn suppression."""
    runtime_injection = RuntimeFactsTurn(
        text="[background-main-agent-context] large injection",
        source="prompt.runtime_injection",
    )
    user_turn = UserTurn(text="task request")
    history = [runtime_injection, user_turn]

    # Ordinary turn (trigger is None): kept 100% intact
    ordinary = _replayable_turn_history(history, None)
    assert len(ordinary) == 2
    assert ordinary[0] is runtime_injection

    # Lifecycle wake trigger: stripped
    wake_trigger = TurnTrigger(kind="lifecycle_wake")
    wake_history = _replayable_turn_history(history, wake_trigger)
    assert len(wake_history) == 1
    assert wake_history[0] is user_turn

    # Session task trigger: stripped
    task_trigger = TurnTrigger(kind="session_task")
    task_history = _replayable_turn_history(history, task_trigger)
    assert len(task_history) == 1
    assert task_history[0] is user_turn

    # Partial turn persistence behavior
    persisted: list[Any] = []

    def callback(r: Any) -> None:
        persisted.append(r)

    params = _make_test_params()

    # Pre-execution model failure with no executed tools: do not write empty error row
    _persist_partial_native_turn(
        callback, params, RuntimeError("pre-execution connection failure")
    )
    assert len(persisted) == 0

    # User cancellation (InterruptedError): must persist
    _persist_partial_native_turn(callback, params, InterruptedError("user stop"))
    assert len(persisted) == 1
    assert persisted[0].runtime_status == "cancelled"

    # Turn with executed tools: must persist failure snapshot
    params_with_tools = _make_test_params(executed_tools=["run_command"])
    _persist_partial_native_turn(
        callback, params_with_tools, RuntimeError("execution failure")
    )
    assert len(persisted) == 2
    assert persisted[1].runtime_status == "failed"
    assert persisted[1].executed_tools == ["run_command"]


def test_compact_transient_failure_and_headless_lane_retry() -> None:
    """Verify transient compact provider failure classification and headless retry handling."""
    transient_cause = ProviderTransientError("rate limit exceeded")
    compact_err = ConversationCompactError("compact failed", code="COMPACT_FAILED")
    compact_err.__cause__ = transient_cause
    assert is_compact_transient_provider_failure(compact_err) is True

    timeout_err = ConversationCompactError(
        "compact timeout", code="COMPACT_PROVIDER_TIMEOUT"
    )
    assert is_compact_transient_provider_failure(timeout_err) is True

    quota_err = ConversationCompactError(
        "quota limit", code="COMPACT_PROVIDER_QUOTA_EXHAUSTED"
    )
    assert is_compact_transient_provider_failure(quota_err) is False

    standard_err = ConversationCompactError("standard error", code="COMPACT_UNKNOWN")
    assert is_compact_transient_provider_failure(standard_err) is False

    # Lane retry handling
    retries = BackgroundLaneRetry()
    retries.failed("owner1", "t1", compact_err, delay=5.0, headless=False)
    row1 = retries._failures[("owner1", "t1")]
    assert row1.waiting_for_user_input is False
    assert row1.retry_at > 0.0

    permanent_err = ConversationCompactError(
        "candidate exceeds ceiling", code="COMPACT_CANDIDATE_TOO_LARGE"
    )
    retries.failed("owner1", "t2", permanent_err, delay=5.0, headless=False)
    row2 = retries._failures[("owner1", "t2")]
    assert row2.waiting_for_user_input is True

    # Headless lane never waits for user input
    retries.failed("owner1", "t3", permanent_err, delay=5.0, headless=True)
    row3 = retries._failures[("owner1", "t3")]
    assert row3.waiting_for_user_input is False
    assert row3.retry_at > 0.0

    # Wake poison inspection
    assert _is_transient(compact_err) is True
    assert _is_transient(timeout_err) is True
    verdict = verdict_for_error(compact_err)
    assert verdict.kind == WAKE_VERDICT_NEUTRAL


def test_compact_degradation_and_failed_turn_sanitization() -> None:
    """Verify soft-trigger compact degradation and legacy oversized failed-turn self-healing."""
    err_large = ConversationCompactError(
        "candidate too large", code="COMPACT_CANDIDATE_TOO_LARGE"
    )
    err_empty = ConversationCompactError(
        "compact source empty", code="COMPACT_SOURCE_EMPTY"
    )

    allowed_large, reason_large = compact_degradation_decision(
        err_large, degrade_eligible=True
    )
    assert allowed_large is True
    assert "CANDIDATE_TOO_LARGE" in reason_large

    allowed_empty, reason_empty = compact_degradation_decision(
        err_empty, degrade_eligible=True
    )
    assert allowed_empty is True
    assert "SOURCE_EMPTY" in reason_empty

    allowed_disallowed, _ = compact_degradation_decision(
        err_large, degrade_eligible=False
    )
    assert allowed_disallowed is False

    # Backwards compatibility check
    assert (
        compact_failure_is_degradable(err_large, degrade_eligible=True)[0] is True
    )
    assert (
        compact_failure_is_degradable(err_large, degrade_eligible=False)[0] is False
    )

    # Legacy oversized failed-turn excerpting
    huge_prompt = "Simulated context injection text\n" * 500
    assert len(huge_prompt) > 8_000
    failed_envelope = {
        "schema": "conversation_native_messages.v1",
        "messages": [
            {"role": "user", "content": huge_prompt},
            {"role": "assistant", "content": ""},
        ],
    }
    failed_row = MessageLogEntry(
        message_id="msg-failed-turn-0",
        thread_id="t-legacy-poison",
        role="assistant",
        content="",
        metadata={
            "turn_end_reason": "error",
            "runtime_status": "failed",
            CANONICAL_NATIVE_MESSAGES_METADATA_KEY: failed_envelope,
        },
    )
    active_row = MessageLogEntry(
        message_id="msg-turn-1",
        thread_id="t-legacy-poison",
        role="user",
        content="Active user task message",
    )
    rows = [failed_row, active_row]
    sanitized = _sanitize_failed_turn_projections(rows)
    assert len(sanitized) == 2
    sanitized_env = sanitized[0].metadata[CANONICAL_NATIVE_MESSAGES_METADATA_KEY]
    sanitized_content = sanitized_env["messages"][0]["content"]
    assert len(sanitized_content) <= 4_100
    assert "...[truncated excerpt]" in sanitized_content
    assert sanitized[1].content == "Active user task message"
    # Ensure immutability of the source rows
    assert (
        len(
            rows[0].metadata[CANONICAL_NATIVE_MESSAGES_METADATA_KEY]["messages"][0][
                "content"
            ]
        )
        > 8_000
    )
