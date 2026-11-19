"""GW-03 / MAX1-C3：投影 SQLite 读头先试线程本地 FD 号提示，列表投影不再每次重跑建表。

不变量：
- 提示只是整数：fstat→pread→fstat 与全量扫描同一核验，从不 dup/关闭/持有 FD，不缓存头内容；
- 换 inode、原地损坏、0 字节后写入垃圾仍按 SQLITE_NOTADB 拒绝；提示失配回退原 /dev/fd 全量扫描；
- 恢复/替换前的“无本地句柄”检查始终全量扫描；建表记忆只按 (路径, dev, ino, kind) + schema_version 生效；
- C5：冷路径建表与 schema_version 读取同在一个写事务内，提交成功后才记忆，他处改表不能被记成已建好。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from contextlib import closing
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation import session_list_index
from agent_py_agent.agent.conversation.session_list_index import SessionListIndex
from agent_py_agent.agent.io import sqlite_probe
from agent_py_agent.agent.io.sqlite_scope import connect_projection_sqlite

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX /dev/fd 借读路径")


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    monkeypatch.setattr(sqlite_probe, "_HEADER_FD_HINTS", threading.local())
    monkeypatch.setattr(session_list_index, "_LIST_PROJECTION_DDL_MEMO",
                        type(session_list_index._LIST_PROJECTION_DDL_MEMO)())


# 函数用途: 统计 /dev/fd 全量扫描次数，其余目录照常枚举。
@pytest.fixture
def fd_scans(monkeypatch):
    calls = []
    real = os.scandir

    def scandir(path=".", *args, **kwargs):
        if os.fspath(path) == "/dev/fd":
            calls.append(1)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", scandir)
    return calls


def _database(path: Path) -> Path:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS t(v INTEGER)")
        conn.execute("INSERT INTO t VALUES(1)")
        conn.commit()
    return path


def _identity(path: Path) -> tuple[int, int]:
    info = os.lstat(path)
    return info.st_dev, info.st_ino


def _hints() -> dict:
    return getattr(sqlite_probe._HEADER_FD_HINTS, "descriptors", {})


def _connect_and_close(path: Path) -> None:
    conn = connect_projection_sqlite(path, timeout=1)
    try:
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] >= 1
    finally:
        conn.close()


def _assert_notadb(path: Path) -> None:
    with pytest.raises(sqlite3.DatabaseError) as caught:
        connect_projection_sqlite(path, timeout=1).close()
    assert getattr(caught.value, "sqlite_errorname", "") == "SQLITE_NOTADB"


def test_live_descriptor_hint_skips_dev_fd_scan(tmp_path, fd_scans):
    db = _database(tmp_path / "projection.sqlite3")
    with closing(sqlite3.connect(db)) as holder:
        holder.execute("SELECT count(*) FROM t").fetchone()
        _connect_and_close(db)
        assert len(fd_scans) == 1
        for _ in range(5):
            _connect_and_close(db)
        # 同 inode 上仍有活连接时 FD 号稳定：之后的连接都由提示核验命中，不再逐个 fstat /dev/fd。
        assert len(fd_scans) == 1
        assert _identity(db) in _hints()


def test_sequential_reconnects_reuse_hint_without_leaking_descriptors(tmp_path, fd_scans):
    db = _database(tmp_path / "projection.sqlite3")
    before = len(os.listdir("/dev/fd"))
    for _ in range(8):
        _connect_and_close(db)
    assert len(fd_scans) < 8
    assert len(os.listdir("/dev/fd")) == before


def test_stale_or_foreign_hint_falls_back_to_full_scan(tmp_path, fd_scans):
    db = _database(tmp_path / "projection.sqlite3")
    other = tmp_path / "other.bin"
    other.write_bytes(b"x" * 64)
    with open(other, "rb") as foreign, closing(sqlite3.connect(db)) as holder:
        holder.execute("SELECT 1").fetchone()
        for bogus in (foreign.fileno(), 1_000_000):
            sqlite_probe._HEADER_FD_HINTS.descriptors = {_identity(db): bogus}
            assert sqlite_probe._opened_sqlite_header(db)[:16] == b"SQLite format 3\0"
            hinted = _hints()[_identity(db)]
            assert hinted != bogus and os.fstat(hinted).st_ino == _identity(db)[1]
    assert len(fd_scans) == 2


def test_reused_descriptor_number_never_reads_another_file(tmp_path):
    db = _database(tmp_path / "projection.sqlite3")
    _connect_and_close(db)
    hinted = _hints()[_identity(db)]
    # 关闭后同一 FD 号被别的文件占用：提示核验发现身份不符，全量扫描也找不到库 FD，返回空而非错头。
    other = tmp_path / "other.bin"
    other.write_bytes(b"SQLite format 3\0" + b"\xff" * 64)
    descriptors = []
    try:
        while not descriptors or descriptors[-1] < hinted:
            descriptors.append(os.open(other, os.O_RDONLY))
        assert hinted in descriptors
        assert sqlite_probe._opened_sqlite_header(db) == b""
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def test_replaced_inode_with_invalid_header_still_raises_notadb(tmp_path):
    db = _database(tmp_path / "projection.sqlite3")
    _connect_and_close(db)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"not a sqlite database at all" * 64)
    os.replace(replacement, db)
    _assert_notadb(db)
    _assert_notadb(db)


def test_in_place_same_size_corruption_still_raises_notadb(tmp_path, fd_scans):
    db = _database(tmp_path / "projection.sqlite3")
    _connect_and_close(db)
    size = db.stat().st_size
    # 此时没有任何本进程连接持锁；直接改写头部模拟外部损坏（尺寸不变、inode 不变）。
    descriptor = os.open(db, os.O_WRONLY)
    try:
        os.pwrite(descriptor, b"X" * 16, 0)
    finally:
        os.close(descriptor)
    assert db.stat().st_size == size
    # 头内容从不缓存：同 inode 命中提示后读到的就是现盘损坏头。
    _assert_notadb(db)


def test_zero_byte_then_garbage_is_reverified(tmp_path):
    db = _database(tmp_path / "projection.sqlite3")
    _connect_and_close(db)
    for suffix in ("-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    os.truncate(db, 0)
    connect_projection_sqlite(db, timeout=1).close()
    db.write_bytes(b"garbage-header!!" + b"\0" * 4080)
    _assert_notadb(db)


def test_exclusive_handle_check_always_scans_every_descriptor(tmp_path, fd_scans):
    db = _database(tmp_path / "projection.sqlite3")
    with closing(sqlite3.connect(db)) as holder:
        holder.execute("SELECT 1").fetchone()
        sqlite_probe._opened_sqlite_header(db)
        scans = len(fd_scans)
        with pytest.raises(BlockingIOError):
            sqlite_probe._require_no_local_database_handles(db)
        assert len(fd_scans) == scans + 1
    # 连接关闭后提示仍在，但负证明照旧全量扫描并放行。
    assert _identity(db) in _hints()
    sqlite_probe._require_no_local_database_handles(db)
    assert len(fd_scans) == scans + 2


def test_hints_are_thread_local_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_probe, "_SQLITE_HEADER_FD_HINT_MAX_ENTRIES", 2)
    paths = [_database(tmp_path / f"p{index}.sqlite3") for index in range(4)]
    holders = [sqlite3.connect(path) for path in paths]
    try:
        for path in paths:
            assert sqlite_probe._opened_sqlite_header(path)[:16] == b"SQLite format 3\0"
        assert list(_hints()) == [_identity(path) for path in paths[-2:]]
        seen = {}

        def worker():
            seen["before"] = dict(getattr(sqlite_probe._HEADER_FD_HINTS, "descriptors", {}))
            seen["header"] = sqlite_probe._opened_sqlite_header(paths[0])[:16]

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert seen == {"before": {}, "header": b"SQLite format 3\0"}
        assert _identity(paths[0]) not in _hints()
    finally:
        for holder in holders:
            holder.close()


# ---- 列表投影建表记忆 ----

def _thread_index(root: Path) -> SessionListIndex:
    def read(path):
        try:
            return json.loads(path.read_text(encoding="utf-8")), None
        except (OSError, ValueError) as exc:
            return {}, {"error": str(exc)}

    return SessionListIndex(root, kind="thread", reader=read)


# 函数用途: 记录每条新投影连接执行的 SQL，供断言是否重跑建表。
@pytest.fixture
def projection_sql(monkeypatch):
    statements: list[list[str]] = []
    real = session_list_index.connect_projection_sqlite

    def connect(path, **kwargs):
        conn = real(path, **kwargs)
        current: list[str] = []
        statements.append(current)
        conn.set_trace_callback(current.append)
        return conn

    monkeypatch.setattr(session_list_index, "connect_projection_sqlite", connect)
    return statements


def _ddl(statements: list[str]) -> list[str]:
    return [sql for sql in statements if sql.lstrip().upper().startswith("CREATE")]


def test_list_projection_skips_repeat_ddl_but_keeps_wal_and_revision_seed(tmp_path, projection_sql):
    root = tmp_path / "threads"
    root.mkdir()
    record = root / "t1.json"
    record.write_text(json.dumps({"owner_id": "u1", "updated_at": 1}), encoding="utf-8")
    index = _thread_index(root)
    index.publish(record)
    index.publish(record)
    page = index.page(limit=10)
    assert [item["owner_id"] for item in page["items"]] == ["u1"]
    first, *later = projection_sql
    assert _ddl(first)
    assert later and all(not _ddl(opened) for opened in later)
    for opened in projection_sql:
        assert any("INSERT OR IGNORE INTO meta" in sql for sql in opened)
        assert any(sql.upper().startswith("PRAGMA JOURNAL_MODE=WAL") for sql in opened)


def test_list_projection_rebuilds_schema_after_replacement_or_external_drop(tmp_path, projection_sql):
    root = tmp_path / "threads"
    root.mkdir()
    record = root / "t1.json"
    record.write_text(json.dumps({"owner_id": "u1", "updated_at": 1}), encoding="utf-8")
    index = _thread_index(root)
    index.publish(record)
    with closing(sqlite3.connect(index.path)) as conn:
        conn.execute("DROP INDEX list_generation")
        conn.commit()
    index.publish(record)
    assert _ddl(projection_sql[-1])
    with closing(sqlite3.connect(index.path)) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "list_generation" in names
    for suffix in ("", "-wal", "-shm"):
        Path(f"{index.path}{suffix}").unlink(missing_ok=True)
    index.publish(record)
    assert _ddl(projection_sql[-1])
    assert index.page(limit=10)["items"][0]["owner_id"] == "u1"


def _drop_meta(path: Path) -> str:
    try:
        with closing(sqlite3.connect(path, timeout=0)) as other:
            other.execute("DROP TABLE meta")
            other.commit()
    except sqlite3.OperationalError as exc:
        return getattr(exc, "sqlite_errorname", str(exc))
    return "dropped"


def test_schema_change_between_ddl_and_cookie_read_is_never_memoized(tmp_path, monkeypatch):
    root = tmp_path / "threads"
    root.mkdir()
    record = root / "t1.json"
    record.write_text(json.dumps({"owner_id": "u1", "updated_at": 1}), encoding="utf-8")
    index = _thread_index(root)
    interloper, opened = [], []
    real = session_list_index.connect_projection_sqlite

    def connect(path, **kwargs):
        conn = real(path, **kwargs)
        current: list[str] = []
        opened.append(current)

        def trace(sql):
            current.append(sql)
            text = sql.lstrip().upper()
            # 另一条不持应用锁的真实连接恰在最后一条 CREATE 之后、读 schema_version 之前改表。
            if text.startswith("PRAGMA SCHEMA_VERSION") and _ddl(current) and not interloper:
                interloper.append(_drop_meta(Path(path)))

        conn.set_trace_callback(trace)
        return conn

    index.publish(record)
    # 新进程：库与 meta 已提交，建表记忆为空，下一次打开走冷路径。
    session_list_index._LIST_PROJECTION_DDL_MEMO.clear()
    monkeypatch.setattr(session_list_index, "connect_projection_sqlite", connect)
    index.publish(record)
    # 写事务持有期间他处改表只能 BUSY（timeout=0 即刻失败），不会落在建表与 cookie 读取之间。
    assert interloper == ["SQLITE_BUSY"]
    # 提交之后他处改表成功：schema_version 变化令下一次 page 重跑建表并重建；再下一次打开照常跳过建表。
    assert _drop_meta(index.path) == "dropped"
    assert [item["owner_id"] for item in index.page(limit=10)["items"]] == ["u1"]
    assert _ddl(opened[-1])
    assert [item["owner_id"] for item in index.page(limit=10)["items"]] == ["u1"]
    assert not _ddl(opened[-1])


def test_cold_open_failure_before_commit_records_no_ddl_memo(tmp_path):
    root = tmp_path / "threads"
    root.mkdir()
    record = root / "t1.json"
    record.write_text(json.dumps({"owner_id": "u1", "updated_at": 1}), encoding="utf-8")
    index = _thread_index(root)
    index.publish(record)
    session_list_index._LIST_PROJECTION_DDL_MEMO.clear()
    with closing(sqlite3.connect(index.path, timeout=0)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as caught:
            index._open_projection(timeout=0)
        assert getattr(caught.value, "sqlite_errorname", "") == "SQLITE_BUSY"
        assert not session_list_index._LIST_PROJECTION_DDL_MEMO
        writer.rollback()
    assert [item["owner_id"] for item in index.page(limit=10)["items"]] == ["u1"]
    assert len(session_list_index._LIST_PROJECTION_DDL_MEMO) == 1


def test_list_projection_ddl_memo_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(session_list_index, "_LIST_PROJECTION_DDL_MEMO_ENTRIES", 1)
    for name in ("a", "b", "c"):
        root = tmp_path / name
        root.mkdir()
        record = root / "t.json"
        record.write_text(json.dumps({"owner_id": name, "updated_at": 1}), encoding="utf-8")
        _thread_index(root).publish(record)
    assert len(session_list_index._LIST_PROJECTION_DDL_MEMO) <= 1
