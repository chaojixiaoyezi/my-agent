"""R2：真实规范任务、列表投影、guidance 和 wake 的跨页生命周期回归。"""
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.background_team_lifecycle import (
    reconcile_background_identity,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft, SessionTaskUpdate
from agent_py_agent.tests.test_business_v12_f3_agents import (
    _service,
)
from agent_py_agent.tests.test_business_v12_f3_agents import (
    business_host as business_host,
)


def worker(business_host):
    service, host, actor = _service(business_host)
    view = service.create({"space_id": "space-A", "name": "worker"}, actor=actor, idempotency_key="worker")["agent"]
    return service, host, actor, view


def terminal_history(store, sender, target, count):
    for _ in range(count):
        task = store.session_tasks.create(SessionTaskDraft(sender, target, "retained history"))
        store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="accepted"))
        store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="done"))


def ready_index(store):
    # 只让原有有界回填追平；不替换分页、索引或任务状态。
    for _ in range(20):
        page = store.session_tasks.list_page(limit=100)
        if page["coverage"] == "complete" and not page["load_errors"]:
            return
    pytest.fail("canonical index did not reach complete coverage")


def retire(service, actor, view, **options):
    return service.transition(view["agent_id"], "retire", actor=actor,
        expected_revision=view["revision"], idempotency_key="retire", **options)


def chain(service, actor):
    root = service.submit({"space_id": "space-A", "instruction": "root"}, actor=actor, idempotency_key="root")["task_id"]
    child = service.follow_up({"task_id": root, "instruction": "child"}, actor=actor, idempotency_key="child")["new_task_id"]
    leaf = service.follow_up({"task_id": child, "instruction": "leaf"}, actor=actor, idempotency_key="leaf")["new_task_id"]
    return root, child, leaf


@pytest.mark.parametrize("count", [100, 101, 205])
def test_terminal_history_retires_and_restores_same_identity(business_host, count):
    service, host, actor, view = worker(business_host)
    terminal_history(host.conversation_store, actor.source_thread_id, view["agent_id"], count)
    ready_index(host.conversation_store)
    result = retire(service, actor, view)
    assert result["agent"]["identity_state"] == "retired"
    assert result["outstanding_refs"] == []
    restored = service.transition(view["agent_id"], "restore", actor=actor,
        expected_revision=result["agent"]["revision"], idempotency_key="restore")
    assert restored["agent"]["identity_state"] == "available"
    assert restored["agent"]["agent_id"] == view["agent_id"]


# LLM: 原三层提交、guidance及wake均用规范存储；核验真实运行时取消门，不把返回completed当作后代已撤销。
# 函数用途: 验证完整后代取消和单任务取消的实际可运行范围，以及重复调用幂等。
@pytest.mark.parametrize("descendants", [False, True])
def test_three_level_cancellation_uses_complete_original_chain(business_host, descendants):
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _session_task_turn_was_cancelled,
    )

    service, host, actor = _service(business_host)
    ids = chain(service, actor)
    store = host.conversation_store
    ready_index(store)
    result = service.cancel_task(ids[0], actor=actor, include_descendants=descendants)
    expected = ["cancelled"] * 3 if descendants else ["cancelled", "queued", "queued"]
    assert [store.session_tasks.require(task_id).status for task_id in ids] == expected
    assert result == {"operation_id": ids[0], "state": "completed", "outstanding_refs": []}
    for task_id, status in zip(ids, expected):
        task = store.session_tasks.require(task_id)
        assert store.guidance.receipt(task.body_dedupe_key).status == ("rejected" if status == "cancelled" else "pending")
        wake = next(item for item in store.wakes.pending() if item.metadata.get("session_task_id") == task_id)
        request = BackgroundRunRequest(thread_id=task.target_thread_id, task_id=task.conversation_request_id,
            reason=wake.reason, wake_signal=wake.to_dict())
        assert _session_task_turn_was_cancelled(host, request) is (status == "cancelled")
    assert service.cancel_task(ids[0], actor=actor, include_descendants=descendants) == result


def edit_task(store, task_id, transform):
    from agent_py_agent.agent.conversation.session_tasks import SessionTask
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic

    update_json_file_atomic(store.session_tasks._path(task_id),
        lambda raw: transform(SessionTask.from_dict(raw)).to_dict(), require_existing=True)
    store.session_tasks._list_index().publish(store.session_tasks._path(task_id))


