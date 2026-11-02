"""R9 P1-R9-03：取消在正文入队前或绑定写入前提交时，绑定不得复活任务、不得再唤醒模型。

真实业务入口、原 SessionTask/guidance/wake 存储、原取消与调度器；只把外部模型换成计数后端。
并发取消在独立线程完成后再放行派发线程，模拟它恰好在该点提交。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from agent_py_agent.agent.conversation import session_tasks as session_tasks_module
from agent_py_agent.agent.conversation.session_tasks import (
    SessionTaskDraft,
    SessionTaskStore,
    SessionTaskUpdate,
)
from agent_py_agent.tests.test_mega_background_lifecycle_controls import (
    eventually_retired,
    ready_index,
)


@dataclass
class _Case:
    agent: object
    backend: object
    scheduler: object
    call: object
    space_id: str
    view: dict


def _native_business(tmp_path) -> _Case:
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.agent.conversation.host_actor import HostChannelActor
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.business_api import (
        BusinessServices,
        bind_business_context,
    )
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "native-home"),
        business_spaces_enabled=True, enable_tools=True, prompt_files=[]), tmp_path / "workspace")
    store, owner = agent.conversation_store, str(agent.home_paths.owner_id)
    source = store.threads.create(owner_id=owner, canonical_user_id="local-agent")
    actor = HostChannelActor(owner, source.thread_id, "binding-cancel", "local", "local", "local-agent")
    context = bind_business_context(agent, authorize=lambda action, facts: True, actor=actor)
    services = BusinessServices(agent)
    space = services.spaces.create({"name": "绑定取消空间"}, actor=services.actor(context),
                                   idempotency_key="binding-cancel-space")

    def call(action, params, **options):
        return services.background.execute(action, params, context=context, actor=actor, **options)

    view = call("background_agents.create", {"space_id": space["space_id"], "name": "原执行者"},
                idempotency_key="binding-cancel-worker")["agent"]

    class CountingBackend:
        name = "binding-cancel-counting-model"

        def __init__(self):
            self.calls = 0

        def probe_tool_capability(self):
            return ProviderToolCapability(provider=self.name, endpoint="local://test", model="", stream=False,
                native_supported=True, evidence="contract_test", observed_at=_utc_now_iso())

        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            return ModelResponse(text="original worker reply", backend=self.name)

    agent.backend = backend = CountingBackend()
    scheduler = BackgroundMainAgentScheduler({"runtime": BackgroundMainAgentRuntime(agent=agent, store=store), "store": store})
    return _Case(agent, backend, scheduler, call, space["space_id"], view)


def _assign(case, key):
    return case.call("tasks.assign", {"space_id": case.space_id, "agent_id": case.view["agent_id"],
                                      "instruction": "读取原资料后汇报"}, idempotency_key=key)["task_id"]


def _cancel_concurrently(case, task_id):
    # 另一个调用方线程完成整次原取消后才放行派发线程：取消恰好在此刻提交。
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(case.call, "tasks.cancel", {"task_id": task_id, "include_descendants": True}).result(timeout=30)


def _thread_wakes(store, thread_id):
    return [wake for wake in store.wakes.pending(limit=0) if wake.thread_id == thread_id]


def _tick(case, rounds=3):
    for _ in range(rounds):
        case.scheduler.tick()


def _retire_and_drain(case):
    store = case.agent.conversation_store
    ready_index(store)
    retiring = case.call("background_agents.retire", {"agent_id": case.view["agent_id"], "disposition": "drain"},
                         expected_revision=case.view["revision"], idempotency_key="binding-cancel-retire")
    assert retiring["agent"]["identity_state"] in {"retiring", "retired"}
    return eventually_retired(case.agent, case.view["agent_id"])


def test_uncancelled_dispatch_still_wakes_and_reaches_model(tmp_path):
    # 对照：同一真实链路未取消时照常唤醒并执行，下面"零调用"断言不是空转。
    case = _native_business(tmp_path)
    store = case.agent.conversation_store
    task_id = _assign(case, "control-dispatch")
    task = store.session_tasks.require(task_id)
    assert task.status == "queued" and task.body_guidance_id
    assert _thread_wakes(store, task.target_thread_id)
    _tick(case, rounds=1)
    assert case.backend.calls >= 1


def test_cancel_before_body_enqueue_publishes_no_wake_and_settles_late_body(tmp_path, monkeypatch):
    case = _native_business(tmp_path)
    store = case.agent.conversation_store
    append_once, fired = store.guidance.append_once, []

    def cancel_then_append(request, **kwargs):
        metadata = request.get("metadata") or {}
        if metadata.get("origin_kind") == "session_task" and not fired:
            fired.append(_cancel_concurrently(case, metadata["session_task_id"]))
        return append_once(request, **kwargs)

    monkeypatch.setattr(store.guidance, "append_once", cancel_then_append)
    task_id = _assign(case, "cancel-before-enqueue")
    # 取消提交时尚无正文：原取消按"从未发表"完成；派发随后入队的迟到正文不能被遗留或唤醒。
    assert fired == [{"operation_id": task_id, "state": "completed", "outstanding_refs": []}]
    task = store.session_tasks.require(task_id)
    assert task.status == "cancelled" and task.body_guidance_id
    assert task.metadata["cancellation_control"]["outstanding_refs"] == []
    receipt = store.guidance.receipt(task.body_dedupe_key)
    assert receipt.status == "rejected" and receipt.entry.guidance_id == task.body_guidance_id
    assert not _thread_wakes(store, task.target_thread_id)
    assert not store.guidance.pending_report("thread", task.target_thread_id, limit=0)[0]
    _tick(case)
    assert case.backend.calls == 0
    assert store.session_tasks.require(task_id).status == "cancelled"
    assert _retire_and_drain(case).metadata["background_agent"]["identity_state"] == "retired"
    assert case.backend.calls == 0


def test_cancel_committed_during_binding_stays_cancelled_and_never_reaches_model(tmp_path, monkeypatch):
    case = _native_business(tmp_path)
    store = case.agent.conversation_store
    dispatcher, state = threading.get_ident(), {"binding": "", "fired": []}
    bind_body = store.session_tasks.bind_body

    def binding(task_id, body_guidance_id):
        state["binding"] = task_id
        try:
            return bind_body(task_id, body_guidance_id)
        finally:
            state["binding"] = ""

    def cancel_before_bind_write(original):
        # 绑定已开始、其写入尚未发生时提交原取消；旧实现在锁外已读过queued快照。
        def wrapper(path, *args, **kwargs):
            task_id = state["binding"]
            if (task_id and not state["fired"] and threading.get_ident() == dispatcher
                    and path == store.session_tasks._path(task_id)):
                state["fired"].append(_cancel_concurrently(case, task_id))
            return original(path, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(store.session_tasks, "bind_body", binding)
    for name in ("update_json_file_atomic", "write_json_file_atomic"):
        monkeypatch.setattr(session_tasks_module, name, cancel_before_bind_write(getattr(session_tasks_module, name)))
    task_id = _assign(case, "cancel-during-binding")
    # 正文已入队但尚未绑定：原取消不能确认这条正文，保留本任务责任。
    assert state["fired"] == [{"operation_id": task_id, "state": "pending", "outstanding_refs": [task_id]}]
    task = store.session_tasks.require(task_id)
    assert task.status == "cancelled", "binding resurrected a committed cancellation"
    control = task.metadata["cancellation_control"]
    assert control["outstanding_refs"] == [task_id]
    assert task.body_guidance_id
    receipt = store.guidance.receipt(task.body_dedupe_key)
    assert receipt.status == "rejected" and receipt.entry.guidance_id == task.body_guidance_id
    assert not _thread_wakes(store, task.target_thread_id)
    _tick(case)
    assert case.backend.calls == 0
    # 原取消重试收掉已结清正文的同一责任，不复制第二份。
    assert case.call("tasks.cancel", {"task_id": task_id, "include_descendants": True}) == {
        "operation_id": task_id, "state": "completed", "outstanding_refs": []}
    assert store.session_tasks.require(task_id).status == "cancelled"
    assert _retire_and_drain(case).metadata["background_agent"]["identity_state"] == "retired"
    assert case.backend.calls == 0


def test_unsettled_late_body_keeps_original_cancellation_responsibility(tmp_path, monkeypatch):
    case = _native_business(tmp_path)
    store = case.agent.conversation_store
    append_once, mark_status = store.guidance.append_once, store.guidance.mark_status
    fired, failures = [], []

    def cancel_then_append(request, **kwargs):
        metadata = request.get("metadata") or {}
        if metadata.get("origin_kind") == "session_task" and not fired:
            fired.append(_cancel_concurrently(case, metadata["session_task_id"]))
            failures.append("armed")
        return append_once(request, **kwargs)

    def failing_mark_status(key, status):
        if failures == ["armed"]:
            failures.append("raised")
            raise OSError("simulated receipt write error")
        return mark_status(key, status)

    monkeypatch.setattr(store.guidance, "append_once", cancel_then_append)
    monkeypatch.setattr(store.guidance, "mark_status", failing_mark_status)
    task_id = _assign(case, "late-body-unsettled")
    assert failures == ["armed", "raised"]
    task = store.session_tasks.require(task_id)
    assert task.status == "cancelled"
    assert task.metadata["cancellation_control"]["outstanding_refs"] == [task_id]
    assert store.guidance.receipt(task.body_dedupe_key).status == "pending"
    assert not _thread_wakes(store, task.target_thread_id)
    _tick(case)
    assert case.backend.calls == 0
    assert case.call("tasks.cancel", {"task_id": task_id, "include_descendants": True}) == {
        "operation_id": task_id, "state": "completed", "outstanding_refs": []}
    assert store.guidance.receipt(task.body_dedupe_key).status == "rejected"
    assert _retire_and_drain(case).metadata["background_agent"]["identity_state"] == "retired"
    assert case.backend.calls == 0


def test_bind_body_only_fills_empty_reference_and_preserves_latest_record(tmp_path):
    from agent_py_agent.agent.conversation import ConversationStore

    store = ConversationStore(tmp_path / "conversations")
    sender = store.threads.create(owner_id="owner", canonical_user_id="u")
    target = store.threads.create(owner_id="owner", canonical_user_id="u")
    tasks: SessionTaskStore = store.session_tasks
    task = tasks.create(SessionTaskDraft(sender.thread_id, target.thread_id, "goal", body_dedupe_key="body:k"))
    tasks.advance(task.task_id, SessionTaskUpdate(status="cancelled"))
    controlled = tasks.record_cancellation_control(task.task_id, outstanding_refs=["exact-ref"])
    bound = tasks.bind_body(task.task_id, "guidance-1")
    assert bound.status == "cancelled" and bound.body_guidance_id == "guidance-1"
    assert bound.metadata == controlled.metadata
    assert tasks.bind_body(task.task_id, "guidance-1") == bound
    assert tasks.bind_body(task.task_id, "guidance-other") == bound
    assert tasks.bind_body(task.task_id, "") == bound
    assert tasks.bind_body("stask-missing", "guidance-1") is None
    with pytest.raises(ValueError):
        tasks.advance(task.task_id, SessionTaskUpdate(status="queued"))
