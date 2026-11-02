# LLM: 旧派单以原写法直写旧owner库（可选旧双写规范库）；交接只经调用方提交门、按原身份发布并以持久回执续作。
# 模块用途: 验证受管旧派单有界交接、双唤醒恢复、活动/不确定工作保留与冻结旧请求的来源变更。
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import BaseBackend, ModelResponse
from agent_py_agent.agent.conversation.models import new_id
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.store_wakes import WakeStore
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.managed_dispatch_recovery import (
    canonical_conversation_root,
    legacy_conversation_root,
    open_canonical_conversation_store,
    open_legacy_conversation_store,
    reconcile_managed_dispatches,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


@pytest.fixture
def home(tmp_path):
    root = (tmp_path / "host").resolve()
    LocalUserLifecycle(LocalUserStore(root)).create("alice", idempotency_key="managed_alice_0001")
    return LocalUserStore(root).owner_home("alice")


# LLM: 逐字复现旧派单：线程/消息/queued任务/同键唤醒写入旧owner库；two_wake时按旧双写同时写入规范库。
# 函数用途: 构造一条尚未开始的旧管理员派单，返回原身份。
def _legacy_dispatch(home, prompt, *, two_wake=False, metadata_overrides=None):
    request_id, task_id = new_id("request"), new_id("task")
    metadata = {"dispatched_by": "local/main", "target_owner_id": home.owner_id,
                "execution_owner_id": home.owner_id, "request_id": request_id, "task_id": task_id,
                **(metadata_overrides or {})}
    legacy = ConversationStore(legacy_conversation_root(home))
    thread = legacy.threads.create(canonical_user_id=home.owner_id, title="Assigned", owner_id=home.owner_id,
                                   owner_home=str(home.home_dir), workspace_root=str(home.workspace_dir),
                                   cwd=str(home.workspace_dir))
    stores = [legacy, *([open_canonical_conversation_store(home, initialize=True)] if two_wake else [])]
    wakes = []
    for store in stores:
        if store.threads.load(thread.thread_id) is None:
            store.threads.write(thread)
        store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": prompt,
                               "channel": "internal", "metadata": metadata})
        store.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id, "request_id": request_id,
                          "goal": prompt, "status": "queued"})
        wakes.append(store.wakes.raise_signal({"thread_id": thread.thread_id, "reason": "admin_dispatched_task",
                                               "root_task_id": task_id, "summary": prompt, "urgency": "normal",
                                               "metadata": metadata, "dedupe_key": task_id}))
    return SimpleNamespace(thread_id=thread.thread_id, task_id=task_id, request_id=request_id,
                           legacy_wake=wakes[0], canonical_wake=wakes[1] if two_wake else None)


# LLM: 记录每次提交门调用后照常执行；不放宽或伪造门内结果。
# 函数用途: 提供可观察的同步提交门。
def _commit(log):
    def commit(operation):
        log.append(operation)
        return operation()

    return commit


def _receipt(home, task_id):
    return json.loads((home.home_dir / "data" / "managed_dispatch_handoffs" / f"{task_id}.json").read_text())


class _RecordingBackend(BaseBackend):
    name = "recording"

    def __init__(self):
        self.prompts = []

    def probe_tool_capability(self):
        from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso

        return ProviderToolCapability(provider="recording", endpoint="local://recording", model="", stream=False,
                                      native_supported=True, evidence="test_backend_declares_native_tools",
                                      observed_at=_utc_now_iso())

    def generate(self, prompt, on_chunk=None, **kwargs):
        # 原生工具协议把会话消息放在结构化messages里，派单正文由此送达模型。
        self.prompts.append(repr(kwargs.get("messages")))
        return ModelResponse(text="managed work finished", backend=self.name)