def settle(store, task_id, status="done"):
    store.session_tasks.advance(task_id, SessionTaskUpdate(status="accepted"))
    return store.session_tasks.advance(task_id, SessionTaskUpdate(status=status,
        result_refs=("original-result",), summary="original-summary"))


def eventually_retired(host, identifier):
    for _ in range(10):
        thread = reconcile_background_identity(host, identifier)
        if thread.metadata["background_agent"]["identity_state"] == "retired":
            return thread
    pytest.fail("fresh complete traversal did not converge")


@pytest.mark.parametrize("obligation", ["unfinished", "cancellation", "learning"])
def test_later_page_obligation_blocks_then_resolves(business_host, obligation):
    service, host, actor, view = worker(business_host)
    store = host.conversation_store
    old = store.session_tasks.create(SessionTaskDraft(actor.source_thread_id, view["agent_id"], "old obligation"))
    if obligation != "unfinished":
        settle(store, old.task_id)
        metadata = ({"cancellation_control": {"outstanding_refs": ["exact-stop"]}}
            if obligation == "cancellation" else {"task_learning_enqueue": {"status": "pending"},
                "trusted_task_completion": {"completion_ref": "exact-completion"}})
        edit_task(store, old.task_id, lambda task: replace(task, metadata=metadata))
    terminal_history(store, actor.source_thread_id, view["agent_id"], 101)
    ready_index(store)
    result = retire(service, actor, view)
    assert result["agent"]["identity_state"] == "retiring"
    expected = {"unfinished": old.task_id, "cancellation": "exact-stop", "learning": "exact-completion"}[obligation]
    assert expected in result["outstanding_refs"]
    if obligation == "unfinished":
        store.session_tasks.advance(old.task_id, SessionTaskUpdate(status="cancelled"))
    else:
        edit_task(store, old.task_id, lambda task: replace(task, metadata={}))
    eventually_retired(host, view["agent_id"])


def test_available_reuse_never_scans_history(business_host, monkeypatch):
    _, host, _, view = worker(business_host)
    def forbidden(**kwargs):
        pytest.fail("ordinary reusable completion performed a history scan")
    monkeypatch.setattr(host.conversation_store.session_tasks, "list_page", forbidden)
    for _ in range(3):
        result = reconcile_background_identity(host, view["agent_id"])
        assert result.metadata["background_agent"]["identity_state"] == "available"
        assert result.metadata["background_agent"]["identity_revision"] == view["revision"]


def test_real_cursor_expiry_does_not_merge_snapshots(business_host, monkeypatch):
    from agent_py_agent.agent.conversation.background_team_lifecycle import identity_outstanding
    _, host, actor, view = worker(business_host)
    store = host.conversation_store
    terminal_history(store, actor.source_thread_id, view["agent_id"], 101)
    ready_index(store)
    original = store.session_tasks.list_page
    seen = []
    injected = []
    def concurrent_write(**kwargs):
        page = original(**kwargs)
        seen.append((kwargs, page["coverage"]))
        if page["next_cursor"] and not injected:
            injected.append(store.session_tasks.create(SessionTaskDraft(actor.source_thread_id, view["agent_id"], "new obligation")))
        return page
    monkeypatch.setattr(store.session_tasks, "list_page", concurrent_write)
    refs, _ = identity_outstanding(host, store.threads.require(view["agent_id"]))
    assert "session_tasks:coverage_unknown" in refs
    assert any(options.get("cursor") and coverage == "expired" for options, coverage in seen)
    assert all(options["limit"] <= 100 for options, _ in seen)
    refs, _ = identity_outstanding(host, store.threads.require(view["agent_id"]))
    assert injected[0].task_id in refs
    store.session_tasks.advance(injected[0].task_id, SessionTaskUpdate(status="cancelled"))
    ready_index(store)
    assert identity_outstanding(host, store.threads.require(view["agent_id"])) == ([], [])


