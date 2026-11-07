"""#125: 精确 ack、可恢复迁移和有界 pending 的真实磁盘回归。"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation import ConversationStore


# LLM: 临时 owner 不接触日常 home；事件经实际追加入口生成。
# 函数用途: 返回带一个真实线程的 store。
def _store(root):
    store = ConversationStore(root)
    thread = store.threads.get_or_create({"canonical_user_id": "u", "channel": "internal",
        "channel_conversation_id": "c", "channel_user_id": "u", "now": 1})
    return store, thread.thread_id


# LLM: 旧格式使用长 ID 和缩进，确保 25k 案例确实跨过原 1MiB 上限。
# 函数用途: 写只读迁移输入，返回原字节。
def _legacy(store, count):
    raw = json.dumps({f"obs-{i:032x}": 10 for i in range(count)}, indent=2).encode()
    store.storage.observation_handled_path.write_bytes(raw)
    return raw


# LLM: 每轮维护仍只跑产品固定切片；上限防止游标丢失形成无限循环。
# 函数用途: 将显式迁移推进到 complete。
def _drain(index):
    for _ in range(2000):
        result = index.maintenance()
        assert not result["load_errors"], result
        if result["coverage"] == "complete":
            return
    pytest.fail("migration cursor did not converge")


# LLM: 固定 pending 数而增长已处理历史，ack 不能读取/重写 legacy 全图。
# 函数用途: 在 0/1/8 × 1k/25k 规模检查实际 ack 和 pending。
@pytest.mark.parametrize("history", [1000, 25000])
@pytest.mark.parametrize("pending", [0, 1, 8])
def test_ack_above_legacy_cap_and_pending_scale(tmp_path, history, pending):
    store, thread = _store(tmp_path / "owner")
    raw = _legacy(store, history)
    if history == 25000:
        assert len(raw) > 1024 * 1024
    events = [store.observations.append({"thread_id": thread, "requires_main_agent": True,
        "summary": str(i), "now": 20 + i}) for i in range(pending)]
    store.observations.mark_handled(["fresh-ack"], now=40)
    assert store.storage.observation_handled_path.read_bytes() == raw
    _drain(store.observations._index)
    for _ in range(4):
        assert [e.observation_id for e in store.observations.unhandled_requiring_main()] == [e.observation_id for e in events]
    store.observations.mark_handled([e.observation_id for e in events], now=50)
    assert ConversationStore(store.storage.root).observations.unhandled_requiring_main() == []


# LLM: 使用多个独立 store 和同步起跑，不能用 mock 替代文件锁和 SQLite 事务。
# 函数用途: 验证跨实例重复 ack 首次时间不变、崩溃重放不重复生效。
def test_concurrent_duplicate_ack_is_idempotent(tmp_path):
    store, thread = _store(tmp_path / "owner")
    event = store.observations.append({"thread_id": thread, "requires_main_agent": True})
    barrier = threading.Barrier(8)

    # LLM: 各写者共享真实路径但不共享 Python store 锁。
    # 函数用途: 同时提交同一 ID。
    def ack(i):
        other = ConversationStore(store.storage.root)
        barrier.wait(timeout=10)
        other.observations.mark_handled([event.observation_id], now=100 + i)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(ack, range(8)))
    index = store.observations._index
    first = index.handled([event.observation_id])[event.observation_id]
    before = index.journal_path.read_bytes()
    store.observations.mark_handled([event.observation_id], now=999)
    assert index.handled([event.observation_id])[event.observation_id] == first
    assert index.journal_path.read_bytes() == before
    assert len(before.splitlines()) == 1
    assert store.observations.unhandled_requiring_main() == []


# LLM: 未读到 legacy 尾部前必须显式 partial，不能将未知 ID 解释为未处理。
# 函数用途: 验证固定字节切片、重启游标和只读 legacy。
def test_legacy_resume_never_resurfaces_unmigrated_ack(tmp_path):
    from agent_py_agent.agent.conversation.observation_index import ObservationIndexIncomplete
    store, thread = _store(tmp_path / "owner")
    raw = _legacy(store, 25000)
    store.observations.append({"thread_id": thread, "_observation_id": f"obs-{24999:032x}",
        "requires_main_agent": True})
    with pytest.raises(ObservationIndexIncomplete):
        store.observations.unhandled_requiring_main()
    first = store.observations._index.maintenance()
    assert first["coverage"] == "partial"
    assert first["legacy_offset"] > 0
    assert first["bytes_read"] <= 131072
    restarted = ConversationStore(store.storage.root)
    second = restarted.observations._index.maintenance()
    assert second["legacy_offset"] > first["legacy_offset"]
    _drain(restarted.observations._index)
    assert restarted.observations.unhandled_requiring_main() == []
    assert store.storage.observation_handled_path.read_bytes() == raw


# LLM: 索引是可重建投影，独立 append-only ack journal 才是新确认事实源。
# 函数用途: 验证投影损坏恢复不丢 ack，legacy 错误仍明确暴露。
def test_corruption_rebuild_and_legacy_error(tmp_path):
    from agent_py_agent.agent.conversation.observation_index import ObservationIndexError
    store, thread = _store(tmp_path / "owner")
    event = store.observations.append({"thread_id": thread, "requires_main_agent": True})
    store.observations.mark_handled([event.observation_id], now=10)
    index = store.observations._index
    index.path.write_bytes(b"not sqlite")
    assert ConversationStore(store.storage.root).observations.unhandled_requiring_main() == []
    assert index.journal_path.exists()
    legacy, thread = _store(tmp_path / "legacy")
    legacy.observations.append({"thread_id": thread, "requires_main_agent": True})
    legacy.storage.observation_handled_path.write_text("{broken")
    with pytest.raises(ObservationIndexError):
        legacy.observations.unhandled_requiring_main()
    legacy.storage.observation_handled_path.write_text("{}")
    assert len(legacy.observations.unhandled_requiring_main()) == 1


# LLM: 测试语法门真正拒绝反例，不仅检查文件或符号存在。
# 函数用途: 防止热 ack 再次引入整图读写。
def test_ack_ast_guard():
    import inspect
    import textwrap

    from agent_py_agent.agent.conversation.store_observations import ObservationStore

    # LLM: 只拦截已知整账本调用，不限制实现内部结构。
    # 函数用途: 返回函数内是否有整图热路调用。
    def unsafe(source):
        return any(isinstance(node, ast.Call) and
            ((isinstance(node.func, ast.Attribute) and node.func.attr in {"_read_handled", "events_report", "read_delivered"}) or
             (isinstance(node.func, ast.Name) and node.func.id == "update_json_file_atomic"))
            for node in ast.walk(ast.parse(textwrap.dedent(source))))

    assert unsafe("def bad(self):\n self._read_handled()")
    assert not unsafe(inspect.getsource(ObservationStore.mark_handled))


# LLM: 真正增长观察 JSONL 和已处理 ID 两条历史轴；计数器只包真实 IO，不替代结果。
# 函数用途: 证明常规 ack/pending 的读取字节、解析和 FD 不随历史增长。
@pytest.mark.parametrize("history", [1000, 25000])
@pytest.mark.parametrize("pending", [0, 1, 8])
def test_real_history_io_and_fd_are_bounded(tmp_path, monkeypatch, history, pending):
    from agent_py_agent.agent.conversation import observation_index as module
    store, thread = _store(tmp_path / "owner")
    _legacy(store, history)
    path = store.storage.observation_path(thread)
    with path.open("w") as handle:
        for i in range(history):
            handle.write(json.dumps({"observation_id": f"obs-{i:032x}", "thread_id": thread,
                "requires_main_agent": True, "observed_at": i, "summary": "history"}) + "\n")
    events = [store.observations.append({"thread_id": thread, "requires_main_agent": True,
        "summary": "pending", "now": history + i}) for i in range(pending)]
    _drain(store.observations._index)
    counts = {"read_bytes": 0, "written_bytes": 0, "json_loads": 0}
    original_reader, original_write, original_loads = module._reader, os.write, json.loads

    # LLM: 包装器不拥有 FD，原 reader 的 finally 始终负责关闭。
    # 类用途: 记录真实 read/readline 返回字节数。
    class Counted:
        # LLM: 只保存当前 with 范围借入的文件。
        # 函数用途: 初始化计数字节代理。
        def __init__(self, handle):
            self.handle = handle

        # LLM: seek/fileno 等全部转发，不制造内存账本。
        # 函数用途: 透传不涉及计数的文件操作。
        def __getattr__(self, name):
            return getattr(self.handle, name)

        # LLM: 原 read 的返回值与异常不改动。
        # 函数用途: 记录实际读取量。
        def read(self, *args):
            raw = self.handle.read(*args)
            counts["read_bytes"] += len(raw)
            return raw

        # LLM: 原 readline 仍受产品字节参数限制。
        # 函数用途: 记录实际逐行读取量。
        def readline(self, *args):
            raw = self.handle.readline(*args)
            counts["read_bytes"] += len(raw)
            return raw

    # LLM: 原 contextmanager 的异常和清理都继续运行。
    # 函数用途: 包装一个真实来源读取。
    @contextmanager
    def reader(target):
        with original_reader(target) as handle:
            yield Counted(handle)

    # LLM: 只在真实 write 成功后累计其已写字节。
    # 函数用途: 记录 ack journal 的实际追加量。
    def write(fd, raw):
        size = original_write(fd, raw)
        counts["written_bytes"] += size
        return size

    # LLM: JSON 原解析与异常均保留。
    # 函数用途: 记录真实解析次数。
    def loads(*args, **kwargs):
        counts["json_loads"] += 1
        return original_loads(*args, **kwargs)

    monkeypatch.setattr(module, "_reader", reader)
    monkeypatch.setattr(os, "write", write)
    monkeypatch.setattr(json, "loads", loads)
    fd_root = "/dev/fd" if Path("/dev/fd").exists() else "/proc/self/fd"
    before = len(os.listdir(fd_root))
    for i in range(10):
        counts.update(dict.fromkeys(counts, 0))
        store.observations.mark_handled([f"new-{i}"], now=100)
        assert counts["read_bytes"] == 0
        assert counts["written_bytes"] <= 128
        assert counts["json_loads"] <= 4
        last_ack = dict(counts)
        counts.update(dict.fromkeys(counts, 0))
        assert [e.observation_id for e in store.observations.unhandled_requiring_main(limit=0)] == [e.observation_id for e in events]
        assert counts["read_bytes"] <= pending * 1024
        assert counts["written_bytes"] == 0
        assert counts["json_loads"] <= pending + 4
        assert store.observations._index.last_work["candidate_rows"] <= pending
        assert len(os.listdir(fd_root)) == before
    print(json.dumps({"history": history, "pending": pending, "last_ack_io": last_ack,
        "last_pending_io": counts, "fd_delta": len(os.listdir(fd_root)) - before}))


# LLM: 在日志已发布、派生事务未提交的准确窗口失败，重启只能重放确认，不能重复追加。
# 函数用途: 验证 ack 两阶段恢复和 journal 损坏的 fail-closed 行为。
def test_ack_crash_replay_and_corrupt_journal(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.observation_index import ObservationIndexError
    store, thread = _store(tmp_path / "owner")
    event = store.observations.append({"thread_id": thread, "requires_main_agent": True})
    index = store.observations._index
    original = index._install

    # LLM: 仅中断本次非空安装，磁盘 journal 已真实写入并 fsync。
    # 函数用途: 模拟派生事务提交前的进程中断窗口。
    def interrupted(conn, pairs):
        if pairs:
            raise OSError("simulated I/O error after journal publication")
        return original(conn, pairs)

    with monkeypatch.context() as patch:
        patch.setattr(index, "_install", interrupted)
        with pytest.raises(OSError):
            store.observations.mark_handled([event.observation_id], now=10)
    raw = index.journal_path.read_bytes()
    restarted = ConversationStore(store.storage.root)
    restarted.observations.mark_handled([event.observation_id], now=20)
    assert index.journal_path.read_bytes() == raw
    assert restarted.observations.recent(thread)[0].handled_at == 10
    with index.journal_path.open("ab") as handle:
        handle.write(b"{broken\n")
    with pytest.raises(ObservationIndexError):
        restarted.observations.unhandled_requiring_main()
    assert index.journal_path.read_bytes().endswith(b"{broken\n")


# LLM: busy 必须传播，不能截断投影；私有文件和链接检查沿真实内核路径执行。
# 函数用途: 验证 SQLite 约定和投影入口的安全边界。
def test_private_projection_busy_and_link_rejection(tmp_path):
    store, _ = _store(tmp_path / "owner")
    store.observations.mark_handled(["one"], now=1)
    index = store.observations._index
    assert index.path.stat().st_mode & 0o777 == 0o600
    assert index.journal_path.stat().st_mode & 0o777 == 0o600
    with index._connection() as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        plans = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN SELECT * FROM pending WHERE needs_main=1 AND status='pending' ORDER BY due,id LIMIT 20")]
        assert any("pending_all" in plan for plan in plans)
        assert not any("TEMP B-TREE" in plan or "SCAN pending" in plan for plan in plans)
    with sqlite3.connect(index.path) as held:
        held.execute("BEGIN IMMEDIATE")
        before = index.path.read_bytes()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            store.observations.mark_handled(["two"], now=2)
        assert index.path.read_bytes() == before
    alias = tmp_path / "alias"
    os.link(index.path, alias)
    with pytest.raises(ValueError, match="regular file"):
        store.observations.mark_handled(["three"], now=3)


# LLM: limit=0 的真实调用方必须跨过内部 128 页边界，只枚举活跃项；唤醒链接包括普通观察。
# 函数用途: 验证旧无限返回合同和 wake 关联在索引热路保持。
def test_active_keyset_and_wake_association(tmp_path):
    store, thread = _store(tmp_path / "owner")
    path = store.storage.observation_path(thread)
    with path.open("w") as handle:
        for i in range(140):
            handle.write(json.dumps({"observation_id": f"active-{i:03d}", "thread_id": thread,
                "requires_main_agent": True, "observed_at": i, "wake_signal_id": "wake-one",
                "summary": "active" * 100}) + "\n")
        handle.write(json.dumps({"observation_id": "plain", "thread_id": thread,
            "requires_main_agent": False, "wake_signal_id": "wake-one", "observed_at": 141}) + "\n")
    _drain(store.observations._index)
    assert len(store.observations.unhandled_requiring_main()) == 20
    assert len(store.observations.unhandled_requiring_main(limit=0)) == 140
    assert store.observations.mark_handled_for_wake("unknown", now=9) == []
    linked = store.observations.mark_handled_for_wake("wake-one", now=10)
    assert set(linked) == {*(f"active-{i:03d}" for i in range(140)), "plain"}
    assert store.observations.mark_handled_for_wake("wake-one", now=20) == []
    assert ConversationStore(store.storage.root).observations.unhandled_requiring_main(limit=0) == []


# LLM: 真实分片删除使目录代次变化，未读来源仍须被清理；不能让事务回滚锁住探测游标。
# 函数用途: 验证冷回填删除分片后可在有限轮次恢复。
def test_deleted_unread_source_does_not_stall_backfill(tmp_path):
    store, thread = _store(tmp_path / "owner")
    directory = store.storage.observations_dir
    for n in range(40):
        (directory / f"part-{n:03}.jsonl").write_text(json.dumps({
            "observation_id": f"part-{n:03}", "thread_id": thread,
            "requires_main_agent": True, "observed_at": n}) + "\n")
    index = store.observations._index
    assert index.maintenance()["coverage"] == "partial"
    with index._connection() as conn:
        unread = conn.execute("SELECT path FROM sources WHERE complete=0 ORDER BY path").fetchall()
    deleted = unread[0][0]
    (directory / deleted).unlink()
    for _ in range(15):
        report = index.maintenance()
        assert not report["load_errors"], report
        if report["coverage"] == "complete":
            break
    else:
        pytest.fail("deleted source prevented bounded convergence")
    assert {entry.observation_id for entry in store.observations.unhandled_requiring_main(limit=0)} == {
        f"part-{n:03}" for n in range(40)} - {Path(deleted).stem}


# LLM: summary超过单轮64KiB但低于原读取上限仍是合法事实，跨轮恢复不能丢行或重复返回。
# 函数用途: 验证大记录分片读取、重启续接以及wake确认仍可完成。
def test_large_legal_observation_resumes_with_fixed_read_budget(tmp_path):
    from agent_py_agent.agent.conversation.observation_index import ObservationIndexIncomplete

    store, thread = _store(tmp_path / "owner")
    event, wake = store.wakes.append_observation(
        {"thread_id": thread, "requires_main_agent": True, "summary": "x" * 80000},
        {"thread_id": thread, "reason": "large_record", "dedupe_key": "large-wake"})
    ordinary = store.observations.append({"thread_id": thread, "requires_main_agent": True,
        "summary": "ordinary"})
    for _ in range(5):
        try:
            pending = store.observations.unhandled_requiring_main(limit=0)
        except ObservationIndexIncomplete:
            assert store.observations._index.last_work["bytes_read"] <= 131072
            store = ConversationStore(store.storage.root)
            continue
        break
    else:
        pytest.fail("legal record never completed bounded backfill")
    assert {entry.observation_id for entry in pending} == {event.observation_id, ordinary.observation_id}
    assert next(entry for entry in pending if entry.observation_id == event.observation_id).summary == "x" * 80000
    assert event.wake_signal_id == wake.wake_signal_id
    assert store.observations.mark_handled_for_wake(wake.wake_signal_id, now=10) == [event.observation_id]
    assert [entry.observation_id for entry in store.observations.unhandled_requiring_main(limit=0)] == [ordinary.observation_id]


@pytest.mark.parametrize("domain", ["observations", "guidance"])
def test_large_ack_journal_resumes_at_complete_record_boundaries(tmp_path, domain):
    """两类确认日志共用恢复器，跨 64KiB 切片并重建实例仍须逐条恢复。"""
    from agent_py_agent.agent.conversation.guidance_pending_index import GuidanceIndexIncomplete
    from agent_py_agent.agent.conversation.observation_index import ObservationIndexIncomplete

    store, _ = _store(tmp_path / "owner")

    def index_for(current):
        return (current.observations._index if domain == "observations"
                else current.guidance.ledger.pending_index())

    index = index_for(store)
    pairs = [(f"ack-{number:03}-" + "x" * 900, 10.0) for number in range(180)]
    end, _written = index._append(pairs)
    raw = index.journal_path.read_bytes()
    assert end == len(raw) > 128 * 1024
    ids = [key for key, _at in pairs]
    offsets, read_bytes = [], []
    for _ in range(8):
        index = index_for(ConversationStore(store.storage.root))
        try:
            found = (index.handled(ids, include_legacy=False) if domain == "observations"
                     else index.delivered_many(ids))
        except (ObservationIndexIncomplete, GuidanceIndexIncomplete):
            found = None
        read_bytes.append(index.last_work["bytes_read"])
        assert 0 < read_bytes[-1] <= 128 * 1024
        assert index.last_work["json_decodes"] <= 128
        with sqlite3.connect(index.path) as conn:
            offsets.append(int(conn.execute("SELECT value FROM meta WHERE key='journal_offset'").fetchone()[0]))
        assert raw[:offsets[-1]].endswith(b"\n")
        if found is not None:
            assert found == dict(pairs)
            break
    else:
        pytest.fail("large ack journal did not converge")
    assert len(offsets) >= 3 and offsets == sorted(set(offsets)) and offsets[-1] == end
    assert sum(read_bytes) == end
    assert index.journal_path.read_bytes() == raw


@pytest.mark.parametrize("mutation", ["append", "chmod"])
def test_observation_reader_accepts_concurrent_append_and_permission_refresh(tmp_path, monkeypatch, mutation):
    """用事件固定 lstat/open 交错，正文和权限变更均由真实写线程完成。"""
    from agent_py_agent.agent.conversation import observation_index as module

    path = tmp_path / "journal.jsonl"
    path.write_bytes(b"original\n")
    sampled, written = threading.Event(), threading.Event()
    original_identity = module._identity
    errors = []

    def writer():
        try:
            assert sampled.wait(5)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND)
            try:
                os.fchmod(fd, 0o600)
                if mutation == "append":
                    os.write(fd, b"appended\n")
            finally:
                os.close(fd)
        except BaseException as exc:
            errors.append(exc)
        finally:
            written.set()

    def identity(target):
        expected = original_identity(target)
        if target == path:
            sampled.set()
            assert written.wait(5)
        return expected

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module, "_identity", identity)
            with module._reader(path) as handle:
                assert handle.read() == b"original\n" + (b"appended\n" if mutation == "append" else b"")
    finally:
        sampled.set()
        thread.join(5)
    assert not thread.is_alive() and errors == []


@pytest.mark.parametrize("mutation", ["truncate", "replace", "hardlink"])
def test_observation_reader_still_rejects_unsafe_open_races(tmp_path, monkeypatch, mutation):
    from agent_py_agent.agent.conversation import observation_index as module

    path = tmp_path / "journal.jsonl"
    path.write_bytes(b"original\n")
    original_identity = module._identity

    def identity(target):
        expected = original_identity(target)
        if mutation == "truncate":
            path.write_bytes(b"x")
        elif mutation == "replace":
            replacement = tmp_path / "replacement"
            replacement.write_bytes(b"replacement\n")
            replacement.replace(path)
        else:
            os.link(path, tmp_path / "alias")
        return expected

    monkeypatch.setattr(module, "_identity", identity)
    with pytest.raises(module.ObservationIndexError, match="changed while opening"):
        with module._reader(path):
            pytest.fail("unsafe source was accepted")


def test_ack_sync_checkpoints_opened_inode_after_concurrent_append(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import observation_index as module

    store, _ = _store(tmp_path / "owner")
    index = store.observations._index
    index._append([("first", 10.0)])
    original_open = os.open
    appended = []

    def append_before_open(path, flags, *args, **kwargs):
        if path == index.journal_path and flags & os.O_ACCMODE == os.O_RDONLY and not appended:
            fd = original_open(path, os.O_WRONLY | os.O_APPEND)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, b'{"id":"second","at":20}\n')
                appended.append(True)
            finally:
                os.close(fd)
        return original_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "open", append_before_open)
        assert index.handled(["first", "second"], include_legacy=False) == {"first": 10, "second": 20}
    assert appended == [True]
    # 同长权限刷新只改变 ctime，下一次查询仍须认可刚提交的精确确认。
    os.chmod(index.journal_path, 0o600)
    assert index.handled(["first", "second"], include_legacy=False) == {"first": 10, "second": 20}
    with index._connection() as conn:
        assert index._journal_current(conn)


def test_ack_sync_retries_concurrently_completed_tail_from_line_start(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import observation_index as module

    store, _ = _store(tmp_path / "owner")
    index = store.observations._index
    index._append([("first", 10.0)])
    first_end = index.journal_path.stat().st_size
    original_reader = module._reader

    class AppendedTail:
        def __init__(self, handle):
            self.handle = handle
            self.calls = 0

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def readline(self, size):
            self.calls += 1
            if self.calls != 2:
                return self.handle.readline(size)
            with index.journal_path.open("ab") as writer:
                writer.write(b'{"id":"second",')
            raw = self.handle.readline(size)
            with index.journal_path.open("ab") as writer:
                writer.write(b'"at":20}\n')
            return raw

    @contextmanager
    def reader(path):
        with original_reader(path) as handle:
            yield AppendedTail(handle)

    with monkeypatch.context() as patch:
        patch.setattr(module, "_reader", reader)
        with pytest.raises(module.ObservationIndexIncomplete):
            index.handled(["first", "second"], include_legacy=False)
    with index._connection() as conn:
        assert int(index._get(conn, "journal_offset")) == first_end
        assert index._lookup(conn, ["first"]) == {"first": 10}
    assert index.handled(["first", "second"], include_legacy=False) == {"first": 10, "second": 20}


@pytest.mark.parametrize("raw", [b'{"id":"one","at":1}', b"x" * 65537, b"{broken\n"],
                         ids=["incomplete", "oversized", "invalid-json"])
def test_ack_sync_still_rejects_malformed_records(tmp_path, raw):
    from agent_py_agent.agent.conversation.observation_index import ObservationIndexError

    store, _ = _store(tmp_path / "owner")
    index = store.observations._index
    index.journal_path.write_bytes(raw)
    with pytest.raises(ObservationIndexError) as error:
        index.handled(["one"], include_legacy=False)
    assert type(error.value) is ObservationIndexError
    assert index.journal_path.read_bytes() == raw


@pytest.mark.parametrize("count", [129, 658])
def test_observation_directory_changes_do_not_restart_small_discovery_slices(tmp_path, count):
    store, thread = _store(tmp_path / "owner")
    directory = store.storage.observations_dir
    for number in range(count):
        (directory / f"history-{number:04}.jsonl").touch()
    _drain(store.observations._index)
    expected = []
    for number in range(6):
        (directory / f"history-{number:04}.jsonl").unlink()
        path = directory / f"fresh-{number}.jsonl"
        event_id = f"fresh-{number}"
        path.write_text(json.dumps({"observation_id": event_id, "thread_id": thread,
            "requires_main_agent": True, "observed_at": number + 1}) + "\n")
        expected.append(event_id)
        pending = store.observations.unhandled_requiring_main(limit=0)
        assert [event.observation_id for event in pending] == expected