def test_legacy_only_dispatch_hands_off_once_with_original_identity(home):
    dispatch = _legacy_dispatch(home, "Sort the inbox")
    log = []
    report = reconcile_managed_dispatches(home, commit_transition=_commit(log))
    assert report == {"handed_off": [dispatch.task_id], "retained": [], "load_errors": 0, "truncated": False}
    assert len(log) == 2
    canonical = open_canonical_conversation_store(home, initialize=False)
    assert canonical.storage.root == canonical_conversation_root(home)
    [wake] = canonical.wakes.pending()
    assert (wake.wake_signal_id, wake.thread_id, wake.root_task_id, wake.dedupe_key) == (
        dispatch.legacy_wake.wake_signal_id, dispatch.thread_id, dispatch.task_id, dispatch.task_id)
    assert wake.metadata["request_id"] == dispatch.request_id and wake.reason == "admin_dispatched_task"
    link = canonical.tasks.load(dispatch.task_id)
    assert (link.thread_id, link.goal, link.status) == (dispatch.thread_id, "Sort the inbox", "active")
    [message] = canonical.messages.recent(dispatch.thread_id)
    assert message.content == "Sort the inbox" and message.metadata["request_id"] == dispatch.request_id
    thread = canonical.threads.require(dispatch.thread_id)
    assert thread.owner_id == home.owner_id and thread.owner_home == str(home.home_dir)
    legacy = open_legacy_conversation_store(home)
    assert legacy.wakes.pending() == [] and legacy.tasks.load(dispatch.task_id).status == "queued"
    receipt = _receipt(home, dispatch.task_id)
    assert receipt["phase"] == "completed" and receipt["canonical_receipt"] == "pending"
    assert (receipt["request_id"], receipt["legacy_wake_signal_id"]) == (dispatch.request_id, dispatch.legacy_wake.wake_signal_id)

    again = reconcile_managed_dispatches(home, commit_transition=_commit(log))
    assert again["handed_off"] == [] and again["retained"] == [] and len(log) == 2
    assert len(canonical.wakes.pending()) == 1 and len(canonical.messages.recent(dispatch.thread_id)) == 1


@pytest.mark.parametrize("canonical_state", ["pending", "handled"])
def test_old_two_wake_dispatch_keeps_one_canonical_execution(home, canonical_state):
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.agent.gateway_parts.managed_owner_runtime import (
        prepare_managed_owner_runtime,
    )

    dispatch = _legacy_dispatch(home, "Review the plan", two_wake=True)
    canonical = open_canonical_conversation_store(home, initialize=False)
    assert dispatch.canonical_wake.wake_signal_id != dispatch.legacy_wake.wake_signal_id
    if canonical_state == "handled":
        canonical.wakes.mark_handled(dispatch.canonical_wake.wake_signal_id)
    report = reconcile_managed_dispatches(home, commit_transition=lambda operation: operation())
    assert report["handed_off"] == [dispatch.task_id] and report["retained"] == []
    assert canonical.wakes.pending_one(dispatch.legacy_wake.wake_signal_id) is None
    assert open_legacy_conversation_store(home).wakes.pending() == []
    assert len(canonical.messages.recent(dispatch.thread_id)) == 1
    if canonical_state == "handled":
        # 已结清的规范副本不重投，也不复活任务。
        assert canonical.wakes.pending() == []
        assert canonical.tasks.load(dispatch.task_id).status == "queued"
        return
    assert [wake.wake_signal_id for wake in canonical.wakes.pending()] == [dispatch.canonical_wake.wake_signal_id]
    assert canonical.tasks.load(dispatch.task_id).status == "active"
    agent = prepare_managed_owner_runtime(home.root, OwnerIdentity.local_user("alice"), host_options={})
    backend = agent.backend = _RecordingBackend()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=agent.conversation_store, channels=agent.delivery_service)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": agent.conversation_store})
    assert len(scheduler.tick_thread(dispatch.thread_id)) == 1
    assert scheduler.tick_thread(dispatch.thread_id) == []
    assert len(backend.prompts) == 1 and "Review the plan" in backend.prompts[0]
    assert agent.conversation_store.tasks.load(dispatch.task_id).status == "completed"
    assert agent.conversation_store.wakes.pending() == []