def test_partial_and_unreadable_history_remain_unresolved(business_host):
    from agent_py_agent.agent.conversation.background_team_lifecycle import identity_outstanding
    service, host, actor, view = worker(business_host)
    store = host.conversation_store
    terminal_history(store, actor.source_thread_id, view["agent_id"], 205)
    refs, _ = identity_outstanding(host, store.threads.require(view["agent_id"]))
    assert "session_tasks:coverage_unknown" in refs
    ready_index(store)
    broken = store.session_tasks._dir() / "broken.json"
    broken.write_text("not-json")
    ready_index_with_error = store.session_tasks.list_page(limit=100)
    assert ready_index_with_error["coverage"] in {"partial", "complete"}
    result = retire(service, actor, view)
    assert result["agent"]["identity_state"] == "retiring"
    assert "session_tasks:coverage_unknown" in result["outstanding_refs"]


@pytest.mark.parametrize("terminal", ["done", "failed", "cancelled"])
def test_terminal_root_and_intermediate_preserve_results_but_visit_leaf(business_host, terminal):
    service, host, actor = _service(business_host)
    root, child, leaf = chain(service, actor)
    store = host.conversation_store
    if terminal == "cancelled":
        store.session_tasks.advance(root, SessionTaskUpdate(status="cancelled"))
    else:
        settle(store, root, terminal)
    settle(store, child)
    before = {task_id: store.session_tasks.require(task_id).to_dict() for task_id in (root, child)}
    ready_index(store)
    result = service.cancel_task(root, actor=actor, include_descendants=True)
    assert result["state"] == "completed" and result["operation_id"] == root
    assert store.session_tasks.require(leaf).status == "cancelled"
    for task_id in (root, child):
        after = store.session_tasks.require(task_id).to_dict()
        after["metadata"].pop("cancellation_control", None)
        assert after == before[task_id]
    assert service.cancel_task(root, actor=actor, include_descendants=True) == result


def test_descendant_after_page_one_and_unrelated_history(business_host):
    service, host, actor = _service(business_host)
    ids = chain(service, actor)
    store = host.conversation_store
    target = store.session_tasks.require(ids[0]).target_thread_id
    terminal_history(store, actor.source_thread_id, target, 105)
    unrelated = service.submit({"space_id": "space-A", "instruction": "unrelated"}, actor=actor, idempotency_key="unrelated")["task_id"]
    ready_index(store)
    original = store.session_tasks.require(unrelated).to_dict()
    result = service.cancel_task(ids[0], actor=actor, include_descendants=True)
    assert result["state"] == "completed"
    assert [store.session_tasks.require(task_id).status for task_id in ids] == ["cancelled"] * 3
    assert store.session_tasks.require(unrelated).to_dict() == original


def test_running_descendant_retains_exact_stop_until_retry_confirms(business_host, monkeypatch):
    from agent_py_agent.agent.gateway_parts import session_task_stop
    service, host, actor = _service(business_host)
    ids = chain(service, actor)
    store = host.conversation_store
    leaf = store.session_tasks.require(ids[-1])
    receipt = store.guidance.receipt(leaf.body_dedupe_key)
    assert store.guidance.claim_for_turn(receipt.entry, expected_turn_id="exact-turn", attempt_id="attempt", owning_task_id=leaf.task_id)
    store.session_tasks.advance(leaf.task_id, SessionTaskUpdate(status="accepted", conversation_request_id="exact-turn"))
    confirmed, calls = [False], []
    def stop(host, *, target_thread, turn_id):
        calls.append((target_thread.thread_id, turn_id))
        return session_task_stop.SessionTaskStopOutcome(confirmed[0], "external stop observation", turn_id)
    monkeypatch.setattr(session_task_stop, "stop_session_task_turn", stop)
    ready_index(store)
    pending = service.cancel_task(ids[0], actor=actor, include_descendants=True)
    assert pending == {"operation_id": ids[0], "state": "pending", "outstanding_refs": ["exact-turn"]}
    assert store.session_tasks.require(ids[0]).metadata["cancellation_control"]["outstanding_refs"] == ["exact-turn"]
    confirmed[0] = True
    done = service.cancel_task(ids[0], actor=actor, include_descendants=True)
    assert done == {"operation_id": ids[0], "state": "completed", "outstanding_refs": []}
    assert calls == [(leaf.target_thread_id, "exact-turn")] * 2
    assert service.cancel_task(ids[0], actor=actor, include_descendants=True) == done
    assert len(calls) == 2


