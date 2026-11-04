import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from itertools import islice
from threading import Event, current_thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import session_tasks as module
from agent_py_agent.agent.conversation.session_tasks import SessionTask, _write_completion_metadata


# LLM: 统计真实 SQL 游标交付的候选/引用行，不把查询次数冒充读取行数。
# 函数用途: 在现有连接与锁内观察待办页查询，不改变查询、数据或事务行为。
def _observe_pending_sql(monkeypatch, counts):
    from agent_py_agent.agent.conversation.session_task_pending_index import SessionTaskPendingIndex

    original = SessionTaskPendingIndex._connection

    # LLM: 透明装饰真实 SQL 游标，不筛选返回行。
    # 类用途: 统计数据库实际交付的定位行。
    class ObservedCursor:
        # LLM: 保留真实游标，只在 fetchall 交付后记账。
        # 函数用途: 绑定原游标及当前查询分类。
        def __init__(self, cursor, key):
            self.cursor, self.key = cursor, key

        # LLM: 使用原 SQL 结果，不过滤或补造行。
        # 函数用途: 记录本次实际返回的定位行数量。
        def fetchall(self):
            rows = self.cursor.fetchall()
            counts[self.key] += len(rows)
            return rows

    # LLM: 原连接负责执行、事务和所有权，此处只分类查询。
    # 类用途: 将计数游标接到真实连接上。
    class ObservedConnection:
        # LLM: 只装饰当前原连接，不另开数据库或改锁顺序。
        # 函数用途: 保存原连接供透明委托。
        def __init__(self, conn):
            self.conn = conn

        # LLM: total_changes 等原连接属性必须保持原义。
        # 函数用途: 委托非观测接口。
        def __getattr__(self, name):
            return getattr(self.conn, name)

        # LLM: 只测量分页候选及两引用探测的真实结果，其他 SQL 原样执行。
        # 函数用途: 按查询分类装饰 fetchall。
        def execute(self, sql, args=()):
            cursor = self.conn.execute(sql, args)
            if sql.startswith("SELECT * FROM entries"):
                return ObservedCursor(cursor, "candidate_rows")
            if sql.startswith("SELECT path,stamp FROM"):
                return ObservedCursor(cursor, "reference_rows")
            return cursor

    # LLM: 原 context manager 负责提交、回滚和释放原索引锁。
    # 函数用途: 将观测连接限定于被测调用的生命周期。
    @contextmanager
    def observe(index):
        with original(index) as conn:
            yield ObservedConnection(conn)

    monkeypatch.setattr(SessionTaskPendingIndex, "_connection", observe)


# LLM: 回填后按真实 SQL 行和 canonical 读取数核对欠账候选。
# 函数用途: 验证修复调用不会遍历终态历史。
@pytest.mark.parametrize("history", [1000, 5000, 10000])
@pytest.mark.parametrize("pending", [0, 1, 8])
def test_repair_opens_only_owed_tasks_after_bounded_backfill(tmp_path, monkeypatch, history, pending):
    store = ConversationStore(tmp_path / "conversations")
    tasks = store.session_tasks
    tasks._dir().mkdir(parents=True, exist_ok=True)
    for n in range(history + pending):
        owed = n >= history
        task = SessionTask(f"task-{n:05}", "source", "target", "method", status="done", created_at=n + 1,
                           metadata={"task_learning_enqueue": {"status": "ineligible",
                               "reason": "canonical_completion_reader_unbound" if owed else "background_learning_disabled"}})
        tasks._path(task.task_id).write_text(json.dumps(task.to_dict()))
    host = SimpleNamespace(conversation_store=store, subagents=SimpleNamespace(runtime_db=object()))
    page_reader = getattr(tasks, "pending_learning_page", tasks.list_page)
    for _ in range(100):
        page = page_reader(limit=16)
        if page["coverage"] == "complete":
            break
    else:
        pytest.fail("bounded backfill did not converge")
    reads, repaired, sql_counts = [], Counter(), Counter()
    _observe_pending_sql(monkeypatch, sql_counts)
    original = module.read_json_file_report

    # LLM: 真实读取器及其错误不变，只记录任务编号。
    # 函数用途: 统计 canonical 任务读取。
    def observe(path, **kwargs):
        reads.append(path.stem)
        return original(path, **kwargs)

    # LLM: 只替代外部学习动作，结清仍经真实锁内 updater 和索引发布。
    # 函数用途: 精确记录并结清欠账任务。
    def retire(host, task_id):
        repaired[task_id] += 1
        task = tasks.require(task_id)
        _write_completion_metadata(host, task, lambda current: {**current.metadata,
            "task_learning_enqueue": {"status": "ineligible", "reason": "legacy_request_material_missing"}})

    monkeypatch.setattr(module, "read_json_file_report", observe)
    monkeypatch.setattr(module, "retry_completed_task_learning", retire)
    module.repair_pending_completed_task_learning_wakes(host)
    assert all(int(name.split("-")[-1]) >= history for name in reads), reads
    assert len(reads) <= pending * 5
    assert len(repaired) == pending and set(repaired.values()) <= {1}
    assert sql_counts["candidate_rows"] == pending, sql_counts
    assert sql_counts["reference_rows"] <= 2, sql_counts
    print(f"history={history} owed={pending} json_reads={len(reads)} sql={dict(sql_counts)}")
    sql_counts.clear()
    reads.clear()
    module.repair_pending_completed_task_learning_wakes(host)
    assert set(repaired.values()) <= {1}
    assert sql_counts["candidate_rows"] == 0 and sql_counts["reference_rows"] <= 2, sql_counts
    # 结清走原子替换，目录 stamp 会改变；原协议允许重新做一批有界回填。
    assert len(reads) <= 128
    for _ in range(100):
        reads.clear()
        page = page_reader(limit=16)
        assert len(reads) <= 128
        if page["coverage"] == "complete":
            break
    else:
        pytest.fail("post-write bounded backfill did not converge")
    reads.clear()
    sql_counts.clear()
    module.repair_pending_completed_task_learning_wakes(host)
    assert reads == []
    assert sql_counts["candidate_rows"] == 0 and sql_counts["reference_rows"] <= 2, sql_counts


