"""恢复日志去重不代替权威查询，不可读也不能放行后台副作用。"""
from __future__ import annotations

import logging

import pytest

from agent_py_agent.agent.conversation.background_recovery import BackgroundRecoveryGuard


def test_recovery_fingerprint_only_suppresses_logs_and_current_checker_is_used(caplog):
    calls = []
    state = {"block": {"task_id": "task", "reason": "unknown"}}

    def check(task_id):
        calls.append(task_id)
        return state["block"]

    current = {"checker": check}
    guard = BackgroundRecoveryGuard(lambda: current["checker"])
    with caplog.at_level(logging.INFO):
        assert guard.block_for_task(" task ") is state["block"]
        assert guard.block_for_task("task") is state["block"]
        state["block"] = {"task_id": "task", "reason": "other_block"}
        assert guard.block_for_task("task") is state["block"]
        state["block"] = None
        assert guard.block_for_task("task") is None
        assert guard.block_for_task("task") is None
        current["checker"] = lambda task_id: {"task_id": task_id, "reason": "new_repository"}
        assert guard.block_for_task("task")["reason"] == "new_repository"
    assert calls == ["task"] * 5
    messages = [record.message for record in caplog.records]
    assert sum("BACKGROUND_AUTHORITY_RECOVERY_BLOCK" in msg for msg in messages) == 3
    assert sum("BACKGROUND_AUTHORITY_RECOVERY_RESUMED" in msg for msg in messages) == 1


def test_unreadable_recovery_returns_structured_block_each_time(caplog):
    calls = []

    def check(task_id):
        calls.append(task_id)
        raise OSError("private detail")

    guard = BackgroundRecoveryGuard(lambda: check)
    first = guard.block_for_task("task")
    second = guard.block_for_task("task")
    assert first == second == {
        "schema_version": "main-agent-recovery-block.v1", "reason": "authority_state_unreadable",
        "task_id": "task", "error_type": "OSError",
    }
    assert first is not second and calls == ["task", "task"]
    assert len(caplog.records) == 1 and "private detail" not in caplog.text


def test_blank_task_does_not_resolve_checker_and_missing_checker_does_not_block():
    calls = []
    guard = BackgroundRecoveryGuard(lambda: calls.append("lookup"))
    assert guard.block_for_task("  ") is None and calls == []
    assert guard.block_for_task("task") is None and calls == ["lookup"]


def test_checker_resolution_error_retains_original_propagation_boundary():
    failure = RuntimeError("lookup failed")

    def lookup():
        raise failure

    guard = BackgroundRecoveryGuard(lookup)
    with pytest.raises(RuntimeError) as raised:
        guard.block_for_task("task")
    assert raised.value is failure