def test_partial_descendant_traversal_retains_control_and_converges(business_host):
    service, host, actor = _service(business_host)
    ids = chain(service, actor)
    store = host.conversation_store
    target = store.session_tasks.require(ids[0]).target_thread_id
    terminal_history(store, actor.source_thread_id, target, 300)
    first = service.cancel_task(ids[0], actor=actor, include_descendants=True)
    assert first["state"] == "pending" and "descendants:coverage_unknown" in first["outstanding_refs"]
    assert [store.session_tasks.require(task_id).status for task_id in ids] == ["queued"] * 3
    for _ in range(12):
        result = service.cancel_task(ids[0], actor=actor, include_descendants=True)
        if result["state"] == "completed":
            break
    assert result["state"] == "completed" and result["operation_id"] == ids[0]
    assert [store.session_tasks.require(task_id).status for task_id in ids] == ["cancelled"] * 3


def test_completed_running_root_is_not_stopped_again(business_host, monkeypatch):
    from agent_py_agent.agent.gateway_parts import session_task_stop
    service, host, actor = _service(business_host)
    root = service.submit({"space_id": "space-A", "instruction": "root"}, actor=actor, idempotency_key="root")["task_id"]
    store = host.conversation_store
    store.session_tasks.advance(root, SessionTaskUpdate(status="accepted", conversation_request_id="original-turn"))
    calls = []
    def stop(host, *, target_thread, turn_id):
        calls.append(turn_id)
        return session_task_stop.SessionTaskStopOutcome(True, "confirmed", turn_id)
    monkeypatch.setattr(session_task_stop, "stop_session_task_turn", stop)
    ready_index(store)
    result = service.cancel_task(root, actor=actor, include_descendants=True)
    assert result["state"] == "completed"
    assert service.cancel_task(root, actor=actor, include_descendants=True) == result
    assert calls == ["original-turn"]


@pytest.mark.parametrize("action", ["resume", "cancel_tasks"])
def test_lifecycle_action_reaches_queued_task_after_page_one(business_host, action):
    service, host, actor, view = worker(business_host)
    store = host.conversation_store
    queued = service.assign({"agent_id": view["agent_id"], "space_id": "space-A", "instruction": "old queued"},
        actor=actor, idempotency_key="assign")["task_id"]
    for wake in store.wakes.pending(limit=0):
        store.wakes.mark_handled(wake.wake_signal_id)
    terminal_history(store, actor.source_thread_id, view["agent_id"], 101)
    ready_index(store)
    if action == "resume":
        suspended = service.transition(view["agent_id"], "suspend", actor=actor,
            expected_revision=view["revision"], idempotency_key="suspend")
        service.transition(view["agent_id"], "resume", actor=actor,
            expected_revision=suspended["agent"]["revision"], idempotency_key="resume")
        assert any(w.metadata.get("session_task_id") == queued for w in store.wakes.pending(limit=0))
        assert store.session_tasks.require(queued).status == "queued"
    else:
        result = retire(service, actor, view, disposition="cancel_tasks")
        assert store.session_tasks.require(queued).status == "cancelled"
        assert store.guidance.receipt(store.session_tasks.require(queued).body_dedupe_key).status == "rejected"
        assert result["agent"]["identity_state"] == "retired"


def test_cancellation_expired_snapshot_changes_no_task_states_and_fresh_retry_converges(business_host, monkeypatch):
    service, host, actor = _service(business_host)
    ids = chain(service, actor)
    store = host.conversation_store
    terminal_history(store, actor.source_thread_id, store.session_tasks.require(ids[0]).target_thread_id, 101)
    ready_index(store)
    original, observations, changed = store.session_tasks.list_page, [], []
    def concurrent_change(**kwargs):
        page = original(**kwargs)
        observations.append(page["coverage"])
        if page["next_cursor"] and not changed:
            changed.append(True)
            store.session_tasks.advance(page["items"][0].task_id, SessionTaskUpdate(status="done"))
        return page
    monkeypatch.setattr(store.session_tasks, "list_page", concurrent_change)
    result = service.cancel_task(ids[0], actor=actor, include_descendants=True)
    assert result["state"] == "pending" and "descendants:coverage_unknown" in result["outstanding_refs"]
    assert "expired" in observations
    assert [store.session_tasks.require(identifier).status for identifier in ids] == ["queued"] * 3
    for _ in range(10):
        result = service.cancel_task(ids[0], actor=actor, include_descendants=True)
        if result["state"] == "completed":
            break
    assert result["state"] == "completed"
    assert [store.session_tasks.require(identifier).status for identifier in ids] == ["cancelled"] * 3


