from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.models import SubAgentTask
from agent_py_agent.agent.subagents.services.runner_result_commit import commit_runner_result
from agent_py_agent.agent.subagents.services.runtime_closeout import (
    deliver_parent_wake,
    pending_closeout,
)


@pytest.mark.parametrize("failure", ["", "pending", "settle", "notify", "delivered", "clear", "trace"])
def test_commit_with_explicit_dependencies_preserves_durable_order(failure):
    task = SubAgentTask(id="child", goal="g", thought="t", plan=[], status="DONE")
    params = RecordRunnerResultParams(
        run_id=task.id, dry_run=False, ok=True, message="", attempt_id="attempt-1", status="DONE",
    )
    result = SimpleNamespace(status="DONE", message="")
    calls = []
    durable = {}

    def save_task(saved):
        fact = pending_closeout(saved)
        stage = str(fact["delivery"]) if fact else "clear"
        calls.append(stage)
        if failure == stage:
            raise OSError("injected canonical write failure")
        durable.clear()
        durable.update(deepcopy(fact or {}))

    def settle(**kwargs):
        assert durable["attempt_id"] == kwargs["attempt_id"] == "attempt-1"
        assert kwargs["agent_run_id"] == "agent-1"
        assert kwargs["status"] == "done"
        calls.append("settle")
        if failure == "settle":
            raise OSError("injected runtime write failure")
        return {"settled": True}

    def notify():
        calls.append("notify")
        assert durable["delivery"] == "pending"
        if failure == "notify":
            raise OSError("injected notification failure")
        return "delivered"

    def deliver():
        calls.append("trace")
        if failure == "trace":
            raise OSError("injected trace failure")
        return deliver_parent_wake(notify)

    repo = SimpleNamespace(
        agent_run_for_run_id=lambda run_id: {"agent_run_id": "agent-1"},
        settle_agent_run=settle,
        append_event=lambda **kwargs: calls.append(kwargs["event_type"]),
    )
    if failure == "trace":
        with pytest.raises(OSError, match="trace failure"):
            commit_runner_result(repo, task, params, result, save_task=save_task, deliver_result=deliver)
    else:
        commit_runner_result(repo, task, params, result, save_task=save_task, deliver_result=deliver)

    expected = {
        "": ["pending", "settle", "trace", "notify", "delivered", "clear"],
        "pending": ["pending", "pending", "closeout_unpersisted"],
        "settle": ["pending", "settle", "pending", "closeout_pending"],
        "notify": ["pending", "settle", "trace", "notify", "pending"],
        "delivered": ["pending", "settle", "trace", "notify", "delivered"],
        "clear": ["pending", "settle", "trace", "notify", "delivered", "clear"],
        "trace": ["pending", "settle", "trace"],
    }
    assert calls == expected[failure]
    if failure in {"settle", "notify", "delivered", "trace"}:
        assert durable["delivery"] == "pending"
    elif failure == "clear":
        assert durable["delivery"] == "delivered"
    else:
        assert durable == {}


def test_nonterminal_result_does_not_settle_or_write_wal():
    task = SubAgentTask(id="child", goal="g", thought="t", plan=[], status="PENDING")
    params = RecordRunnerResultParams(run_id=task.id, dry_run=False, ok=False, message="")
    calls = []
    commit_runner_result(
        object(), task, params, SimpleNamespace(status="PENDING"),
        save_task=lambda task: pytest.fail("waiting result must not write closeout WAL"),
        deliver_result=lambda: calls.append("deliver") or "skipped",
    )
    assert calls == ["deliver"]