# LLM: 查询计划来自真实 SQLite，无过滤分页不得依赖临时排序。
# 函数用途: 核对公共列表的排序索引。
def test_unfiltered_page_uses_order_index_without_temp_sort(tmp_path):
    index = ConversationStore(tmp_path / "conversations").session_tasks._list_index()
    with index._connection() as conn:
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM entries ORDER BY sort_at DESC,path DESC LIMIT 17").fetchall()
        assert not any("TEMP B-TREE" in str(tuple(row)) for row in plan)


# LLM: 使用原完成事件和真实学习唤醒，重开后仍只认同一条唤醒。
# 函数用途: 验证完成欠账恢复的持久幂等性。
def test_real_completion_wake_repair_is_idempotent_across_reopen(tmp_path):
    from agent_py_agent.tests.test_skill_learning_integration import _completed_learning_host

    _ctx, host, completion = _completed_learning_host(tmp_path)
    task = host.conversation_store.session_tasks.require(completion.task_id)
    _write_completion_metadata(host, task, lambda current: {key: value for key, value in current.metadata.items()
                                                          if key != "task_learning_wake_id"})
    host.conversation_store = ConversationStore(host.conversation_store.storage.root)
    for _ in range(4):
        module.repair_pending_completed_task_learning_wakes(host)
    saved = host.conversation_store.session_tasks.require(task.task_id)
    assert saved.metadata["task_learning_wake_id"] == task.metadata["task_learning_wake_id"]
    assert saved.status == "done"
    assert host.conversation_store.session_tasks.pending_learning_page()["items"] == []


# LLM: 迟到正文绑定须锁内保留已提交终态，不能重新成为待办。
# 函数用途: 验证终态任务不会被旧写者复活。
def test_terminal_task_cannot_reenter_learning_index_from_late_bind(tmp_path):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft, SessionTaskUpdate

    tasks = ConversationStore(tmp_path / "conversations").session_tasks
    task = tasks.create(SessionTaskDraft("a", "b", "method"))
    tasks.advance(task.task_id, SessionTaskUpdate("cancelled"))
    tasks.bind_body(task.task_id, "body")
    assert tasks.require(task.task_id).status == "cancelled"
    assert tasks.pending_learning_page()["items"] == []


# LLM: 覆盖点读及两种回填，被监听目录中不允许产生新锁文件。
# 函数用途: 验证旧 JSON 只读访问不污染目录时间戳。
def test_legacy_task_reads_do_not_create_sidecars_or_change_directory_stamp(tmp_path):
    tasks = ConversationStore(tmp_path / "conversations").session_tasks
    tasks._dir().mkdir(parents=True, exist_ok=True)
    task = SessionTask("legacy", "a", "b", "method", status="done", created_at=1)
    tasks._path(task.task_id).write_text(json.dumps(task.to_dict()))
    before = tasks._dir().stat()
    assert tasks.list_page()["items"][0].task_id == "legacy"
    assert tasks.require("legacy").status == "done"
    assert tasks.list_report()[0][0].task_id == "legacy"
    assert tasks.pending_learning_page()["coverage"] == "complete"
    after = tasks._dir().stat()
    assert (before.st_mtime_ns, before.st_ctime_ns) == (after.st_mtime_ns, after.st_ctime_ns)
    assert list(tasks._dir().glob("*.lock")) == []