@pytest.mark.parametrize("conflict", ["owner", "space"])
def test_every_descendant_is_scope_checked_before_any_cancellation(business_host, conflict):
    service, host, actor = _service(business_host)
    root, child, leaf = chain(service, actor)
    store = host.conversation_store
    if conflict == "owner":
        foreign = store.threads.create(owner_id="foreign", canonical_user_id="foreign")
        edit_task(store, leaf, lambda task: replace(task, target_thread_id=foreign.thread_id))
    else:
        edit_task(store, leaf, lambda task: replace(task, metadata={**task.metadata,
            "business_context": {**task.metadata["business_context"], "space_id": "space-B"}}))
    ready_index(store)
    with pytest.raises(PermissionError, match="OWNER_MISMATCH|SPACE_MISMATCH"):
        service.cancel_task(root, actor=actor, include_descendants=True)
    assert [store.session_tasks.require(identifier).status for identifier in (root, child, leaf)] == ["queued"] * 3


def test_cycle_protection_visits_each_task_once(business_host):
    service, host, actor = _service(business_host)
    root, child, leaf = chain(service, actor)
    store = host.conversation_store
    edit_task(store, root, lambda task: replace(task, origin_task_id=leaf))
    ready_index(store)
    result = service.cancel_task(root, actor=actor, include_descendants=True)
    assert result["state"] == "completed"
    assert [store.session_tasks.require(identifier).status for identifier in (root, child, leaf)] == ["cancelled"] * 3


def test_reference_overflow_is_explicit_and_all_stops_can_later_complete(business_host, monkeypatch):
    from agent_py_agent.agent.conversation.business_contracts import BusinessContextIntent
    from agent_py_agent.agent.gateway_parts import session_task_stop
    service, host, actor = _service(business_host)
    root = service.submit({"space_id": "space-A", "instruction": "root"}, actor=actor, idempotency_key="root")["task_id"]
    store = host.conversation_store
    task, context = service._owned_task(root, actor)
    children = []
    for i in range(101):
        child = store.session_tasks.create(SessionTaskDraft(task.sender_thread_id, task.target_thread_id,
            "running child", origin_task_id=root), business_context_intent=BusinessContextIntent(actor.owner_id, "space-A", None, None),
            business_context_factory=lambda identifier: replace(context, task_id=identifier))
        store.session_tasks.advance(child.task_id, SessionTaskUpdate(status="accepted", conversation_request_id=f"turn-{i}"))
        children.append(child.task_id)
    confirmed, calls = [False], []
    def stop(host, *, target_thread, turn_id):
        calls.append(turn_id)
        return session_task_stop.SessionTaskStopOutcome(confirmed[0], "external observation", turn_id)
    monkeypatch.setattr(session_task_stop, "stop_session_task_turn", stop)
    ready_index(store)
    pending = service.cancel_task(root, actor=actor, include_descendants=True)
    assert pending["state"] == "pending" and len(pending["outstanding_refs"]) <= 100
    assert "descendants:unresolved_scope" in pending["outstanding_refs"]
    assert all(store.session_tasks.require(identifier).metadata["cancellation_control"]["outstanding_refs"] for identifier in children)
    confirmed[0] = True
    for _ in range(10):
        result = service.cancel_task(root, actor=actor, include_descendants=True)
        if result["state"] == "completed":
            break
    assert result == {"operation_id": root, "state": "completed", "outstanding_refs": []}
    assert set(calls) == {f"turn-{i}" for i in range(101)} and len(calls) == 202