@pytest.mark.parametrize("state", ["running", "completed", "claimed", "attempted", "unreadable", "missing", "foreign"])
def test_active_or_uncertain_legacy_work_is_retained(home, state):
    overrides = {"target_owner_id": "local/bob"} if state == "foreign" else None
    dispatch = _legacy_dispatch(home, "Keep me", metadata_overrides=overrides)
    legacy = open_legacy_conversation_store(home)
    if state in {"running", "completed"}:
        legacy.tasks.update_status({"task_id": dispatch.task_id, "status": state})
    elif state == "claimed":
        assert legacy.claims.acquire({"thread_id": dispatch.thread_id, "reason": "legacy", "task_id": dispatch.task_id})
    elif state == "attempted":
        path = legacy.storage.wake_attempt_path(dispatch.legacy_wake.wake_signal_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    elif state == "unreadable":
        legacy.storage.task_path(dispatch.task_id).write_text("{not json")
    elif state == "missing":
        legacy.storage.task_path(dispatch.task_id).unlink()
    expected = {"running": "legacy_task_running", "completed": "legacy_task_completed",
                "claimed": "legacy_thread_claimed", "attempted": "legacy_wake_attempted",
                "unreadable": "legacy_task_unreadable", "missing": "legacy_task_missing",
                "foreign": "not_exact_dispatch"}[state]
    log = []
    report = reconcile_managed_dispatches(home, commit_transition=_commit(log))
    assert report["handed_off"] == [] and log == []
    assert report["retained"] == [{"task_id": dispatch.task_id, "wake_signal_id": dispatch.legacy_wake.wake_signal_id,
                                   "reason": expected}]
    assert [wake.wake_signal_id for wake in legacy.wakes.pending()] == [dispatch.legacy_wake.wake_signal_id]
    canonical = open_canonical_conversation_store(home, initialize=False)
    assert canonical.tasks.load(dispatch.task_id) is None and canonical.wakes.pending() == []
    assert not (home.home_dir / "data" / "managed_dispatch_handoffs" / f"{dispatch.task_id}.json").exists()


def test_commit_refusal_leaves_original_dispatch_untouched(home):
    dispatch = _legacy_dispatch(home, "Not now")

    def refuse(_operation):
        raise ValueError("background_original_coordinator_required")

    report = reconcile_managed_dispatches(home, commit_transition=refuse)
    assert report["retained"] == [{"task_id": dispatch.task_id, "wake_signal_id": dispatch.legacy_wake.wake_signal_id,
                                   "reason": "handoff_error:ValueError"}]
    assert not (home.home_dir / "data" / "managed_dispatch_handoffs" / f"{dispatch.task_id}.json").exists()
    assert open_canonical_conversation_store(home, initialize=False).wakes.pending() == []
    assert len(open_legacy_conversation_store(home).wakes.pending()) == 1


@pytest.mark.parametrize("stop_at", ["before_retire", "inside_retire"])
def test_interrupted_handoff_resumes_from_durable_receipt(home, monkeypatch, stop_at):
    dispatch = _legacy_dispatch(home, "Resume me")
    calls = []

    def flaky(operation):
        calls.append(operation)
        if stop_at == "before_retire" and len(calls) == 2:
            raise OSError("simulated commit window loss")
        return operation()

    original = WakeStore.mark_handled
    if stop_at == "inside_retire":
        def stop_once(self, wake_signal_id, **kwargs):
            monkeypatch.setattr(WakeStore, "mark_handled", original)
            raise OSError("simulated stop before legacy wake retirement")

        monkeypatch.setattr(WakeStore, "mark_handled", stop_once)
    first = reconcile_managed_dispatches(home, commit_transition=flaky)
    assert first["handed_off"] == [] and first["retained"][0]["reason"] == "handoff_error:OSError"
    assert _receipt(home, dispatch.task_id)["phase"] == ("prepared" if stop_at == "before_retire" else "published")
    canonical = open_canonical_conversation_store(home, initialize=False)
    assert [wake.wake_signal_id for wake in canonical.wakes.pending()] == [dispatch.legacy_wake.wake_signal_id]
    assert len(open_legacy_conversation_store(home).wakes.pending()) == 1

    second = reconcile_managed_dispatches(home, commit_transition=lambda operation: operation())
    assert second["handed_off"] == [dispatch.task_id] and second["retained"] == []
    assert _receipt(home, dispatch.task_id)["phase"] == "completed"
    assert [wake.wake_signal_id for wake in canonical.wakes.pending()] == [dispatch.legacy_wake.wake_signal_id]
    assert len(canonical.messages.recent(dispatch.thread_id)) == 1
    assert open_legacy_conversation_store(home).wakes.pending() == []


def test_conflicting_receipt_retains_work_without_publication(home):
    dispatch = _legacy_dispatch(home, "Conflict")
    path = home.home_dir / "data" / "managed_dispatch_handoffs" / f"{dispatch.task_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": "managed_dispatch_handoff.v1", "owner_id": home.owner_id,
                                "task_id": dispatch.task_id, "request_id": "request-other", "phase": "prepared"}))
    report = reconcile_managed_dispatches(home, commit_transition=lambda operation: operation())
    assert report["retained"][0]["reason"] == "handoff_receipt_conflict"
    assert open_canonical_conversation_store(home, initialize=False).wakes.pending() == []
    assert len(open_legacy_conversation_store(home).wakes.pending()) == 1