# LLM: 回填后反复走真实分页，逐轮核对 FD 并累计解析字节。
# 函数用途: 验证十倍历史下列表热路有界且不泄漏句柄。
@pytest.mark.parametrize("history", [100, 1000])
def test_task_page_and_learning_repair_release_fds_with_bounded_json(tmp_path, monkeypatch, history):
    tasks = ConversationStore(tmp_path / "conversations").session_tasks
    for n in range(history):
        task = SessionTask(f"old-{n}", "source", "target", "method", status="done", created_at=n)
        tasks._path(task.task_id).write_text(json.dumps(task.to_dict()))
    for _ in range(history // 128 + 5):
        public = tasks.list_page(limit=5)
        learning = tasks.pending_learning_page(limit=16)
        if public["coverage"] == learning["coverage"] == "complete":
            break
    else:
        pytest.fail("task projections did not converge")
    counts = {"json": 0, "bytes": 0}
    loads = json.loads

    # LLM: 不替换解析结果，不改变存储或 SQL 路径。
    # 函数用途: 累计真实 JSON 解码成本。
    def observe(raw, *args, **kwargs):
        counts["json"] += 1
        counts["bytes"] += len(raw.encode() if isinstance(raw, str) else raw)
        return loads(raw, *args, **kwargs)

    monkeypatch.setattr(json, "loads", observe)
    before = len(os.listdir("/dev/fd")) if os.path.isdir("/dev/fd") else None
    for _ in range(5):
        assert len(tasks.list_page(limit=5)["items"]) == 5
        assert tasks.pending_learning_page(limit=16)["items"] == []
        if before is not None:
            assert len(os.listdir("/dev/fd")) == before
    assert counts["json"] <= 35 and counts["bytes"] <= 32768, counts


# LLM: 旧写者不发布投影且目录身份不变，重启/跨实例轮询仍须发现新欠账。
# 函数用途: 用真实原位 JSON 改写复现非 owed 引用被遗漏的回归。
@pytest.mark.parametrize("enqueue", [
    {"status": "pending"},
    {"status": "ineligible", "reason": "canonical_completion_reader_unbound"},
])
def test_inplace_legacy_writer_is_found_across_pending_index_instances(tmp_path, enqueue):
    from agent_py_agent.agent.conversation.session_list_index import _stamp

    root = tmp_path / "conversations"
    tasks = ConversationStore(root).session_tasks
    for n in range(9):
        task = SessionTask(f"legacy-{n}", "a", "b", "method", status="done", created_at=n,
                           metadata={"task_learning_enqueue": {"status": "ineligible",
                                     "reason": "background_learning_disabled"}})
        tasks._path(task.task_id).write_text(json.dumps(task.to_dict()))
    assert tasks.pending_learning_page()["coverage"] == "complete"
    other = ConversationStore(root).session_tasks
    assert other.pending_learning_page()["items"] == []
    directory_stamp = _stamp(tasks._dir())
    path = tasks._path("legacy-8")
    inode = path.stat().st_ino
    payload = json.loads(path.read_text())
    payload["metadata"]["task_learning_enqueue"] = enqueue
    path.write_text(json.dumps(payload))
    assert path.stat().st_ino == inode
    assert _stamp(tasks._dir()) == directory_stamp
    seen = set()
    for n in range(10):
        reader = tasks if n % 2 else other
        seen.update(task.task_id for task in reader.pending_learning_page()["items"])
        if "legacy-8" in seen:
            break
    assert seen == {"legacy-8"}
    assert _stamp(tasks._dir()) == directory_stamp
    assert list(tasks._dir().glob("*.lock")) == []


# LLM: 升级旧 owed-only 库不能把缺失的历史引用当作已完整；每次仍只做原128条回填。
# 函数用途: 验证旧库重启升级、跨实例收敛以及定位表不保存业务正文。
def test_pending_reference_schema_upgrade_is_bounded_and_restartable(tmp_path):
    from agent_py_agent.agent.conversation.session_list_index import SessionListIndex, _stamp

    root = tmp_path / "conversations"
    tasks = ConversationStore(root).session_tasks
    for n in range(260):
        task = SessionTask(f"history-{n:04}", "a", "b", "method", status="done", created_at=n)
        tasks._path(task.task_id).write_text(json.dumps(task.to_dict()))
    pending = tasks._learning_index()
    # 用未扩展的真实基类连接创建升级前 schema：已完整、没有 owed entries。
    legacy = SessionListIndex(tasks._dir(), kind="session_task", reader=pending.reader)
    legacy.path = pending.path
    with legacy._connection() as conn:
        legacy._put_meta(conn, "directory", _stamp(tasks._dir()))
        legacy._put_meta(conn, "complete", 1)
        legacy._put_meta(conn, "offset", 260)
        legacy._put_meta(conn, "generation", 7)
    directory = _stamp(tasks._dir())
    with os.scandir(tasks._dir()) as entries:
        first_batch = {entry.name for entry in islice(entries, 128)
                       if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False)}
    assert tasks.pending_learning_page()["coverage"] == "partial"
    with pending._connection() as conn:
        assert {row[0] for row in conn.execute("SELECT path FROM task_references")} == first_batch
        assert len(first_batch) <= 128
        assert pending._meta(conn, "offset") == "128"
        assert [row[1] for row in conn.execute("PRAGMA table_info(task_references)")] == [
            "path", "stamp", "generation"]
    reopened = ConversationStore(root).session_tasks
    assert reopened.pending_learning_page()["coverage"] == "partial"
    assert reopened.pending_learning_page()["coverage"] == "complete"
    with reopened._learning_index()._connection() as conn:
        assert conn.execute("SELECT count(*) FROM task_references").fetchone()[0] == 260
        assert conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 0
    assert _stamp(tasks._dir()) == directory
    assert list(tasks._dir().glob("*.lock")) == []