def test_existing_descendant_execution_cancellation_is_not_erased(business_host, tmp_path):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    service, host, actor = _service(business_host)
    root, _, leaf = chain(service, actor)
    store = host.conversation_store
    task, context = service._owned_task(leaf, actor)
    repo = RuntimeRepository(tmp_path / "execution.db")
    run = repo.record_run_creation(owner_id=actor.owner_id, conversation_task_id="runtime-root",
        run_id="still-unconfirmed", thread_id=task.target_thread_id, attempt_status="running",
        task_run_metadata={"business_context": context.to_dict(), "session_task_ref": leaf})
    store.session_tasks.bind_execution(leaf, task_run_id=run["task_run_id"], target_thread_id=task.target_thread_id)
    ref = repo.parent_execution_ref(owner_id=actor.owner_id, run_id="still-unconfirmed", attempt_id=run["attempt_id"])
    store.session_tasks.record_execution_cancellation_control(leaf, execution_ref=ref, outstanding_refs=["still-unconfirmed"])
    ready_index(store)
    result = service.cancel_task(root, actor=actor, include_descendants=True)
    assert result["state"] == "pending" and "still-unconfirmed" in result["outstanding_refs"]
    current = store.session_tasks.require(leaf)
    assert current.metadata["cancellation_control"]["outstanding_refs"] == ["still-unconfirmed"]
    assert current.metadata["cancellation_control"]["execution_ref"] == ref.to_dict()
    # 原执行停止路径确认后用原控制写口解除责任；根重试须收口同一操作，不能保留无法消除的聚合残影。
    store.session_tasks.record_cancellation_control(leaf, outstanding_refs=[])
    for _ in range(10):
        result = service.cancel_task(root, actor=actor, include_descendants=True)
        if result["state"] == "completed":
            break
    assert result == {"operation_id": root, "state": "completed", "outstanding_refs": []}


def test_terminal_root_atomic_updater_revalidates_business_binding(business_host, monkeypatch):
    from agent_py_agent.agent.gateway_parts import io
    service, host, actor = _service(business_host)
    root, _, _ = chain(service, actor)
    store = host.conversation_store
    settle(store, root)
    ready_index(store)
    original, changed = io.update_json_file_atomic, []
    def change_binding(path, updater, **options):
        if path == store.session_tasks._path(root) and not changed:
            changed.append(True)
            original(path, lambda raw: {**raw, "metadata": {**raw["metadata"], "business_context": {
                **raw["metadata"]["business_context"], "space_id": "space-B"}}}, require_existing=True)
        return original(path, updater, **options)
    monkeypatch.setattr(io, "update_json_file_atomic", change_binding)
    with pytest.raises(PermissionError, match="SESSION_TASK_SCOPE_MISMATCH"):
        service.cancel_task(root, actor=actor, include_descendants=True)
    current = store.session_tasks.require(root)
    assert current.status == "done" and current.result_refs == ("original-result",)
    assert current.metadata["business_context"]["space_id"] == "space-B"


@pytest.mark.parametrize("terminal", ["done", "failed"])
def test_terminal_ancestor_keeps_its_unconfirmed_turn_until_stop_confirmation(business_host, monkeypatch, terminal):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import session_task_stop

    service, host, actor = _service(business_host)
    root, _, _ = chain(service, actor)
    store = host.conversation_store
    store.session_tasks.advance(root, SessionTaskUpdate(status="accepted", conversation_request_id="original-turn"))
    store.session_tasks.advance(root, SessionTaskUpdate(status=terminal, result_refs=("original-result",)))
    edit_task(store, root, lambda task: replace(task, metadata={**task.metadata,
        "cancellation_control": {"conversation_request_id": "original-turn", "outstanding_refs": ["original-turn"]}}))
    ready_index(store)
    confirmed, stopped = [False], []
    def stop(host, *, target_thread, turn_id):
        stopped.append(turn_id)
        return SimpleNamespace(confirmed=confirmed[0])
    monkeypatch.setattr(session_task_stop, "stop_session_task_turn", stop)
    result = service.cancel_task(root, actor=actor, include_descendants=True)
    assert result == {"operation_id": root, "state": "pending", "outstanding_refs": ["original-turn"]}
    current = store.session_tasks.require(root)
    assert current.status == terminal and current.result_refs == ("original-result",)
    confirmed[0] = True
    assert service.cancel_task(root, actor=actor, include_descendants=True) == {
        "operation_id": root, "state": "completed", "outstanding_refs": []}
    assert stopped == ["original-turn", "original-turn"]
