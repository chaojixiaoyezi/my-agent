"""W5：真实派活、claim、队列与消费；只有模型生成由 Event 控制。"""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
)
from agent_py_agent.agent.conversation import runtime as runtime_module
from agent_py_agent.agent.conversation import store_wake_publication as publication_module
from agent_py_agent.agent.conversation.background_team_lifecycle import background_identity
from agent_py_agent.agent.conversation.models import ConversationThread, WakeSignal
from agent_py_agent.agent.conversation.session_tasks import SESSION_TASK_TERMINAL_STATUSES
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.wake_poison import ledger_corrupt_decision
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.tests.test_v14_w5_model_set_adoption import (
    business_wire as business_wire,
)
from agent_py_agent.tests.test_v14_w5_model_set_adoption import (
    completed,
)


# LLM: 生成器只替代外部模型；事件在首个流片段之后发布，记录真实调用重叠并在finally归还计数。
# 类用途: 让退休、第二次调度和取消严格发生在同一业务生成期间。
class StreamingModel:
    name = "w5-controlled-model"

    def __init__(self, *, block=False):
        self.calls = self.active = self.peak = 0
        self.lock, self.first_chunk, self.release = Lock(), Event(), Event()
        self.claims = []
        if not block:
            self.release.set()

    def generate(self, prompt, on_chunk=None, **kwargs):
        with self.lock:
            self.calls += 1
            number = self.calls
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if on_chunk:
                on_chunk("正在处理")
            self.first_chunk.set()
            assert self.release.wait(15), "test did not release streamed model"
            return ModelResponse(text=f"W5_RESULT_{number}", backend=self.name)
        finally:
            with self.lock:
                self.active -= 1


# LLM: 对端业务服务、正文派发及任务身份全部来自真实HTTP调用，不手造空间或任务记录。
# 函数用途: 创建可复用助手并绑定受控模型与原调度器。
def runtime_case(business_wire, *, block=False):
    host, client = business_wire
    space = completed(client, "spaces.create", {"name": "后台责任核验"})
    view = completed(client, "background_agents.create", {
        "space_id": space["space_id"], "name": "worker", "after_task": "reuse"})["agent"]
    model = StreamingModel(block=block)
    host.backend = model
    scheduler = BackgroundMainAgentScheduler({"runtime": BackgroundMainAgentRuntime(
        agent=host, store=host.conversation_store), "store": host.conversation_store})
    return host, client, space["space_id"], view, model, scheduler


def assign(case, key):
    _host, client, space_id, view, _model, _scheduler = case
    return completed(client, "tasks.assign", {"space_id": space_id, "agent_id": view["agent_id"],
        "instruction": "整理一份简短文字结果：" + key}, idempotency_key="w5-assignment-" + key)["task_id"]


def task_wakes(store, task_id):
    return [wake for wake in store.wakes.pending(limit=0)
        if wake.reason == "session_task" and wake.metadata.get("session_task_id") == task_id]


def finals(store, thread_id):
    messages, errors = store.messages.recent_report(thread_id, limit=100)
    assert not errors
    return [message for message in messages if message.role == "assistant" and message.content.strip()]


def drain(case):
    _host, client, _space_id, view, _model, _scheduler = case
    current = completed(client, "background_agents.get", {"agent_id": view["agent_id"]})["agent"]
    return completed(client, "background_agents.retire", {"agent_id": view["agent_id"], "disposition": "drain"},
        expected_revision=current["revision"])


def finish_retirement(case):
    host, _client, _space_id, view, _model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    states = []
    for _ in range(5):
        state = background_identity(store.threads.require(thread_id)).identity_state
        states.append(state)
        if state == "retired":
            break
        scheduler.tick_thread(thread_id)
    assert states[-1] == "retired", states
    assert store.threads.require(thread_id).active_task_ids == ()
    # 退休只清活跃责任；分页历史仍须能读回原任务完成事实，不能为断言删历史。
    for task_id in store.tasks.iter_task_ids(thread_id):
        link, error = store.tasks.link_report(thread_id, task_id)
        assert error is None and link is not None and link.status == "completed"
    return states