def test_reconcile_is_bounded_and_resumes_next_round(home):
    dispatches = [_legacy_dispatch(home, f"Item {index}") for index in range(3)]
    first = reconcile_managed_dispatches(home, commit_transition=lambda operation: operation(), limit=2)
    assert len(first["handed_off"]) == 2 and first["truncated"] is True
    second = reconcile_managed_dispatches(home, commit_transition=lambda operation: operation(), limit=2)
    assert len(second["handed_off"]) == 1 and second["truncated"] is False
    assert sorted(first["handed_off"] + second["handed_off"]) == sorted(item.task_id for item in dispatches)
    with pytest.raises(ValueError):
        reconcile_managed_dispatches(home, commit_transition=None)


def test_frozen_legacy_wake_request_sees_source_changed_after_handoff(home):
    from agent_py_agent.agent.gateway_parts.multiprocess_background_request import (
        BackgroundSourceChanged,
        freeze_source,
        read_source,
    )

    dispatch = _legacy_dispatch(home, "Frozen")
    legacy = open_legacy_conversation_store(home)
    owner = OwnerIdentity.local_user("alice")
    ref = freeze_source(home.root, owner, legacy.storage.wake_signal_path(dispatch.legacy_wake), "wake")
    assert ref["store_ref"] == "conversations" and ref["source_id"] == dispatch.legacy_wake.wake_signal_id
    read_source(home.root, ref)
    reconcile_managed_dispatches(home, commit_transition=lambda operation: operation())
    with pytest.raises(BackgroundSourceChanged):
        read_source(home.root, ref)
    # 冻结请求不被改投：规范库发布的是另一个来源位置上的同一原唤醒。
    canonical = open_canonical_conversation_store(home, initialize=False)
    moved = freeze_source(home.root, owner, canonical.storage.wake_signal_path(canonical.wakes.pending()[0]), "wake")
    assert moved["store_ref"] == canonical_conversation_root(home).relative_to(home.home_dir).as_posix()
    assert moved["source_id"] == ref["source_id"] and moved["store_ref"] != ref["store_ref"]


def test_coordinator_discovery_hands_off_then_scans_only_canonical_store(home, monkeypatch):
    from agent_py_agent.agent.gateway_parts import managed_owner_runtime
    from agent_py_agent.agent.gateway_parts.multiprocess_background_discovery import (
        CoordinatorBackgroundRequests,
    )

    dispatch = _legacy_dispatch(home, "Collect metrics")
    owner = OwnerIdentity.local_user("alice")
    commits = []
    stub = SimpleNamespace(_source_cursors={}, control=SimpleNamespace(home_root=home.root, config=AgentConfig(
        memory_curator_enabled=False, enable_self_learning=False, owner_maintenance_scan_interval_seconds=0)))
    stub.commit = lambda target, operation: commits.append(target) or operation()
    stub._managed_canonical_root = lambda target: CoordinatorBackgroundRequests._managed_canonical_root(stub, target)
    refs = list(CoordinatorBackgroundRequests._owner_sources(stub, SimpleNamespace(identity=owner, home_dir=home.home_dir)))
    assert commits == [owner, owner]
    canonical = canonical_conversation_root(home).relative_to(home.home_dir).as_posix()
    wakes = [ref for ref in refs if ref["kind"] == "wake"]
    assert [(ref["source_id"], ref["store_ref"]) for ref in wakes] == [(dispatch.legacy_wake.wake_signal_id, canonical)]
    assert all(ref["store_ref"] in {"", canonical} for ref in refs)
    assert CoordinatorBackgroundRequests._managed_canonical_root(stub, OwnerIdentity.local_main()) is None
    assert commits == [owner, owner]

    def unreadable(*_args):
        raise OSError("account store unreadable")

    monkeypatch.setattr(managed_owner_runtime, "managed_local_user_id", unreadable)
    assert list(CoordinatorBackgroundRequests._owner_sources(stub, SimpleNamespace(identity=owner, home_dir=home.home_dir))) == []
