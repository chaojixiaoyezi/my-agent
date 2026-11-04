"""R1：session_task 唤醒责任索引自举、旧库覆盖升级与终态补账准入；真实存储/服务/消费者，只替换模型供应商。"""
import json
import os
import sqlite3
import stat
from contextlib import closing
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
)
from agent_py_agent.agent.conversation import store_wake_publication as publication
from agent_py_agent.agent.conversation.background_team_lifecycle import background_identity
from agent_py_agent.agent.conversation.im_delegation import prepare_delegated_execution
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.session_task_report import (
    TaskTurnOutcome,
    close_out_turn,
    delegated_result_report_pending,
    result_report_key,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.tests.test_im_delegation_runtime import runtime_fixture
from agent_py_agent.tests.test_v14_w5_background_wakes import (
    assign,
    finish_retirement,
    runtime_case,
    task_wakes,
)
from agent_py_agent.tests.test_v14_w5_model_set_adoption import (
    business_wire as business_wire,
)
from agent_py_agent.tests.test_v14_w5_model_set_adoption import (
    completed,
)

THREAD = "thread-r1"


# LLM: 统计本进程打开的描述符；列目录本身临时占用的那一个在前后两次测量里抵消。
# 函数用途: 为fd_delta == 0断言取当前描述符数量。
def _fd_count():
    return len(os.listdir("/dev/fd"))


# LLM: 线程记录走真实ThreadStore写入，不写任何唤醒或索引；initialize控制是否运行ensure_dirs。
# 函数用途: 建一个只含一个会话线程的会话根。
def _store(root, *, initialize=True):
    root.mkdir(parents=True, exist_ok=True)
    store = ConversationStore(root, initialize=initialize)
    store.threads.write(ConversationThread(thread_id=THREAD, canonical_user_id="user"))
    return store


# LLM: 与生产派活同形的带键session_task唤醒（retain_handled），身份只放在结构化metadata。
# 函数用途: 为一个任务发布一条派活唤醒。
def _raise(store, task_id, key="dispatch"):
    return store.wakes.raise_signal({"thread_id": THREAD, "reason": "session_task", "urgency": "normal",
        "dedupe_key": f"session_task:{task_id}:{key}", "retain_handled": True,
        "metadata": {"session_task_id": task_id}})


# 函数用途: 读取一个任务的责任报告，返回(仍待处理的wake ID列表, 错误)。
def _live(store, task_id):
    rows, error = store.wakes.task_responsibility_report(THREAD, task_id)
    return [row["wake_signal_id"] for row in rows], error


# LLM: W5之前的旧库只有canonical队列与发布账：真实发布一条已处理的保留去重记录和一条仍待处理的唤醒，再删掉派生索引与锁文件。
# 函数用途: 建一个无派生索引的旧版会话根，返回根路径和仍存活的唤醒。
def _legacy_root(tmp_path):
    store = _store(tmp_path / "legacy")
    store.wakes.mark_handled(_raise(store, "stask-old").wake_signal_id)
    live = _raise(store, "stask-live")
    store.storage.wake_task_index_path.unlink()
    store.storage.wake_task_index_lock_path.unlink()
    return store.storage.root, live


# LLM: 复现W5首个可写初始化留给旧库的派生状态：独占新建索引并写入coverage=unknown，从未自动重建。
# 函数用途: 在旧版会话根上放置W5留下的unknown覆盖索引。
def _w5_unknown_index(storage):
    descriptor = os.open(storage.wake_task_index_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(storage.wake_task_index_path)) as connection, connection:
        publication._create_task_index(connection, "unknown")


# 场景1：没有wake_queue/的根以initialize=False打开，session_task发布照常成功，报告列出这条存活唤醒。
def test_publish_on_root_without_wake_queue_lists_live_wake(tmp_path):
    store = _store(tmp_path / "bare", initialize=False)
    assert not store.storage.wake_queue_dir.exists()
    before = _fd_count()
    signal = _raise(store, "stask-bare")
    live, error = _live(store, "stask-bare")
    assert _fd_count() == before, "publication or report leaked a descriptor"
    assert error is None and live == [signal.wake_signal_id]
    assert stat.S_IMODE(store.storage.wake_queue_dir.stat().st_mode) == 0o700
    assert store.storage.wake_task_locks_dir.is_dir()
    assert publication._task_index_coverage(store.storage) == "complete"


# 场景2：旧库无索引、只读打开时发布成功；下一次可写打开后覆盖complete，且只列出真正存活的唤醒。
def test_legacy_root_without_index_publishes_and_writable_open_is_complete(tmp_path):
    root, live = _legacy_root(tmp_path)
    readonly = ConversationStore(root, initialize=False)
    before = _fd_count()
    published = _raise(readonly, "stask-new")
    assert _fd_count() == before, "publication on a legacy root leaked a descriptor"
    reopened = ConversationStore(root)
    assert publication._task_index_coverage(reopened.storage) == "complete"
    assert _live(reopened, "stask-old") == ([], None)
    assert _live(reopened, "stask-live") == ([live.wake_signal_id], None)
    assert _live(reopened, "stask-new") == ([published.wake_signal_id], None)


# 场景2（V14F-07）：W5留下unknown覆盖的旧库，下一次可写打开从canonical重建为complete；之后的打开不再重建。
def test_w5_unknown_coverage_is_upgraded_by_next_writable_open(tmp_path, monkeypatch):
    root, live = _legacy_root(tmp_path)
    storage = ConversationStore(root, initialize=False).storage
    _w5_unknown_index(storage)
    assert _live(ConversationStore(root, initialize=False), "stask-live")[1] is not None, "precondition: unknown coverage"
    before = _fd_count()
    reopened = ConversationStore(root)
    assert _fd_count() == before, "upgrade leaked a descriptor"
    assert _live(reopened, "stask-live") == ([live.wake_signal_id], None)
    assert _live(reopened, "stask-old") == ([], None)
    assert publication._task_index_coverage(reopened.storage) == "complete"
    rebuilds, rebuild = [], publication._rebuild_task_index_locked

    def counted(target):
        rebuilds.append(str(target.root))
        return rebuild(target)

    monkeypatch.setattr(publication, "_rebuild_task_index_locked", counted)
    ConversationStore(root)
    assert rebuilds == [], "complete coverage must not be rebuilt again"


# LLM: 重建遇到真实坏canonical时只保留unknown（读者fail-closed）且不从ensure_dirs抛出；同进程同根不反复全量重建。
# 函数用途: 验证unknown覆盖的重建每进程每根至多一次，失败不阻断可写打开。
def test_failed_unknown_rebuild_keeps_fail_closed_and_runs_once(tmp_path, monkeypatch):
    root, _live_signal = _legacy_root(tmp_path)
    storage = ConversationStore(root, initialize=False).storage
    _w5_unknown_index(storage)
    (storage.wake_queue_dir / "normal" / "wake-damaged.json").write_text("{", encoding="utf-8")
    rebuilds, rebuild = [], publication._rebuild_task_index_locked

    def counted(target):
        rebuilds.append(str(target.root))
        return rebuild(target)

    monkeypatch.setattr(publication, "_rebuild_task_index_locked", counted)
    first = ConversationStore(root)
    assert publication._task_index_coverage(first.storage) == "unknown"
    assert _live(first, "stask-live")[1] is not None
    ConversationStore(root)
    assert len(rebuilds) == 1


# 场景3：零字节或无效字节的索引都不挡发布；可写初始化后覆盖complete且列出这条唤醒。
@pytest.mark.parametrize("damage", [b"", b"not a sqlite database"], ids=["zero-byte", "invalid-bytes"])
def test_damaged_index_never_blocks_publication(tmp_path, damage):
    store = _store(tmp_path / "damaged")
    store.storage.wake_task_index_path.write_bytes(damage)
    before = _fd_count()
    signal = _raise(store, "stask-damaged")
    after = _fd_count()
    assert after == before, "publication over a damaged index leaked a descriptor"
    assert store.wakes.pending_one(signal.wake_signal_id) is not None
    assert _live(store, "stask-damaged")[1] is not None, "lost projection must read as unknown, not empty"
    reopened = ConversationStore(store.storage.root)
    assert publication._task_index_coverage(reopened.storage) == "complete"
    assert _live(reopened, "stask-damaged") == ([signal.wake_signal_id], None)


# LLM: 通过真实create_session_task工具派活；发送方取助手的管理会话，宿主运行参数只携带当前会话这一结构化字段。
# 函数用途: 经工具路径给暂停中的后台助手派一条任务并返回task_id。
def _tool_dispatch(host, view, goal):
    from agent_py_agent.agent.agent_core.orchestration.tools.create_session_task import (
        CreateSessionTaskTool,
    )

    host._current_run_params = SimpleNamespace(task_attributes={"conversation_thread_id": view["managed_by_thread_id"]})
    try:
        outcome = CreateSessionTaskTool(host).execute({"target_thread_id": view["agent_id"], "goal": goal})
    finally:
        del host._current_run_params
    assert outcome.ok, outcome.output
    return json.loads(outcome.output)["task_id"]


# 场景4：暂停→工具派活（不发唤醒）→恢复，恰好得到一条 session_task:{task}:{generation} 唤醒，执行一次。
def test_resume_after_tool_dispatch_to_suspended_identity_wakes_once(business_wire):
    case = runtime_case(business_wire)
    host, client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    suspended = completed(client, "background_agents.suspend", {"agent_id": thread_id}, expected_revision=view["revision"])
    task_id = _tool_dispatch(host, view, "整理一份简短文字结果：暂停后派活")
    assert task_wakes(store, task_id) == []
    completed(client, "background_agents.resume", {"agent_id": thread_id}, expected_revision=suspended["agent"]["revision"])
    generation = background_identity(store.threads.require(thread_id)).identity_revision
    assert [wake.dedupe_key for wake in task_wakes(store, task_id)] == [f"session_task:{task_id}:{generation}"]
    scheduler.tick_thread(thread_id)
    assert store.session_tasks.require(task_id).status == "done"
    assert model.calls == model.peak == 1


# 场景4（V14F-07）：W5升级后的unknown旧库，可写打开完成覆盖升级；暂停→工具派活→退休drain恰好恢复一次，完成后身份retired。
def test_drain_on_legacy_upgraded_store_resumes_tool_task_and_retires(business_wire):
    case = runtime_case(business_wire)
    host, client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    earlier = assign(case, "r1-before-upgrade")
    scheduler.tick_thread(thread_id)
    assert store.session_tasks.require(earlier).status == "done"
    # 已处理的派活唤醒保留去重记录，正是W5把覆盖标成unknown的旧账。
    assert next(store.storage.wake_dedupe_dir.glob("*.json"), None) is not None
    store.storage.wake_task_index_path.unlink()
    _w5_unknown_index(store.storage)
    current = completed(client, "background_agents.get", {"agent_id": thread_id})["agent"]
    suspended = completed(client, "background_agents.suspend", {"agent_id": thread_id}, expected_revision=current["revision"])
    task_id = _tool_dispatch(host, view, "整理一份简短文字结果：升级后排空")
    assert task_wakes(store, task_id) == []
    assert store.wakes.task_responsibility_report(thread_id, task_id)[1] is not None, "precondition: unknown coverage"
    ConversationStore(store.storage.root)
    assert store.wakes.task_responsibility_report(thread_id, task_id) == ([], None), "writable open must upgrade coverage"
    retired = completed(client, "background_agents.retire", {"agent_id": thread_id, "disposition": "drain"},
        expected_revision=suspended["agent"]["revision"])
    assert retired["agent"]["identity_state"] == "retiring"
    generation = background_identity(store.threads.require(thread_id)).identity_revision
    assert [wake.dedupe_key for wake in task_wakes(store, task_id)] == [f"session_task:{task_id}:{generation}"]
    scheduler.tick_thread(thread_id)
    assert store.session_tasks.require(task_id).status == "done"
    finish_retirement(case)
    assert model.calls == 2 and model.peak == 1


# 场景5：只读报告遇到在途定位写（持SQLite写锁）时等待写完返回行，而不是立即报错。
def test_readonly_report_waits_for_inflight_locator_write(tmp_path):
    store = _store(tmp_path / "busy")
    signal = _raise(store, "stask-busy")
    other = _raise(store, "stask-writer")
    writing, release, reported = Event(), Event(), Event()
    result, failures = {}, []

    def hold_locator_write():
        try:
            with closing(publication._task_index_connection(store.storage, writable=True)) as connection:
                connection.execute("BEGIN EXCLUSIVE")
                connection.execute("INSERT OR REPLACE INTO locators VALUES (?, ?, ?, ?, ?, ?)",
                    (THREAD, "stask-writer", other.wake_signal_id, other.dedupe_key, other.urgency, 1))
                writing.set()
                assert release.wait(5)
                connection.commit()
        except BaseException as exc:  # noqa: BLE001 - 交给主线程断言
            failures.append(exc)
            writing.set()

    def report():
        result["value"] = _live(store, "stask-busy")
        reported.set()

    writer = Thread(target=hold_locator_write, daemon=True)
    writer.start()
    assert writing.wait(5) and not failures
    reader = Thread(target=report, daemon=True)
    reader.start()
    try:
        assert not reported.wait(0.3), "read-only report must wait for the locator write instead of failing closed"
    finally:
        release.set()
        writer.join(5)
        reader.join(5)
    assert not writer.is_alive() and not reader.is_alive() and not failures
    assert result["value"] == ([signal.wake_signal_id], None)
    assert _live(store, "stask-writer") == ([other.wake_signal_id], None)


# 场景6：外层进入时索引缺失（外层先懒初始化），内层同根不同任务不再初始化；期间索引再丢也不自锁，异根门照常初始化。
def test_nested_guards_for_two_tasks_do_not_deadlock_while_index_missing(tmp_path):
    store = _store(tmp_path / "nested")
    foreign = _store(tmp_path / "foreign")
    index = store.storage.wake_task_index_path
    index.unlink()
    foreign.storage.wake_task_index_path.unlink()
    done, failures, published = Event(), [], {}

    def nested():
        try:
            with store.wakes.task_responsibility_guard(THREAD, "stask-outer"):
                assert index.is_file(), "outer entry must initialize the missing index before taking its locks"
                index.unlink()
                with store.wakes.task_responsibility_guard(THREAD, "stask-inner"):
                    published["inner"] = _raise(store, "stask-inner")
                with foreign.wakes.task_responsibility_guard(THREAD, "stask-foreign"):
                    assert foreign.storage.wake_task_index_path.is_file(), "a guard on another root must not block init"
            done.set()
        except BaseException as exc:  # noqa: BLE001 - 交给主线程断言
            failures.append(exc)

    worker = Thread(target=nested, daemon=True)
    worker.start()
    worker.join(5)
    assert done.is_set() and not failures, failures or "nested guards deadlocked"
    # 内层在同根门内发布：定位写遇到缺失索引按投影丢失处理，canonical照常；下一次门入口从权威源重建。
    assert store.wakes.pending_one(published["inner"].wake_signal_id) is not None
    assert not index.exists()
    with store.wakes.task_responsibility_guard(THREAD, "stask-next"):
        pass
    assert _live(store, "stask-inner") == ([published["inner"].wake_signal_id], None)


# LLM: 只替换模型供应商边界；任何调用都会被记下，补账路径必须保持零次。
# 类用途: 记录后台片是否真的调用了模型。
class _CountingModel:
    name = "r1-counting-model"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt, on_chunk=None, **kwargs):
        self.calls += 1
        return ModelResponse(text="R1_UNEXPECTED_MODEL_CALL", backend=self.name)


