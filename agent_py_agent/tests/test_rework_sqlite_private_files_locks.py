"""R4-P0-02：LocalStore 建连不得对已存在的库、-wal、-shm 开关 FD。

POSIX close() 会释放本进程在该 inode 上的全部 fcntl 锁，包括其它活连接持有的 SQLite WAL 写锁、读锁与 DMS 锁。
LocalStore 与 _connect() 都是生产实现；外部进程只用标准库 sqlite3（timeout=0）抢锁、读写和开关同一个库。
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys

import pytest

from agent_py_agent.agent.local_storage import LocalStore, schema

# 外部进程：write 以 BEGIN IMMEDIATE 写一行，read 返回全部行与完整性检查；锁冲突按 SQLite 原文回报，不等待。
_FOREIGN = """
import json, sqlite3, sys
db, action = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(db, timeout=0, isolation_level=None)
try:
    if action == "write":
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO items(value) VALUES (?)", (sys.argv[3],))
        conn.execute("COMMIT")
        result = {"ok": True}
    else:
        rows = [row[0] for row in conn.execute("SELECT value FROM items ORDER BY id")]
        result = {"ok": True, "rows": rows, "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0]}
except sqlite3.OperationalError as exc:
    result = {"ok": False, "error": str(exc)}
finally:
    conn.close()
print(json.dumps(result))
"""


# LLM: 只在测试临时目录建库；items 表由测试自建，不改 LocalStore 生产 schema。
# 函数用途: 创建关闭 FTS 的真实 LocalStore 并建好测试表。
def _store(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    conn = store._connect()
    try:
        conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
    finally:
        conn.close()
    return store


# LLM: 子进程以 -I 只加载标准库 sqlite3，不导入 my-agent 代码，也不读写 HOME。
# 函数用途: 在另一个真实进程里对同一库执行一次读或写，返回其 JSON 结果。
def _foreign(db, action, value=""):
    completed = subprocess.run([sys.executable, "-I", "-c", _FOREIGN, str(db), action, value],
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


# LLM: 路径拼法与生产 _private_sqlite_files 相同，只计算不访问文件。
# 函数用途: 返回库及其 WAL、SHM 三个路径。
def _files(db):
    return [db.with_name(db.name + suffix) for suffix in ("", "-wal", "-shm")]


# LLM: 持写事务期间再建一条 LocalStore 连接；外部进程必须仍拿不到写锁，提交后只看到持有者的行。
# 函数用途: 证明建连不释放活连接的 WAL 写锁，外部提交不能插进持有者的写事务。
def test_second_connect_keeps_the_live_write_lock(tmp_path):
    store = _store(tmp_path)
    holder = store._connect()
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO items(value) VALUES ('holder')")
        store._connect().close()
        assert _foreign(store.db_path, "write", "foreign") == {"ok": False, "error": "database is locked"}
        holder.commit()
        assert _foreign(store.db_path, "read") == {"ok": True, "rows": ["holder"], "integrity": "ok"}
    finally:
        holder.close()


# LLM: 活连接读过一次后另建连接；外部进程开读关不能自认最后一个连接而 checkpoint 并删除 sidecar。
# 函数用途: 证明活连接的共享锁与 DMS 锁仍在，两边随后的提交都不丢。
def test_foreign_open_close_keeps_the_live_wal_and_shm(tmp_path):
    store = _store(tmp_path)
    live = store._connect()
    try:
        assert live.execute("SELECT count(*) FROM items").fetchone()[0] == 0
        store._connect().close()
        assert _foreign(store.db_path, "read") == {"ok": True, "rows": [], "integrity": "ok"}
        assert [path.name for path in _files(store.db_path) if not path.exists()] == []
        live.execute("INSERT INTO items(value) VALUES ('live')")
        live.commit()
        assert _foreign(store.db_path, "write", "foreign") == {"ok": True}
    finally:
        live.close()
    assert _foreign(store.db_path, "read") == {"ok": True, "rows": ["live", "foreign"], "integrity": "ok"}


# LLM: umask 022 下新库与 sidecar 仍是 0600，旧 0644 库被收紧；出生临时名不残留，链接库路径不跟随也不建目标。
# 函数用途: 保住原私有权限合同，并确认新的出生方式只发表普通文件。
@pytest.mark.parametrize("link_target", ["existing", "dangling"])
def test_private_modes_birth_cleanup_and_symlink_rejection(tmp_path, link_target):
    previous = os.umask(0o022)
    try:
        store = LocalStore(tmp_path / "fresh" / "local.db", enable_fts=False)
        conn = store._connect()
        try:
            assert {path.name: stat.S_IMODE(os.lstat(path).st_mode) for path in _files(store.db_path)} == {
                "local.db": 0o600, "local.db-wal": 0o600, "local.db-shm": 0o600}
        finally:
            conn.close()
        legacy = tmp_path / "legacy" / "local.db"
        legacy.parent.mkdir()
        conn = sqlite3.connect(legacy)
        conn.execute("CREATE TABLE legacy(value TEXT)")
        conn.commit()
        conn.close()
        os.chmod(legacy, 0o644)
        LocalStore(legacy, enable_fts=False)
        assert stat.S_IMODE(os.lstat(legacy).st_mode) == 0o600
    finally:
        os.umask(previous)
    assert [path for path in tmp_path.rglob("*") if path.name.endswith(".birth")] == []
    target = legacy if link_target == "existing" else tmp_path / "missing.db"
    link = tmp_path / "linked" / "local.db"
    link.parent.mkdir()
    link.symlink_to(target)
    with pytest.raises(ValueError, match="SQLite storage must be a regular file"):
        LocalStore(link, enable_fts=False)
    assert os.path.lexists(target) == (link_target == "existing")


# LLM: 只记录 schema 模块所见的 os.open；SQLite 在 C 层自己的打开不经过这里，第二次建连不能碰活文件。
# 函数用途: 直接锁定根因：已存在的库、-wal、-shm 上不再由 Python 开关 FD。
def test_second_connect_opens_no_python_fd_on_live_files(tmp_path, monkeypatch):
    store = _store(tmp_path)
    first = store._connect()
    opened = []
    real_open = schema.os.open

    # LLM: 原样转交真实 os.open，只追加目标路径，不改变打开结果或异常。
    # 函数用途: 记录第二次建连期间 Python 层打开过的路径。
    def recording_open(path, flags, *args, **kwargs):
        opened.append(os.fspath(path))
        return real_open(path, flags, *args, **kwargs)

    try:
        assert all(path.exists() for path in _files(store.db_path))
        monkeypatch.setattr(schema.os, "open", recording_open)
        store._connect().close()
    finally:
        first.close()
    assert set(opened).isdisjoint(os.fspath(path) for path in _files(store.db_path))
