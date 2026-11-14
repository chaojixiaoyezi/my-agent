"""R4-02：原业务入口/调度器/输入账本的取消及退休回归，只控制外部模型。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation.background_team_lifecycle import identity_outstanding
from agent_py_agent.tests.test_business_v12_f3_agents import (
    _service,
)
from agent_py_agent.tests.test_business_v12_f3_agents import (
    business_host as business_host,
)
from agent_py_agent.tests.test_mega_background_lifecycle_controls import (
    edit_task,
    eventually_retired,
    native_cancellation_case,
    ready_index,
)


def failed_provider_case(tmp_path, *, request_timeout=0):
    agent, original, scheduler, call, task_id, view = native_cancellation_case(
        tmp_path, request_timeout=request_timeout)

    class FailedProvider(type(original)):
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            raise ConnectionError("single original provider connection failure")

    agent.backend = backend = FailedProvider()
    with pytest.raises(ConnectionError, match="single original provider connection failure"):
        scheduler.tick()
    store = agent.conversation_store
    task = store.session_tasks.require(task_id)
    assert backend.calls == 1
    assert task.conversation_request_id and task.body_guidance_id
    assert store.guidance.receipt(task.body_dedupe_key).status == "pending"
    claim = store.claims.load(task.target_thread_id)
    assert claim["status"] == "failed"
    assert agent.subagents.runtime_db.get_agent_run_exec_lock(claim["runtime_authority"]["agent_run_id"]) is None
    return agent, backend, scheduler, call, task, view


@pytest.mark.parametrize("request_timeout", [0, 240])
def test_failed_provider_cancel_settles_exact_original_body_and_retires(tmp_path, request_timeout):
    agent, backend, scheduler, call, task, view = failed_provider_case(tmp_path, request_timeout=request_timeout)
    store = agent.conversation_store
    cancelled = call("tasks.cancel", {"task_id": task.task_id, "include_descendants": True})
    assert cancelled["state"] == "completed" and not cancelled["outstanding_refs"]
    assert store.guidance.receipt(task.body_dedupe_key).status == "rejected"
    assert call("tasks.cancel", {"task_id": task.task_id, "include_descendants": True}) == cancelled
    original_wakes = [wake for wake in store.wakes.pending(limit=0)
                      if wake.metadata.get("session_task_id") == task.task_id]
    assert len(original_wakes) == 1
    retry, error = store.wakes.attempts.state_report(original_wakes[0].wake_signal_id)
    assert error is None and retry.next_attempt_at > original_wakes[0].created_at
    # 保留真实失败退避；通过原调度器时钟推进到它自己的耐久重试期限，再处理已取消的原wake。
    scheduler.tick(now=retry.next_attempt_at)
    assert original_wakes[0].wake_signal_id not in {wake.wake_signal_id for wake in store.wakes.pending(limit=0)}
    ready_index(store)
    call("background_agents.retire", {"agent_id": view["agent_id"], "disposition": "drain"},
         expected_revision=view["revision"], idempotency_key="r4-failed-provider-retire")
    retired = eventually_retired(agent, view["agent_id"])
    assert identity_outstanding(agent, retired) == ([], [])
    assert backend.calls == 1


def test_suspended_accepted_message_drains_once_and_retirement_replays(tmp_path):
    agent, original, scheduler, call, task_id, view = native_cancellation_case(tmp_path)
    store = agent.conversation_store
    assert call("tasks.cancel", {"task_id": task_id, "include_descendants": False})["state"] == "completed"
    scheduler.tick()
    assert original.calls == 0
    suspended = call("background_agents.suspend", {"agent_id": view["agent_id"]},
        expected_revision=view["revision"], idempotency_key="r4-suspend")
    accepted = call("background_agents.messages.send", {"agent_id": view["agent_id"],
        "text": "Drain this previously accepted original input.", "input_kind": "guidance"},
        idempotency_key="r4-original-message")
    assert accepted["receipt_state"] == "accepted"
    inputs, errors = store.guidance.pending_report("thread", view["agent_id"], limit=0)
    assert not errors and len(inputs) == 1
    entry = inputs[0]
    assert store.wakes.pending(limit=0) == []
    prompts = []

    class DrainingProvider(type(original)):
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            prompts.append(prompt)
            return ModelResponse(text="Original accepted input drained.", backend=self.name)

    agent.backend = backend = DrainingProvider()
    options = {"expected_revision": suspended["agent"]["revision"], "idempotency_key": "r4-drain"}
    retiring = call("background_agents.retire", {"agent_id": view["agent_id"], "disposition": "drain"}, **options)
    assert retiring["agent"]["identity_state"] == "retiring" and retiring["outstanding_refs"]
    wakes = [wake for wake in store.wakes.pending(limit=0) if wake.thread_id == view["agent_id"]]
    assert len(wakes) == 1, "retire(drain) must restore the accepted input's original wake"
    for _ in range(2):
        call("background_agents.retire", {"agent_id": view["agent_id"], "disposition": "drain"}, **options)
    assert [wake.wake_signal_id for wake in store.wakes.pending(limit=0)] == [wakes[0].wake_signal_id]
    assert backend.calls == 0, "identity control must not execute the model"
    scheduler.tick()
    assert backend.calls == 1 and len(prompts) == 1
    assert store.guidance.receipt(entry.metadata["dedupe_key"]).status == "consumed"
    retired = eventually_retired(agent, view["agent_id"])
    assert identity_outstanding(agent, retired) == ([], [])
    for _ in range(2):
        call("background_agents.retire", {"agent_id": view["agent_id"], "disposition": "drain"}, **options)
        scheduler.tick()
    assert backend.calls == 1
    assert len(store.guidance.recent("thread", view["agent_id"], limit=0)) == 2  # original cancelled body + message


def test_healthy_queued_cancellation_retires_without_model_execution(tmp_path):
    agent, backend, scheduler, call, task_id, view = native_cancellation_case(tmp_path)
    store = agent.conversation_store
    task = store.session_tasks.require(task_id)
    cancelled = call("tasks.cancel", {"task_id": task_id, "include_descendants": True})
    assert cancelled["state"] == "completed" and not cancelled["outstanding_refs"]
    assert store.guidance.receipt(task.body_dedupe_key).status == "rejected"
    assert call("tasks.cancel", {"task_id": task_id, "include_descendants": True}) == cancelled
    scheduler.tick()
    ready_index(store)
    call("background_agents.retire", {"agent_id": view["agent_id"], "disposition": "drain"},
        expected_revision=view["revision"], idempotency_key="r4-healthy-retire")
    assert identity_outstanding(agent, eventually_retired(agent, view["agent_id"])) == ([], [])
    assert backend.calls == 0


@pytest.fixture
def queued_business_task(business_host):
    service, host, actor = _service(business_host)
    view = service.create({"space_id": "space-A", "name": "retirement guards"},
        actor=actor, idempotency_key="r4-guards-worker")["agent"]
    task_id = service.assign({"agent_id": view["agent_id"], "space_id": "space-A", "instruction": "original body"},
        actor=actor, idempotency_key="r4-guards-task")["task_id"]
    ready_index(host.conversation_store)
    return service, host, actor, host.conversation_store.session_tasks.require(task_id), view


# LLM: 精确停止只结算已取消业务任务的原预留正文；同回合其它输入和提交不明回执不能连带收口。
# 函数用途: 验证取消重试真正结清原正文，而不是仅清掉展示引用。
@pytest.mark.parametrize("state", ["reserved", "submitted"])
def test_confirmed_cancel_settles_only_exact_reserved_body(queued_business_task, monkeypatch, state):
    import time

    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
    from agent_py_agent.agent.gateway_parts import session_task_stop
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic

    service, host, actor, task, _ = queued_business_task
    store = host.conversation_store
    receipt = store.guidance.receipt(task.body_dedupe_key)
    assert store.guidance.claim_for_turn(receipt.entry, expected_turn_id="exact-turn",
        attempt_id="original-attempt", owning_task_id=task.task_id)
    if state == "submitted":
        receipt = store.guidance.receipt(task.body_dedupe_key)
        write_json_file_atomic(store.storage.guidance_dedupe_path(task.body_dedupe_key),
            replace(receipt, status="submitted", submission_id="original-submission", submitted_at=time.time()).to_dict())
    store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="accepted", conversation_request_id="exact-turn"))
    unrelated = store.guidance.append_once({"target_type": "thread", "target_id": task.target_thread_id,
        "message": "unrelated input in the same turn", "sender": actor.source_thread_id,
        "metadata": {"expected_turn_id": "exact-turn"}}, dedupe_key="r4-same-turn-unrelated")
    confirmed, calls = [False], []

    def stop(host, *, target_thread, turn_id, canonical_task):
        calls.append(turn_id)
        assert canonical_task.task_id == task.task_id and target_thread.thread_id == task.target_thread_id
        return session_task_stop.SessionTaskStopOutcome(confirmed[0], "original execution observation", turn_id)

    monkeypatch.setattr(session_task_stop, "stop_session_task_turn", stop)
    assert service.cancel_task(task.task_id, actor=actor) == {
        "operation_id": task.task_id, "state": "pending", "outstanding_refs": ["exact-turn"]}
    assert store.session_tasks.require(task.task_id).status == "cancelled"
    assert store.guidance.receipt(task.body_dedupe_key).status == state
    confirmed[0] = True
    expected = {"operation_id": task.task_id,
        "state": "completed" if state == "reserved" else "pending",
        "outstanding_refs": [] if state == "reserved" else [task.task_id]}
    assert service.cancel_task(task.task_id, actor=actor) == expected
    assert service.cancel_task(task.task_id, actor=actor) == expected
    assert store.guidance.receipt(task.body_dedupe_key).status == ("rejected" if state == "reserved" else "submitted")
    assert store.guidance.receipt("r4-same-turn-unrelated").status == "pending"
    assert store.guidance.receipt("r4-same-turn-unrelated").entry.guidance_id == unrelated.guidance_id
    assert calls == ["exact-turn"] * (2 if state == "reserved" else 3)


def test_terminal_cancel_retry_settles_original_pending_body(queued_business_task):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate

    service, host, actor, task, _ = queued_business_task
    store = host.conversation_store
    store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="cancelled"))
    reply = service.cancel_task(task.task_id, actor=actor)
    assert reply["state"] == "completed" and not reply["outstanding_refs"]
    assert store.guidance.receipt(task.body_dedupe_key).status == "rejected"
    assert service.cancel_task(task.task_id, actor=actor) == reply


def test_body_write_failure_preserves_cancelled_task_and_same_retry(queued_business_task, monkeypatch):
    service, host, actor, task, _ = queued_business_task
    store = host.conversation_store
    mark_status, attempts = store.guidance.mark_status, []

    def fail_once(key, status):
        attempts.append((key, status))
        assert json.loads(store.session_tasks._path(task.task_id).read_text())["status"] == "cancelled", "cancel must be durable before input cleanup"
        if len(attempts) == 1:
            raise OSError("one exact body cleanup write failed")
        return mark_status(key, status)

    monkeypatch.setattr(store.guidance, "mark_status", fail_once)
    pending = service.cancel_task(task.task_id, actor=actor)
    assert pending["state"] == "pending" and pending["outstanding_refs"] == [task.task_id]
    assert store.session_tasks.require(task.task_id).status == "cancelled"
    assert store.guidance.receipt(task.body_dedupe_key).status == "pending"
    settled = service.cancel_task(task.task_id, actor=actor)
    assert settled == {"operation_id": task.task_id, "state": "completed", "outstanding_refs": []}
    assert store.guidance.receipt(task.body_dedupe_key).status == "rejected"
    assert attempts == [(task.body_dedupe_key, "rejected")] * 2
    assert service.cancel_task(task.task_id, actor=actor) == settled


@pytest.mark.parametrize("state", ["reserved", "submitted"])
def test_reserved_or_submitted_input_stays_outstanding_after_execution_exit(queued_business_task, monkeypatch, state):
    import time

    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
    from agent_py_agent.agent.gateway_parts import session_task_stop
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic

    service, host, actor, task, _ = queued_business_task
    store = host.conversation_store
    receipt = store.guidance.receipt(task.body_dedupe_key)
    assert store.guidance.claim_for_turn(receipt.entry, expected_turn_id="original-turn",
        attempt_id="original-attempt", owning_task_id=task.task_id)
    if state == "submitted":
        receipt = store.guidance.receipt(task.body_dedupe_key)
        write_json_file_atomic(store.storage.guidance_dedupe_path(task.body_dedupe_key),
            replace(receipt, status="submitted", submission_id="original-submission", submitted_at=time.time()).to_dict())
    store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="accepted", conversation_request_id="original-turn"))
    store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="failed"))
    stopped = []

    def confirmed(host, *, target_thread, turn_id, canonical_task):
        stopped.append(turn_id)
        assert turn_id == "original-turn" and canonical_task.task_id == task.task_id
        return session_task_stop.SessionTaskStopOutcome(True, "original execution exited", turn_id)

    monkeypatch.setattr(session_task_stop, "stop_session_task_turn", confirmed)
    for _ in range(2):
        reply = service.cancel_task(task.task_id, actor=actor)
        assert reply["state"] == "pending" and task.task_id in reply["outstanding_refs"]
        assert store.guidance.receipt(task.body_dedupe_key).status == state
    assert stopped == ["original-turn"] * 2


def test_unreadable_body_is_retryable_on_original_cancellation(queued_business_task):
    import json

    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic

    service, host, actor, task, _ = queued_business_task
    store = host.conversation_store
    path = store.storage.guidance_dedupe_path(task.body_dedupe_key)
    original = json.loads(path.read_text())
    path.write_text("{broken original receipt")
    for _ in range(2):
        reply = service.cancel_task(task.task_id, actor=actor)
        assert reply["state"] == "pending" and task.task_id in reply["outstanding_refs"]
        assert path.read_text() == "{broken original receipt"
    write_json_file_atomic(path, original)
    assert service.cancel_task(task.task_id, actor=actor)["state"] == "completed"
    assert store.guidance.receipt(task.body_dedupe_key).status == "rejected"


@pytest.mark.parametrize("field", ["guidance_id", "sender", "target_type", "target_id", "origin_kind",
    "origin_thread_id", "session_task_id", "expected_turn_id", "session_task_status"])
def test_mismatched_original_body_identity_is_not_settled(queued_business_task, field):
    service, host, actor, task, _ = queued_business_task
    store = host.conversation_store
    request = {"target_type": "thread", "target_id": task.target_thread_id, "message": "protected input",
        "sender": task.sender_thread_id, "metadata": {"origin_kind": "session_task",
        "origin_thread_id": task.sender_thread_id, "session_task_id": task.task_id}}
    if field in {"sender", "target_type", "target_id"}:
        request[field] = "agent_run" if field == "target_type" else "unrelated-source-or-target"
    elif field != "guidance_id":
        request["metadata"][field] = "unrelated-identity"
    key = "r4-protected-body:" + field
    entry = store.guidance.append_once(request, dedupe_key=key)
    edit_task(store, task.task_id, lambda current: replace(current, body_dedupe_key=key,
        body_guidance_id="different-guidance-id" if field == "guidance_id" else entry.guidance_id))
    for _ in range(2):
        reply = service.cancel_task(task.task_id, actor=actor)
        assert reply["state"] == "pending" and task.task_id in reply["outstanding_refs"]
        assert store.guidance.receipt(key).status == "pending"
    assert store.guidance.receipt(task.body_dedupe_key).status == "pending"


def test_original_authorized_body_binding_cannot_be_swapped(queued_business_task):
    from agent_py_agent.agent.conversation.session_messaging import cancel_owned_session_task

    service, host, actor, task, view = queued_business_task
    store = host.conversation_store
    other_id = service.assign({"agent_id": view["agent_id"], "space_id": "space-A", "instruction": "unrelated task"},
        actor=actor, idempotency_key="r4-unrelated-task")["task_id"]
    other = store.session_tasks.require(other_id)
    edit_task(store, task.task_id, lambda current: replace(current, body_dedupe_key=other.body_dedupe_key,
        body_guidance_id=other.body_guidance_id))
    with pytest.raises(PermissionError, match="SESSION_TASK_SCOPE_MISMATCH"):
        cancel_owned_session_task(host, task)
    assert store.guidance.receipt(other.body_dedupe_key).status == "pending"
    assert store.guidance.receipt(task.body_dedupe_key).status == "pending"


def test_cancel_does_not_settle_unrelated_input(queued_business_task):
    service, host, actor, task, _ = queued_business_task
    store = host.conversation_store
    entry = store.guidance.append_once({"target_type": "thread", "target_id": task.target_thread_id,
        "message": "unrelated user input", "sender": actor.source_thread_id,
        "metadata": {"expected_turn_id": "different-turn"}}, dedupe_key="r4-unrelated-input")
    assert service.cancel_task(task.task_id, actor=actor)["state"] == "completed"
    assert store.guidance.receipt(task.body_dedupe_key).status == "rejected"
    assert store.guidance.receipt("r4-unrelated-input").status == "pending"
    assert store.guidance.receipt("r4-unrelated-input").entry.guidance_id == entry.guidance_id


def test_retirement_retry_restores_failed_original_message_wake(queued_business_task, monkeypatch):
    from agent_py_agent.agent.conversation.session_messaging import queue_owned_background_message

    service, host, actor, task, view = queued_business_task
    store = host.conversation_store
    service.cancel_task(task.task_id, actor=actor)
    suspended = service.transition(view["agent_id"], "suspend", actor=actor,
        expected_revision=view["revision"], idempotency_key="r4-retry-suspend")["agent"]
    target = store.threads.require(view["agent_id"])
    queue_owned_background_message(host, actor=actor, target=target, text="accepted before retirement",
        operation_key="r4-retry-original-message", input_kind="guidance")
    before = store.guidance.recent("thread", target.thread_id, limit=0)
    original, attempts = store.wakes.raise_signal, []

    def fail_one_publication(request, **options):
        assert request["reason"] == "session_message"
        attempts.append(request["dedupe_key"])
        if len(attempts) == 1:
            raise OSError("one original wake publication failed")
        return original(request, **options)

    monkeypatch.setattr(store.wakes, "raise_signal", fail_one_publication)
    options = {"actor": actor, "expected_revision": suspended["revision"], "idempotency_key": "r4-retry-retire"}
    with pytest.raises(OSError, match="one original wake publication failed"):
        service.transition(target.thread_id, "retire", **options)
    assert service.get(target.thread_id, actor=actor)["agent"]["identity_state"] == "retiring"
    for _ in range(3):
        reply = service.transition(target.thread_id, "retire", **options)
        assert reply["agent"]["identity_state"] == "retiring" and reply["outstanding_refs"]
        wakes = [wake for wake in store.wakes.pending(limit=0) if wake.reason == "session_message"]
        assert len(wakes) == 1
    assert len(set(attempts)) == 1
    assert store.guidance.recent("thread", target.thread_id, limit=0) == before
    pending, errors = store.guidance.pending_report("thread", target.thread_id, limit=0)
    assert not errors and len(pending) == 1


def test_retirement_drain_republishes_exact_accepted_task_without_reappending(queued_business_task):
    service, host, actor, task, view = queued_business_task
    store = host.conversation_store
    suspended = service.transition(view["agent_id"], "suspend", actor=actor,
        expected_revision=view["revision"], idempotency_key="r4-task-suspend")["agent"]
    before = store.guidance.recent("thread", task.target_thread_id, limit=0)
    # 丢失的原调度通知不等于正文已执行；保持同一canonical任务与pending正文，验证drain能恢复通知。
    for wake in store.wakes.pending(limit=0):
        store.wakes.mark_handled(wake.wake_signal_id)
    options = {"actor": actor, "expected_revision": suspended["revision"], "idempotency_key": "r4-task-drain"}
    identities = []
    for _ in range(3):
        reply = service.transition(task.target_thread_id, "retire", **options)
        assert reply["agent"]["identity_state"] == "retiring" and task.task_id in reply["outstanding_refs"]
        wakes = store.wakes.pending(limit=0)
        assert len(wakes) == 1
        wake = wakes[0]
        assert wake.reason == "session_task" and wake.thread_id == task.target_thread_id
        assert wake.metadata == {"origin_kind": "session_task", "origin_thread_id": task.sender_thread_id,
                                 "session_task_id": task.task_id}
        identities.append(wake.wake_signal_id)
    assert len(set(identities)) == 1
    assert store.session_tasks.require(task.task_id).body_guidance_id == task.body_guidance_id
    assert store.guidance.receipt(task.body_dedupe_key).status == "pending"
    assert store.guidance.recent("thread", task.target_thread_id, limit=0) == before
    with pytest.raises(ValueError, match="AGENT_NOT_AVAILABLE"):
        service.assign({"agent_id": task.target_thread_id, "space_id": "space-A", "instruction": "new work refused"},
            actor=actor, idempotency_key="r4-new-task-refused")


def test_owned_background_input_survives_as_host_history_after_ack(queued_business_task):
    from agent_py_agent.agent.conversation.session_messaging import queue_owned_background_message

    service, host, actor, task, view = queued_business_task
    store = host.conversation_store
    target = store.threads.require(view["agent_id"])
    queue_owned_background_message(host, actor=actor, target=target, text="follow-up body",
                                   operation_key="history-once", input_kind="message")
    entry = next(row for row in store.guidance.recent("thread", target.thread_id, limit=100)
                 if row.message == "follow-up body")
    assert not [row for row in store.messages.history_page_report(target.thread_id, limit=100).rows
                if row.content == "follow-up body"]
    assert store.guidance.claim_for_turn(entry, expected_turn_id="history-turn", attempt_id="history-attempt")
    entry = store.guidance.receipt(entry.metadata["dedupe_key"]).entry
    store.guidance.submissions.mark_submitted("history-turn", [entry], attempt_id="history-attempt",
                                            provider_call_id="history-call")
    for _ in range(2):
        store.guidance.acknowledgements.consume_submitted("history-turn", [entry], provider_call_id="history-call")
    rows = [row for row in store.messages.history_page_report(target.thread_id, limit=100).rows
            if row.content == "follow-up body"]
    assert len(rows) == 1
    assert rows[0].role == "host"
    assert rows[0].metadata["origin_kind"] == "session_message"
    assert rows[0].metadata["origin_thread_id"] == actor.source_thread_id
    assert rows[0].metadata["input_kind"] == "message"
    assert rows[0].metadata["kind"] != "active_turn_user_input"
    from agent_py_agent.agent.conversation.business_access import BusinessActor
    from agent_py_agent.agent.conversation.business_observability import BusinessObservationService
    observation = BusinessObservationService(store.storage.root.parent, owner_id=actor.owner_id,
                                             store=store, cursor_key=b"h" * 32)
    reader = BusinessActor(actor.owner_id, "history-reader", observation, lambda *_: True)
    result = observation.query("messages.list", {"target": {"kind": "background_main", "id": target.thread_id}},
                               actor=reader)
    visible = [row for row in result["items"] if row["text"] == "follow-up body"]
    assert len(visible) == 1 and visible[0]["role"] == "host"
    assert visible[0]['execution_refs'] == [] and visible[0]['input_ref'] is None
    assert visible[0]['association_coverage'] == 'unknown'
    assert visible[0]['association_reason'] == 'RUNTIME_EXECUTION_SOURCE_UNAVAILABLE:NOT_BOUND'
