"""W4：使用真实canonical事务验证过期申请与授权交错。"""
import pytest

from agent_py_agent.agent.subagents.capability_request_expiry import close_stale_capability_requests
from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityRequestParams
from agent_py_agent.tests.test_gateway_unpromoted_stop import hot as hot


def _blocked(hot, *, parent="task", goal="exact duty"):
    manager = hot.agent.subagents
    task = manager.create_run(goal=goal, parent_id=parent, root_id=parent, depth=1, role="worker")
    request = manager.lifecycle.record_capability_request(task.id, RecordCapabilityRequestParams(
        problem="need permission", needed_capability="filesystem", requested_tools=["write_file"],
    ))

    def mark(current):
        current.status = "BLOCKED"
        current.failure_type = "capability_request"
        current.runner_active_attempt_id = ""

    manager.mutate(task.id, mark)
    return manager.load(task.id), request


def test_parent_interruption_closes_open_and_drops_blocked_priority(hot):
    from agent_py_agent.agent.conversation.agent_activity import (
        _subagent_sort_key,
        subagent_display_label,
    )

    task, request = _blocked(hot)
    manager = hot.agent.subagents
    assert close_stale_capability_requests(manager, parent_task_id="task", reason="parent_turn_interrupted") == [task.id]
    closed = manager.load(task.id)
    assert closed.capability_requests[0].status == "CLOSED"
    assert closed.attributes["capability_request_closeout"]["resolution"]["reason"] == "parent_turn_interrupted"
    assert closed.capability_requests[0].constraints["resolution_reason"] == "parent_turn_interrupted"
    assert closed.status == "CANCELLED" and closed.ended_at > 0
    assert subagent_display_label(closed) == "已过期"
    assert _subagent_sort_key(closed)[0] > _subagent_sort_key(task)[0]
    assert close_stale_capability_requests(manager, parent_task_id="task", reason="parent_turn_ended") == []
    assert manager.load(task.id).capability_requests[0].id == request.id


def test_granted_request_is_not_expired(hot):
    task, _ = _blocked(hot)
    manager = hot.agent.subagents
    manager.mutate(task.id, lambda current: setattr(current.capability_requests[0], "status", "GRANTED"))
    assert close_stale_capability_requests(manager, parent_task_id="task", reason="parent_turn_ended") == []
    assert manager.load(task.id).capability_requests[0].status == "GRANTED"


def test_replacement_requires_exact_parent_role_goal(hot):
    task, _ = _blocked(hot)
    manager = hot.agent.subagents
    other, _ = _blocked(hot, goal="not same duty")
    assert close_stale_capability_requests(manager, parent_task_id="task", reason="replaced", replaced_by_run_id=other.id) == []
    replacement = manager.create_run(goal=task.goal, parent_id="task", root_id="task", depth=1, role=task.role)
    assert close_stale_capability_requests(manager, parent_task_id="task", reason="replaced", replaced_by_run_id=replacement.id) == [task.id]
    resolution = manager.load(task.id).attributes["capability_request_closeout"]["resolution"]
    assert resolution == {"reason": "replaced", "replaced_by_run_id": replacement.id}
    assert manager.load(other.id).capability_requests[0].status == "OPEN"


def test_closeout_exact_snapshot_does_not_touch_later_child(hot):
    old, _ = _blocked(hot)
    newer, _ = _blocked(hot)
    closed = close_stale_capability_requests(hot.agent.subagents, parent_task_id="task", reason="user_denied", run_ids=(old.id,))
    assert closed == [old.id]
    assert hot.agent.subagents.load(newer.id).capability_requests[0].status == "OPEN"


def test_invalid_closeout_reason_is_rejected(hot):
    with pytest.raises(ValueError):
        close_stale_capability_requests(hot.agent.subagents, parent_task_id="task", reason="guessed")


def test_confirmed_interrupt_closes_parent_requests(hot):
    from agent_py_agent.agent.gateway_parts import control_service

    task, _ = _blocked(hot)
    active = control_service._GatewayRequestRecord(None, {
        "id": "task", "conversation_thread_id": hot.scope.thread_id,
    }, "task")
    result = control_service._turn_interrupted_result(hot.agent, active, hot.control_scope)
    assert result.ok
    assert hot.agent.subagents.load(task.id).capability_requests[0].status == "CLOSED"


def test_committed_parent_terminal_closes_only_frozen_children(hot, monkeypatch):
    from agent_py_agent.agent.gateway_parts import request_worker as worker
    from agent_py_agent.tests.test_v13_w4_conversation_steer_stop import (
        _promote_same_request_without_backfill,
    )

    _promote_same_request_without_backfill(hot)
    old, _ = _blocked(hot, parent="message")
    archive = worker.archive_request
    later = []

    def interleaved(*args, **kwargs):
        later.append(_blocked(hot, parent="message")[0])
        return archive(*args, **kwargs)

    monkeypatch.setattr(worker, "archive_request", interleaved)
    worker._finish_claimed_gateway_request(hot.paths, hot.path, "message", {
        "id": "message", "status": "done", "ok": True, "response": "done",
    }, conversation_store=hot.agent.conversation_store, execution_agent=hot.agent)
    assert (hot.paths.terminal / "message.json").exists()
    assert hot.agent.subagents.load(old.id).capability_requests[0].status == "CLOSED"
    assert hot.agent.subagents.load(later[0].id).capability_requests[0].status == "OPEN"


@pytest.mark.parametrize("grant_first", [True, False])
def test_grant_and_expiry_are_serialized_by_canonical_transaction(hot, grant_first):
    import threading

    from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityGrantParams

    manager = hot.agent.subagents
    task, request = _blocked(hot)
    first_committed, release = threading.Event(), threading.Event()
    errors = []

    def grant():
        manager.lifecycle.record_capability_grant(task.id, RecordCapabilityGrantParams(
            request_id=request.id, grant_type="tool", tools=["write_file"], reason="explicit parent grant",
        ))

    def expire():
        return close_stale_capability_requests(manager, parent_task_id="task", reason="parent_turn_ended")

    def first():
        try:
            (grant if grant_first else expire)()
            first_committed.set()
            assert release.wait(10)
        except BaseException as exc:
            errors.append(exc)
            first_committed.set()

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert first_committed.wait(10)
        assert not errors
        if grant_first:
            assert expire() == []
        else:
            with pytest.raises(ValueError):
                grant()
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors
    current = manager.load(task.id)
    assert current.capability_requests[0].status == ("GRANTED" if grant_first else "CLOSED")
    assert not current.runner_active_attempt_id