def test_two_pending_session_tasks_keep_separate_completion_responsibilities(business_wire, monkeypatch):
    case = runtime_case(business_wire)
    host, _client, _sid, view, model, scheduler = case
    store = host.conversation_store
    first, second = assign(case, "first"), assign(case, "second")
    assert len(task_wakes(store, first)) == len(task_wakes(store, second)) == 1
    batches, select = [], runtime_module._select_wake_batch

    def observed(*args, **kwargs):
        batch = select(*args, **kwargs)
        if batch[0].reason == "session_task":
            batches.append({wake.metadata["session_task_id"] for wake in batch})
        return batch

    monkeypatch.setattr(runtime_module, "_select_wake_batch", observed)
    scheduler.tick_thread(view["agent_id"])
    assert all(len(batch) == 1 for batch in batches), batches
    assert store.session_tasks.require(first).status == "done"
    assert store.session_tasks.require(second).status == "queued"
    assert len(task_wakes(store, second)) == 1
    scheduler.tick_thread(view["agent_id"])
    assert store.session_tasks.require(second).status == "done"
    assert model.calls == 2 and model.peak == 1
    assert len(finals(store, view["agent_id"])) == 2


@pytest.mark.parametrize("left,right,expected", [("A", "B", False), ("", "", False),
    ("A", "", False), ("A", "A", True)])
def test_only_same_nonempty_session_task_identity_can_batch(left, right, expected):
    first = WakeSignal("wake-first", "thread", reason="session_task", metadata={"session_task_id": left})
    second = replace(first, wake_signal_id="wake-second", metadata={"session_task_id": right})
    assert runtime_module._same_reason_wake_batch(first, second) is expected


@pytest.mark.parametrize("extra_same_task", [False, True])
def test_stream_drain_restore_assign_has_one_execution_and_one_final_per_task(business_wire, monkeypatch, extra_same_task):
    case = runtime_case(business_wire, block=True)
    host, client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    task_id = assign(case, "stream")
    if extra_same_task:
        original_wake = task_wakes(store, task_id)[0]
        store.wakes.raise_signal({**original_wake.to_dict(),
            "dedupe_key": f"session_task:{task_id}:same-task-event", "retain_handled": True})
    statuses, handled_statuses = [], []
    advance, mark = store.session_tasks.advance, store.wakes.mark_handled

    def advancing(identifier, update, **kwargs):
        result = advance(identifier, update, **kwargs)
        if identifier == task_id:
            statuses.append(result.status)
        return result

    def marking(identifier, **kwargs):
        wake = store.wakes.pending_one(identifier)
        if wake and wake.reason == "session_task":
            handled_statuses.append(store.session_tasks.require(wake.metadata["session_task_id"]).status)
        return mark(identifier, **kwargs)

    monkeypatch.setattr(store.session_tasks, "advance", advancing)
    monkeypatch.setattr(store.wakes, "mark_handled", marking)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(scheduler.tick_thread, thread_id)
        try:
            assert model.first_chunk.wait(10)
            claim = store.claims.load(thread_id)
            model.claims.append(copy.deepcopy(claim))
            assert claim["status"] == "running"
            # 原session_task wake不设置root，外层claim的task_id同样为空；真实turn随后由metadata绑定。
            assert claim["task_id"] == ""
            assert store.session_tasks.require(task_id).conversation_request_id == task_id
            retired = drain(case)
            assert retired["agent"]["identity_state"] == "retiring"
            assert len(task_wakes(store, task_id)) == (2 if extra_same_task else 1)
            scheduler.tick_thread(thread_id)  # 第二个消费者仍不能重入相同agent的claim。
            assert model.calls == model.peak == 1
        finally:
            model.release.set()
            future.result(timeout=15)
    assert store.session_tasks.require(task_id).status == "done"
    original = copy.deepcopy(store.session_tasks.require(task_id).metadata.get("task_learning_candidate"))
    finish_retirement(case)
    assert model.calls == 1 and len(finals(store, thread_id)) == 1
    assert statuses.count("done") == 1
    terminal_seen = False
    for status in statuses:
        assert not terminal_seen or status in SESSION_TASK_TERMINAL_STATUSES
        terminal_seen |= status in SESSION_TASK_TERMINAL_STATUSES
    assert handled_statuses and all(status in SESSION_TASK_TERMINAL_STATUSES for status in handled_statuses)
    assert store.session_tasks.require(task_id).metadata.get("task_learning_candidate") == original
    retired_view = completed(client, "background_agents.get", {"agent_id": thread_id})["agent"]
    restored = completed(client, "background_agents.restore", {"agent_id": thread_id}, expected_revision=retired_view["revision"])
    assert restored["agent"]["identity_state"] == "available"
    second = assign(case, "after-restore")
    scheduler.tick_thread(thread_id)
    assert store.session_tasks.require(second).status == "done"
    assert model.calls == 2 and model.peak == 1
    assert len(finals(store, thread_id)) == 2 and store.threads.require(thread_id).active_task_ids == ()
    for completed_id in (task_id, second):
        link, error = store.tasks.link_report(thread_id, completed_id)
        assert error is None and link is not None and link.status == 'completed'