# LLM: 引用代次清理不能留下已删除任务，也不能把跨目录代次仍存在的历史引用误删。
# 函数用途: 验证新增、删除后全量引用与 owed 候选共同收敛。
def test_pending_reference_generation_tracks_addition_and_deletion(tmp_path):
    tasks = ConversationStore(tmp_path / "conversations").session_tasks
    for task_id in ("keep", "remove"):
        task = SessionTask(task_id, "a", "b", "method", status="done")
        tasks._path(task_id).write_text(json.dumps(task.to_dict()))
    assert tasks.pending_learning_page()["coverage"] == "complete"
    tasks._path("remove").unlink()
    task = SessionTask("new", "a", "b", "method", status="done",
                       metadata={"task_learning_enqueue": {"status": "pending"}})
    tasks._path("new").write_text(json.dumps(task.to_dict()))
    page = tasks.pending_learning_page()
    assert page["coverage"] == "complete"
    assert [task.task_id for task in page["items"]] == ["new"]
    with tasks._learning_index()._connection() as conn:
        assert {row[0] for row in conn.execute("SELECT path FROM task_references")} == {"keep.json", "new.json"}


# LLM: 两个真实写线程交错；先启动的绑定必须在终态提交后锁内重读，不得复活 queued 状态。
# 函数用途: 验证迟到 bind 与并发终态推进后的 canonical 状态及学习候选一致。
@pytest.mark.parametrize("terminal", ["cancelled", "failed", "done"])
def test_concurrent_late_bind_preserves_terminal_and_pending_membership(tmp_path, monkeypatch, terminal):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate

    root = tmp_path / "conversations"
    tasks = ConversationStore(root).session_tasks
    other = ConversationStore(root).session_tasks
    task = SessionTask("interleaved", "a", "b", "method", status="accepted",
                       metadata={"task_learning_enqueue": {"status": "ineligible",
                                 "reason": "canonical_completion_reader_unbound"}})
    tasks._path(task.task_id).write_text(json.dumps(task.to_dict()))
    tasks._learning_index().publish(tasks._path(task.task_id))
    binding_started, terminal_committed = Event(), Event()
    original = module.update_json_file_atomic

    # LLM: 暂停的是实际绑定写入入口；终态线程独立提交，拒绝用顺序调用伪装并发。
    # 函数用途: 确定性制造迟到绑定与终态提交交错。
    def delay_binding(path, updater, **kwargs):
        if current_thread().name.startswith("late-bind"):
            binding_started.set()
            assert terminal_committed.wait(10), "terminal writer did not finish"
        return original(path, updater, **kwargs)

    monkeypatch.setattr(module, "update_json_file_atomic", delay_binding)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="late-bind") as binder:
        late = binder.submit(tasks.bind_body, task.task_id, "body")
        try:
            assert binding_started.wait(10), "binding thread did not reach write"
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="terminal") as writer:
                ended = writer.submit(other.advance, task.task_id, SessionTaskUpdate(terminal))
                assert ended.result(timeout=10).status == terminal
            assert not late.done()
        finally:
            terminal_committed.set()
        assert late.result(timeout=10).status == terminal
    saved = other.require(task.task_id)
    assert saved.status == terminal and saved.body_guidance_id == "body"
    page = other.pending_learning_page()
    assert [item.task_id for item in page["items"]] == ([task.task_id] if terminal == "done" else [])


