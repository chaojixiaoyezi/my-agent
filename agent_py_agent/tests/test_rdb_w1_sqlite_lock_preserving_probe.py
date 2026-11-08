"""用真实跨进程 WAL 读写验证只读探针不会释放本进程的 SQLite 锁。"""
from __future__ import annotations

import builtins
import io
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from agent_py_agent.agent.io.sqlite_probe import (
    _opened_sqlite_header,
    require_plain_sqlite_files,
    sqlite_readonly_probe_uri,
)
from agent_py_agent.agent.memory_archive.tool_output_retention import _attempt_states
from agent_py_agent.agent.owner_wake_discovery import _quiescent_probe_connection
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.session_lock.store import SessionLockStore
from agent_py_agent.agent.subagents.services.persistence.locator_hygiene import (
    projection_runtime_facts,
)
from agent_py_agent.agent.user_space.user_auth import _im_password

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="验证 POSIX 文件锁关闭语义")


@pytest.fixture
def owner_home(tmp_path):
    home = tmp_path / "owner"
    home.mkdir()
    db = home / "runtime.db"
    RuntimeRepository(db)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("INSERT INTO tasks(task_id,owner_id,created_at,updated_at) VALUES(?,?,?,?)",
                     ("task", "owner", 100, 101))
        conn.execute("INSERT INTO task_runs(task_run_id,task_id,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                     ("task-run", "task", "done", 100, 101))
        conn.execute("INSERT INTO agent_runs(agent_run_id,task_run_id,run_id,status,current_attempt_id,"
                     "current_attempt_generation,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     ("agent-run", "task-run", "child", "DONE", "attempt", 1, 100, 101))
        conn.execute("INSERT INTO agent_attempts(attempt_id,agent_run_id,attempt_generation,status,started_at,ended_at) "
                     "VALUES(?,?,?,?,?,?)", ("attempt", "agent-run", 1, "done", 100, 101))
        conn.execute("CREATE TABLE probe_writes(value INTEGER)")
        conn.commit()
    return home


def _password_database(home):
    db = home / "session_lock/private_chat_locks.db"
    SessionLockStore(db).set_password_hash("user", "stored-hash", now=100)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE probe_writes(value INTEGER)")
        conn.commit()
    return db


def _probe(home, kind):
    if kind == "projection":
        assert projection_runtime_facts(home, "child", {}) == ("DONE", False)
    elif kind == "quiescent":
        with _quiescent_probe_connection(home) as conn:
            assert conn is not None
            assert conn.execute("SELECT status FROM agent_attempts WHERE attempt_id='attempt'").fetchone()[0] == "done"
    elif kind == "attempts":
        assert _attempt_states(home, {"attempt"}) == {"attempt": ("done", 101.0)}
    elif kind == "header":
        assert _opened_sqlite_header(home / "runtime.db")[18:20] == b"\x02\x02"
    else:
        assert _im_password(home, "user") == "stored-hash"


def _subprocess_sql(db, code):
    return subprocess.run([sys.executable, "-c", code, str(db)], check=True, capture_output=True, text=True,
                          timeout=15).stdout.strip()


@pytest.mark.parametrize("probe", ["projection", "quiescent", "attempts", "password", "header"])
def test_probes_keep_wal_when_another_process_closes(owner_home, probe):
    db = _password_database(owner_home) if probe == "password" else owner_home / "runtime.db"
    wal = Path(str(db) + "-wal")
    with closing(sqlite3.connect(db)) as live:
        live.execute("INSERT INTO probe_writes VALUES(1)")
        live.commit()
        live.execute("SELECT * FROM probe_writes").fetchall()
        before = wal.stat().st_ino
        _probe(owner_home, probe)
        _subprocess_sql(db, "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
                        "c.execute('INSERT INTO probe_writes VALUES(2)'); c.commit(); c.close()")
        after = wal.stat().st_ino if wal.exists() else None
        live.execute("INSERT INTO probe_writes VALUES(3)")
        live.commit()
        parent_count = live.execute("SELECT count(*) FROM probe_writes").fetchone()[0]
        other_count = int(_subprocess_sql(db, "import sqlite3,sys; from pathlib import Path; "
                                         "c=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True); "
                                         "print(c.execute('SELECT count(*) FROM probe_writes').fetchone()[0]); c.close()"))
        assert other_count == parent_count == 3, {"wal_before": before, "wal_after": after,
                                                 "parent_count": parent_count, "other_count": other_count}
        assert after == before


@pytest.mark.parametrize("cold_first", [False, True], ids=["shared-wal", "cold-header"])
def test_probes_never_open_database_files_with_python_io(owner_home, monkeypatch, cold_first):
    _password_database(owner_home)
    opened = []

    def track(original):
        def wrapped(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)):
                name = os.path.basename(os.fsdecode(path))
                if name.startswith(("runtime.db", "private_chat_locks.db")):
                    opened.append(name)
            return original(path, *args, **kwargs)
        return wrapped

    supports_dir_fd = os.open in os.supports_dir_fd
    for module, name in ((builtins, "open"), (io, "open"), (os, "open"), (Path, "open")):
        monkeypatch.setattr(module, name, track(getattr(module, name)))
    if supports_dir_fd:
        # 包装只记录后转调原函数，必须保留其 dir_fd 能力声明。
        monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {os.open})
    probes = (("quiescent", "projection", "attempts", "password") if cold_first
              else ("projection", "attempts", "quiescent", "password"))
    for probe in probes:
        _probe(owner_home, probe)
    assert opened == []