def test_suspended_pending_body_without_live_wake_is_resumed_once_by_drain(business_wire):
    case = runtime_case(business_wire)
    host, client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    task_id = assign(case, "suspended")
    suspended = completed(client, "background_agents.suspend", {"agent_id": thread_id}, expected_revision=view["revision"])
    assert suspended["agent"]["identity_state"] == "suspended"
    # 显式交回原调度责任，保留真实未消费正文，模拟已经停下的暂停工作。
    for wake in task_wakes(store, task_id):
        store.wakes.mark_handled(wake.wake_signal_id)
    task = store.session_tasks.require(task_id)
    assert task.status == "queued" and store.guidance.receipt(task.body_dedupe_key).status == "pending"
    assert drain(case)["agent"]["identity_state"] == "retiring"
    assert len(task_wakes(store, task_id)) == 1
    scheduler.tick_thread(thread_id)
    finish_retirement(case)
    assert store.session_tasks.require(task_id).status == "done"
    assert model.calls == model.peak == 1 and len(finals(store, thread_id)) == 1


def test_old_terminal_task_wake_closes_under_claim_without_new_model_or_candidate(business_wire, monkeypatch):
    case = runtime_case(business_wire)
    host, _client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    task_id = assign(case, "original")
    scheduler.tick_thread(thread_id)
    task = store.session_tasks.require(task_id)
    assert task.status == "done"
    before, messages = task.to_dict(), finals(store, thread_id)
    late = store.wakes.raise_signal({"thread_id": thread_id, "reason": "session_task", "urgency": "normal",
        "dedupe_key": "session_task:" + task_id + ":old", "metadata": {"session_task_id": task_id}})
    observations, require = [], store.session_tasks.load

    def loaded(identifier):
        value = require(identifier)
        if identifier == task_id:
            # 最早的claim后准入点尚未进入模型的词法绑定；直接核原claim权威记录。
            claim, error = store.claims.load_report(thread_id)
            if not error and claim.get("status") == "running":
                observations.append(claim["claim_id"])
        return value

    monkeypatch.setattr(store.session_tasks, "load", loaded)
    scheduler.tick_thread(thread_id)
    assert model.calls == 1
    assert store.wakes.pending_one(late.wake_signal_id) is None
    assert observations, "terminal task must be re-read while holding the original claim"
    assert store.session_tasks.require(task_id).to_dict() == before
    assert finals(store, thread_id) == messages


# LLM: 复用真实业务适配器，只为定向检查producer读取预算；不替换领域存储。
# 函数用途: 取得当前真实助手的业务服务及原调用身份。
def producer_service(case):
    from agent_py_agent.agent.conversation.host_actor import HostChannelActor
    from agent_py_agent.agent.gateway_parts.business_api import (
        BusinessServices,
        _BusinessBackgroundService,
        bind_business_context,
    )

    host, _client, _sid, view, _model, _scheduler = case
    actor = HostChannelActor(str(host.home_paths.owner_id), view["managed_by_thread_id"], "check", "local", "local", "local-agent")
    context = bind_business_context(host, authorize=lambda action, facts: True, actor=actor)
    return _BusinessBackgroundService(BusinessServices(host), context), actor