# LLM: canonical 提交与索引发布并非跨库事务；迟到发布只能读最新文件，不能按旧 owed 返回值复活候选。
# 函数用途: 用并发发布/结清交错验证原锁内重读及去重责任不回流。
def test_concurrent_late_owed_publish_cannot_restore_retired_candidate(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.session_task_pending_index import SessionTaskPendingIndex

    root = tmp_path / "conversations"
    tasks = ConversationStore(root).session_tasks
    other = ConversationStore(root).session_tasks
    task = SessionTask("publish-race", "a", "b", "method", status="done")
    tasks._path(task.task_id).write_text(json.dumps(task.to_dict()))
    host = SimpleNamespace(conversation_store=SimpleNamespace(session_tasks=tasks))
    other_host = SimpleNamespace(conversation_store=SimpleNamespace(session_tasks=other))
    publish_waiting, retired = Event(), Event()
    original = SessionTaskPendingIndex.publish

    # LLM: 延迟旧写者的发布而非持有索引锁，允许另一真实线程结清并发布。
    # 函数用途: 控制旧 owed 提交与最新结清提交的发布顺序。
    def delay_publish(index, path):
        if current_thread().name.startswith("late-owed"):
            publish_waiting.set()
            assert retired.wait(10), "retirement did not commit"
        return original(index, path)

    monkeypatch.setattr(SessionTaskPendingIndex, "publish", delay_publish)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="late-owed") as old_writer:
        late = old_writer.submit(_write_completion_metadata, host, task, lambda current: {
            **current.metadata, "task_learning_enqueue": {"status": "pending"}})
        try:
            assert publish_waiting.wait(10), "old publication did not pause"
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="retire") as new_writer:
                current = new_writer.submit(_write_completion_metadata, other_host, task, lambda latest: {
                    **latest.metadata, "task_learning_enqueue": {"status": "ineligible",
                        "reason": "legacy_request_material_missing"}})
                assert current.result(timeout=10).metadata["task_learning_enqueue"]["status"] == "ineligible"
            assert not late.done()
            assert other.pending_learning_page()["items"] == []
        finally:
            retired.set()
        assert late.result(timeout=10).metadata["task_learning_enqueue"]["status"] == "pending"
    assert other.require(task.task_id).metadata["task_learning_enqueue"]["status"] == "ineligible"
    assert tasks.pending_learning_page()["items"] == []
    with tasks._learning_index()._connection() as conn:
        assert conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM task_references").fetchone()[0] == 1


# LLM: 使用真实 SQL 结果计数，预算包括原分页为 next_cursor 多读的一行；分页仍须无重无漏。
# 函数用途: 防止十六条维护预算隐藏第十七条候选读取。
def test_pending_learning_sql_budget_includes_pagination_probe(tmp_path, monkeypatch):
    tasks = ConversationStore(tmp_path / "conversations").session_tasks
    expected = {f"owed-{n:03}" for n in range(32)}
    for n, task_id in enumerate(sorted(expected)):
        task = SessionTask(task_id, "source", "target", "method", status="done", created_at=n,
                           metadata={"task_learning_enqueue": {"status": "pending"}})
        tasks._path(task_id).write_text(json.dumps(task.to_dict()))
    assert tasks.pending_learning_page()["coverage"] == "complete"
    counts = Counter()
    _observe_pending_sql(monkeypatch, counts)
    cursor, seen = None, []
    for _ in range(5):
        counts.clear()
        page = tasks.pending_learning_page(limit=16, cursor=cursor)
        assert page["coverage"] == "complete" and not page["load_errors"]
        assert counts["candidate_rows"] <= 16, counts
        assert counts["reference_rows"] <= 2, counts
        assert 1 <= len(page["items"]) <= 16
        seen.extend(task.task_id for task in page["items"])
        assert len(seen) == len(set(seen))
        cursor = page["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("bounded learning traversal did not finish")
    assert set(seen) == expected