def test_cold_database_probe_reads_facts(owner_home):
    db = owner_home / "runtime.db"
    wal = Path(str(db) + "-wal")
    assert not wal.exists()
    _probe(owner_home, "projection")
    _probe(owner_home, "attempts")
    # 最后一个可写连接正常退出，让静止探针也从没有旁文件的冷库开始。
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("SELECT count(*) FROM agent_runs").fetchone()
    assert not wal.exists()
    try:
        _probe(owner_home, "quiescent")
    except sqlite3.DatabaseError as exc:
        assert str(exc) == "runtime authority changed during quiescent probe"
    _probe(owner_home, "quiescent")


@pytest.mark.parametrize("condition,code,status", [
    ("corrupt", "STORE_UNAVAILABLE", 503),
    ("hardlink", "STORE_UNAVAILABLE", 503),
    ("symlink", "STORE_UNAVAILABLE", 503),
    ("directory", "STORE_UNAVAILABLE", 503),
    ("missing", "PASSWORD_NOT_CONFIGURED", 401),
])
def test_im_password_rejects_non_database_and_links(tmp_path, condition, code, status):
    root = tmp_path / "home"
    path = root / "session_lock/private_chat_locks.db"
    path.parent.mkdir(parents=True)
    if condition == "corrupt":
        path.write_bytes(b"not a sqlite database")
    elif condition in {"hardlink", "symlink"}:
        target = _password_database(tmp_path)
        if condition == "hardlink":
            os.link(target, path)
        else:
            path.symlink_to(target)
    elif condition == "directory":
        path.mkdir()
    with pytest.raises(HostAPIError) as caught:
        _im_password(root, "user")
    assert (caught.value.code, caught.value.status) == (code, status)


@pytest.mark.parametrize("component", ["db", "-wal", "-shm", "-journal"])
@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_plain_file_check_rejects_unsafe_database_and_sidecars(tmp_path, component, kind):
    db = tmp_path / "runtime.db"
    db.touch()
    path = db if component == "db" else Path(str(db) + component)
    path.unlink(missing_ok=True)
    if kind == "directory":
        path.mkdir()
    else:
        target = tmp_path / "target"
        target.touch()
        path.symlink_to(target)
    with pytest.raises(ValueError):
        require_plain_sqlite_files(db, beneath=tmp_path)


def test_plain_file_check_preserves_root_and_parent_boundaries(tmp_path):
    root = tmp_path / "home"
    parent = root / "data"
    parent.mkdir(parents=True)
    db = parent / "runtime.db"
    with pytest.raises(FileNotFoundError):
        require_plain_sqlite_files(db, beneath=root)
    db.touch()
    require_plain_sqlite_files(db, beneath=root)
    outside = tmp_path / "outside"
    parent.rename(outside)
    parent.symlink_to(outside, target_is_directory=True)
    for candidate in (db, outside / "runtime.db", root / ".." / "outside/runtime.db"):
        with pytest.raises(ValueError):
            require_plain_sqlite_files(candidate, beneath=root)
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        require_plain_sqlite_files(linked_root / "runtime.db", beneath=linked_root)


def test_readonly_probe_uri_handles_special_paths_and_refuses_writes(tmp_path):
    db = tmp_path / "只读 #?.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE sample(value)")
        conn.execute("INSERT INTO sample VALUES(1)")
        conn.commit()
    with closing(sqlite3.connect(sqlite_readonly_probe_uri(db), uri=True)) as conn:
        assert conn.execute("SELECT value FROM sample").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO sample VALUES(2)")


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_plain_file_check_preserves_single_link_requirement(tmp_path, suffix):
    db = tmp_path / "runtime.db"
    original = tmp_path / "original"
    original.touch()
    if suffix:
        db.touch()
    os.link(original, Path(str(db) + suffix))
    with pytest.raises(ValueError):
        require_plain_sqlite_files(db, beneath=tmp_path)


def test_plain_file_check_performs_only_lstat(owner_home, monkeypatch):
    db = owner_home / "runtime.db"
    expected_uri = db.absolute().as_uri() + "?mode=ro"

    def forbidden(*args, **kwargs):
        raise AssertionError("SQLite 路径预检只能 lstat，不能打开数据库或跟随链接")

    with monkeypatch.context() as guard:
        for module, name in ((builtins, "open"), (io, "open"), (os, "open"), (os, "stat"), (Path, "open")):
            guard.setattr(module, name, forbidden)
        assert require_plain_sqlite_files(db, beneath=owner_home) is None
        assert sqlite_readonly_probe_uri(db) == expected_uri


def test_attempt_states_preserve_missing_legacy_and_unavailable_results(owner_home, tmp_path):
    assert _attempt_states(tmp_path / "missing-owner", {"attempt"}) == {}
    legacy = owner_home / "data"
    legacy.mkdir()
    (owner_home / "runtime.db").rename(legacy / "runtime.db")
    assert _attempt_states(owner_home, {"attempt"}) == {"attempt": ("done", 101.0)}
    (owner_home / "runtime.db").write_bytes(b"not a SQLite database")
    assert _attempt_states(owner_home, {"attempt"}) is None


def test_active_wal_facts_are_not_replaced_by_an_immutable_snapshot(owner_home):
    with closing(sqlite3.connect(owner_home / "runtime.db")) as writer:
        writer.execute("UPDATE agent_runs SET status='running'")
        writer.execute("UPDATE agent_attempts SET status='running',ended_at=0")
        writer.commit()
        assert Path(str(owner_home / "runtime.db") + "-wal").exists()
        assert projection_runtime_facts(owner_home, "child", {}) == ("running", False)
        assert _attempt_states(owner_home, {"attempt"}) == {"attempt": ("running", 0.0)}
        with _quiescent_probe_connection(owner_home) as connection:
            assert connection.execute("SELECT status FROM agent_runs").fetchone()[0] == "running"