@pytest.mark.parametrize("fault", ["unreadable", "authority_missing", "authority_invalid"])
def test_producer_preserves_unknown_claim_without_second_wake(business_wire, monkeypatch, fault):
    case = runtime_case(business_wire)
    host, _client, _sid, view, _model, _scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    task_id = assign(case, "claim-" + fault)
    for wake in task_wakes(store, task_id):
        store.wakes.mark_handled(wake.wake_signal_id)
    service, actor = producer_service(case)
    claim = {"thread_id": thread_id, "claim_id": "claim-unknown", "status": "running", "task_id": ""}
    if fault == "authority_invalid":
        claim["runtime_authority"] = {"run_id": "absent-run", "attempt_id": "absent-attempt"}
    calls = []

    def load_report(identifier, **kwargs):
        calls.append(identifier)
        return (claim, {"error": "simulated I/O error"} if fault == "unreadable" else None)

    monkeypatch.setattr(store.claims, "load_report", load_report)
    service._republish_pending_work(store.threads.require(thread_id), [store.session_tasks.require(task_id)], actor=actor)
    assert task_wakes(store, task_id) == []
    assert calls == [thread_id]


def test_producer_matches_running_runtime_context_when_dispatch_wake_is_reowned(business_wire, monkeypatch):
    case = runtime_case(business_wire, block=True)
    host, _client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    task_id = assign(case, "claim-only")
    service, actor = producer_service(case)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(scheduler.tick_thread, thread_id)
        try:
            assert model.first_chunk.wait(10)
            for wake in task_wakes(store, task_id):
                store.wakes.mark_handled(wake.wake_signal_id)
            assert service._running_session_task(store.threads.require(thread_id)) == task_id
            calls, original = [], store.claims.load_report

            def load_report(identifier, **kwargs):
                calls.append(identifier)
                return original(identifier, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(store.claims, "load_report", load_report)
                service._republish_pending_work(store.threads.require(thread_id), [store.session_tasks.require(task_id)], actor=actor)
            assert task_wakes(store, task_id) == []
            assert calls == [thread_id]
        finally:
            model.release.set()
            future.result(timeout=15)
    assert model.calls == model.peak == 1


# LLM: 取消发生在首流片段和drain之后；原业务取消及消费者结案运行，不把模型返回值当作取消后的有效final。
# 函数用途: 验证排空中的任务取消保持终态，不重执且身份最终可退休。
def test_cancel_during_streaming_drain_never_revives_task(business_wire):
    case = runtime_case(business_wire, block=True)
    host, client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    task_id = assign(case, "cancel-drain")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(scheduler.tick_thread, thread_id)
        try:
            assert model.first_chunk.wait(10)
            assert drain(case)["agent"]["identity_state"] == "retiring"
            result = completed(client, "tasks.cancel", {"task_id": task_id, "include_descendants": True},
                expected_revision=int(store.session_tasks.require(task_id).updated_at * 1_000_000))
            assert result["state"] in {"completed", "pending"}
            assert store.session_tasks.require(task_id).status == "cancelled"
        finally:
            model.release.set()
            future.result(timeout=15)
    before = store.session_tasks.require(task_id).to_dict()
    finish_retirement(case)
    scheduler.tick_thread(thread_id)
    assert store.session_tasks.require(task_id).status == "cancelled"
    assert store.session_tasks.require(task_id).metadata.get("task_learning_candidate") == before["metadata"].get("task_learning_candidate")
    assert model.calls == model.peak == 1
    assert task_wakes(store, task_id) == []
    assert finals(store, thread_id) == []


# LLM: 使用真实队列和临时owner；测试索引只定位、canonical才决定责任。
# 函数用途: 建立store专用夹具，不启动模型、网关或生产home。
def responsibility_store(tmp_path):
    store = ConversationStore(tmp_path / "responsibility")
    store.threads.write(ConversationThread(thread_id="thread-responsibility", canonical_user_id="user"))
    return store


# LLM: 不改dedupe格式，不把同task的不同事件强行合并。
# 函数用途: 在同一任务发布一个普通、无键或带键事件。
def responsibility_raise(store, key="dispatch", task="stask-responsibility", **extra):
    return store.wakes.raise_signal({"thread_id": "thread-responsibility", "reason": "session_task",
        "metadata": {"session_task_id": task}, "dedupe_key": key, **extra})


# LLM: 报告只输出定位事实，断言不依赖描述文字或内部SQL布局。
# 函数用途: 对同一任务读取完整责任报告。
def responsibility_report(store, task="stask-responsibility"):
    return store.wakes.task_responsibility_report("thread-responsibility", task)


# LLM: 同task保留多事件；处理和隔离消除旧责任，人工重放恢复原责任。
# 函数用途: 覆盖带键、无键、handled、quarantine、replay与新一代发布。
def test_store_responsibility_lifecycle_and_distinct_events(tmp_path):
    store = responsibility_store(tmp_path)
    assert responsibility_report(store) == ([], None)
    first = responsibility_raise(store)
    second = responsibility_raise(store, "")
    rows, error = responsibility_report(store)
    assert error is None
    assert {row["wake_signal_id"] for row in rows} == {first.wake_signal_id, second.wake_signal_id}
    store.wakes.mark_handled(first.wake_signal_id)
    rows, error = responsibility_report(store)
    assert error is None and [row["wake_signal_id"] for row in rows] == [second.wake_signal_id]
    store.wakes.attempts.quarantine(second.wake_signal_id, ledger_corrupt_decision(), now=10.0)
    assert responsibility_report(store) == ([], None)
    assert store.wakes.attempts.replay(second.wake_signal_id, now=11.0).ok
    rows, error = responsibility_report(store)
    assert error is None and [row["wake_signal_id"] for row in rows] == [second.wake_signal_id]
    third = responsibility_raise(store)
    assert third.wake_signal_id != first.wake_signal_id
    rows, error = responsibility_report(store)
    assert error is None and {row["wake_signal_id"] for row in rows} == {second.wake_signal_id, third.wake_signal_id}


# LLM: 在prepared提交后模拟安装失败；空receipt绝不能解释为任务无责任。
# 函数用途: 证明崩溃重试沿冻结信号恢复而不产生第二责任。
def test_store_responsibility_prepared_retry(tmp_path, monkeypatch):
    store = responsibility_store(tmp_path)
    install = publication_module._install

    def interrupted(*args, **kwargs):
        raise OSError("simulated I/O error")

    with monkeypatch.context() as patch:
        patch.setattr(publication_module, "_install", interrupted)
        with pytest.raises(OSError, match="simulated I/O error"):
            responsibility_raise(store)
    assert store.wakes.delivery_receipt("thread-responsibility", "dispatch") == ""
    rows, _error = responsibility_report(store)
    assert len(rows) == 1 and rows[0]["phase"] == "prepared"
    frozen_id = rows[0]["wake_signal_id"]
    assert publication_module._install is install
    retried = responsibility_raise(store, summary="new request cannot replace frozen content")
    assert retried.wake_signal_id == frozen_id and retried.summary == ""
    rows, error = responsibility_report(store)
    assert error is None and [row["wake_signal_id"] for row in rows] == [frozen_id]


# LLM: 只读打开不能建目录或修账；legacy没有覆盖证明，即使目标无行也必须unknown。
# 函数用途: 覆盖未初始化、旧数据显式重建及不扫描查询的读取上界。
def test_store_responsibility_legacy_rebuild_and_readonly(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    readonly = ConversationStore(missing, initialize=False)
    assert responsibility_report(readonly)[1] is not None
    assert not missing.exists()
    store = responsibility_store(tmp_path)
    for number in range(40):
        signal = responsibility_raise(store, f"history-{number}", task=f"stask-old-{number}")
        store.wakes.mark_handled(signal.wake_signal_id)
    legacy = WakeSignal("wake-legacy", "thread-responsibility", reason="session_task",
        dedupe_key="session_task:stask-responsibility:old-resume", metadata={"session_task_id": "stask-responsibility"})
    store.storage.wake_signal_path(legacy).write_text(json.dumps(legacy.to_dict()), encoding="utf-8")
    index = store.storage.wake_queue_dir / "task_responsibility.sqlite3"
    if index.exists():
        index.unlink()
    assert responsibility_report(store)[1] is not None
    assert not index.exists(), "query must not rebuild legacy coverage"
    store.wakes.rebuild_task_responsibility_index()
    before = {path.relative_to(store.storage.root): path.read_bytes()
        for path in store.storage.root.rglob("*") if path.is_file()}
    opened = ConversationStore(store.storage.root, initialize=False)
    reads, original = [], Path.read_text

    def counted(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)

    def no_scan(*args, **kwargs):
        raise AssertionError("task query must not scan queue or history")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", counted)
        patch.setattr(Path, "glob", no_scan)
        patch.setattr(Path, "rglob", no_scan)
        patch.setattr(Path, "iterdir", no_scan)
        rows, error = responsibility_report(opened)
        assert error is None and [row["wake_signal_id"] for row in rows] == [legacy.wake_signal_id]
        assert responsibility_report(opened, "stask-unseen") == ([], None)
    assert len(reads) <= 6, reads
    after = {path.relative_to(store.storage.root): path.read_bytes()
        for path in store.storage.root.rglob("*") if path.is_file()}
    assert after == before


# LLM: 缺canonical、坏canonical、坏索引均是unknown，而非可重发的空责任。
# 函数用途: 覆盖派生与权威损坏的fail-closed边界。
@pytest.mark.parametrize("damage", ["missing-index", "corrupt-index", "missing-wake", "corrupt-wake", "missing-publication"])
def test_store_responsibility_missing_and_corrupt_are_unknown(tmp_path, damage):
    store = responsibility_store(tmp_path)
    signal = responsibility_raise(store)
    index = store.storage.wake_queue_dir / "task_responsibility.sqlite3"
    if damage == "missing-index":
        index.unlink(missing_ok=True)
    elif damage == "corrupt-index":
        index.write_bytes(b"invalid sqlite")
    elif damage == "missing-wake":
        store.storage.wake_signal_path(signal).unlink()
    elif damage == "corrupt-wake":
        store.storage.wake_signal_path(signal).write_text("{", encoding="utf-8")
    else:
        store.storage.wake_dedupe_path(signal.thread_id, signal.dedupe_key).unlink()
    assert responsibility_report(store)[1] is not None


# LLM: 两producer由Event强制检查→发布交错；锁内报告和raise的重入必须有界结束。
# 函数用途: 证明同task只建立一个恢复责任，非阻塞争用在提交前拒绝。
def test_store_responsibility_two_producers_reentrant_guard(tmp_path):
    store = responsibility_store(tmp_path)
    checked, competitor_started, release = Event(), Event(), Event()

    def first():
        with store.wakes.task_responsibility_guard("thread-responsibility", "stask-responsibility"):
            assert responsibility_report(store) == ([], None)
            checked.set()
            assert release.wait(5)
            return responsibility_raise(store, "resume-first").wake_signal_id

    def second():
        competitor_started.set()
        with store.wakes.task_responsibility_guard("thread-responsibility", "stask-responsibility"):
            rows, error = responsibility_report(store)
            assert error is None
            return rows or [responsibility_raise(store, "resume-second")]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first)
        try:
            assert checked.wait(5)
            second_future = pool.submit(second)
            assert competitor_started.wait(5)
            with pytest.raises(BlockingIOError):
                with store.wakes.task_responsibility_guard("thread-responsibility", "stask-responsibility", blocking=False):
                    pass
        finally:
            release.set()
        identity = first_future.result(timeout=10)
        second_future.result(timeout=10)
    rows, error = responsibility_report(store)
    assert error is None and [row["wake_signal_id"] for row in rows] == [identity]


# LLM: producer只给一个task补责任；无关任务与历史wake不能进入读取集合，claim只读取一次。
# 函数用途: 验证旧resume代次的live责任可被有界查询识别，不依赖当前revision键。
def test_producer_old_generation_lookup_has_one_claim_and_task_only_reads(business_wire, monkeypatch):
    case = runtime_case(business_wire)
    host, _client, _sid, view, _model, _scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    target, unrelated = assign(case, "budget-target"), assign(case, "budget-other")
    for wake in task_wakes(store, target):
        store.wakes.mark_handled(wake.wake_signal_id)
    live = store.wakes.raise_signal({"thread_id": thread_id, "reason": "session_task",
        "dedupe_key": f"session_task:{target}:older-resume", "metadata": {"session_task_id": target}})
    service, actor = producer_service(case)
    reads, claims = [], []
    original_read, original_claim = Path.read_text, store.claims.load_report

    def counted(path, *args, **kwargs):
        if path.is_relative_to(store.storage.wake_queue_dir):
            reads.append(path)
        return original_read(path, *args, **kwargs)

    def claimed(*args, **kwargs):
        claims.append(args)
        return original_claim(*args, **kwargs)

    def no_queue_scan(*args, **kwargs):
        raise AssertionError("producer must not scan the wake queue")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", counted)
        patch.setattr(store.claims, "load_report", claimed)
        patch.setattr(store.wakes, "pending_report", no_queue_scan)
        service._republish_pending_work(store.threads.require(thread_id), [store.session_tasks.require(target)], actor=actor)
    assert len(claims) == 1
    assert len(reads) <= 2 and set(reads) == {
        store.storage.wake_signal_path(live), store.storage.wake_dedupe_path(thread_id, live.dedupe_key)}
    assert task_wakes(store, target) == [live]
    assert len(task_wakes(store, unrelated)) == 1


# LLM: 索引/canonical错误与prepared都不能当作没有责任；私有producer仍运行真实存储与授权。
# 函数用途: 验证未知或半提交责任保留原JSON，不产生新wake；显式恢复可修派生库，canonical缺失必须可观察地拒绝而非假成功。
@pytest.mark.parametrize("state", ["missing-index", "corrupt-index", "missing-wake", "prepared"])
def test_producer_preserves_unknown_or_prepared_task_publication(business_wire, monkeypatch, state):
    case = runtime_case(business_wire)
    host, _client, _sid, view, _model, _scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    task_id = assign(case, "preserve-" + state)
    wake = task_wakes(store, task_id)[0]
    if state == "missing-index":
        store.storage.wake_task_index_path.unlink()
    elif state == "corrupt-index":
        store.storage.wake_task_index_path.write_bytes(b"invalid sqlite")
    elif state == "missing-wake":
        store.storage.wake_signal_path(wake).unlink()
    else:
        store.wakes.mark_handled(wake.wake_signal_id)

        def interrupted(*args, **kwargs):
            raise OSError("simulated I/O error")

        with monkeypatch.context() as patch:
            patch.setattr(publication_module, "_install", interrupted)
            with pytest.raises(OSError, match="simulated I/O error"):
                store.wakes.raise_signal({"thread_id": thread_id, "reason": "session_task",
                    "dedupe_key": f"session_task:{task_id}:older-resume", "metadata": {"session_task_id": task_id}})
    before = {path: path.read_bytes() for path in store.storage.wake_queue_dir.rglob("*.json")}
    service, actor = producer_service(case)
    if state == "missing-wake":
        with pytest.raises(DataCorruptionError, match="recovery is pending"):
            service._republish_pending_work(store.threads.require(thread_id), [store.session_tasks.require(task_id)], actor=actor)
    else:
        service._republish_pending_work(store.threads.require(thread_id), [store.session_tasks.require(task_id)], actor=actor)
    assert {path: path.read_bytes() for path in store.storage.wake_queue_dir.rglob("*.json")} == before
    if state == "missing-index":
        rows, error = store.wakes.task_responsibility_report(thread_id, task_id)
        assert error is None and [row["wake_signal_id"] for row in rows] == [wake.wake_signal_id]


# LLM: 仅目标pending的unlink注入I/O失败；原handled/quarantine与report/producer均真实运行。
# 函数用途: 防止尽力删除失败后清定位，导致仍在队列的责任被错误判空并重发。
@pytest.mark.parametrize("settlement", ["handled", "quarantine"])
def test_settlement_delete_failure_preserves_live_task_responsibility(business_wire, monkeypatch, settlement):
    case = runtime_case(business_wire)
    host, _client, _sid, view, _model, _scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    task_id = assign(case, "delete-failure-" + settlement)
    wake = task_wakes(store, task_id)[0]
    pending, unlink = store.storage.wake_signal_path(wake), Path.unlink

    def interrupted(path, *args, **kwargs):
        if path == pending:
            raise OSError("simulated I/O error")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupted)
        if settlement == "handled":
            store.wakes.mark_handled(wake.wake_signal_id)
        else:
            store.wakes.attempts.quarantine(wake.wake_signal_id, ledger_corrupt_decision(), now=10.0)
    rows, error = store.wakes.task_responsibility_report(thread_id, task_id)
    assert error is not None or rows, "pending deletion failure must not turn live responsibility into empty coverage"
    service, actor = producer_service(case)
    service._republish_pending_work(store.threads.require(thread_id), [store.session_tasks.require(task_id)], actor=actor)
    assert task_wakes(store, task_id) == [wake]