# 场景7（V14F-03）：委派结果首次回报因发布忙被拒；重试claim零模型把原结果交给sender，原wake在回报发布后才结案。
def test_im_delegated_retry_claim_reports_result_before_closing_wake(tmp_path, monkeypatch):
    agent, params, task, _context, _source = runtime_fixture(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    store = agent.conversation_store
    wake = next(signal for signal in store.wakes.pending(limit=0)
        if signal.reason == "session_task" and signal.metadata.get("session_task_id") == task.task_id)
    assert wake.thread_id == task.target_thread_id and wake.root_task_id == ""
    publish = store.wakes.raise_signal
    with monkeypatch.context() as patch:
        patch.setattr(store.wakes, "raise_signal",
            lambda *_a, **_k: (_ for _ in ()).throw(BlockingIOError("ordinary wake busy")))
        closed = close_out_turn(agent, task.task_id, TaskTurnOutcome(True, summary="原工作结果"))
    assert closed.status == "done" and store.wakes.raise_signal == publish
    key = result_report_key(closed)
    first = store.guidance.receipt(key)
    assert first is not None and store.wakes.delivery_receipt(task.sender_thread_id, key) == ""
    # 调度器派发的session_task唤醒root为空，claim参数task_id同样为空。
    assert delegated_result_report_pending(agent, task.target_thread_id, task_id="")
    # 被拒的那一片照原路结束执行权，原唤醒保留待重试。
    claim = store.claims.load(task.target_thread_id)
    assert store.claims.finish({"thread_id": task.target_thread_id, "claim_scope_id": claim["claim_scope_id"],
        "claim_id": claim["claim_id"], "status": "finished"})
    # 调度器线程不携带上一片的运行参数。
    del agent._current_run_params
    model = _CountingModel()
    agent.backend = model
    receipts_when_handled, mark = [], store.wakes.mark_handled

    def marking(identifier, **kwargs):
        if identifier == wake.wake_signal_id:
            receipts_when_handled.append(store.wakes.delivery_receipt(task.sender_thread_id, key))
        return mark(identifier, **kwargs)

    monkeypatch.setattr(store.wakes, "mark_handled", marking)
    scheduler = BackgroundMainAgentScheduler({"runtime": BackgroundMainAgentRuntime(
        agent=agent, store=store, channels=agent.delivery_service), "store": store})
    scheduler.tick_thread(task.target_thread_id)
    assert model.calls == 0, "terminal report repair must not call the model"
    assert receipts_when_handled == ["pending"], "original wake closed before the sender report was published"
    assert store.wakes.pending_one(wake.wake_signal_id) is None
    assert store.guidance.receipt(key).entry.guidance_id == first.entry.guidance_id
    assert not delegated_result_report_pending(agent, task.target_thread_id, task_id="")
    sender_wakes = [signal for signal in store.wakes.pending(limit=0) if signal.thread_id == task.sender_thread_id]
    assert [signal.dedupe_key for signal in sender_wakes] == [key]
    assert store.session_tasks.require(task.task_id).status == "done"
    # 目标会话只留下原结果的内部留档（canonical summary），没有任何模型生成的答复。
    replies = [message for message in store.messages.recent(task.target_thread_id, limit=0) if message.role == "assistant"]
    assert [(message.content, message.metadata.get("delivery_reason")) for message in replies] == [
        ("原工作结果", "delegated_internal_result")]
