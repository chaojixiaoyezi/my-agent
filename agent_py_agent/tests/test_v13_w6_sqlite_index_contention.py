"""真实SQLite短锁、WAL读者及按结构化错误码重试。"""
import sqlite3
import threading
import time
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.conversation import task_locator
from agent_py_agent.agent.conversation.session_list_index import SessionListIndex
from agent_py_agent.agent.user_space import home_indexes as indexes


def _busy(code=sqlite3.SQLITE_BUSY):
    error = sqlite3.OperationalError("SYNTHETIC_ONLY_ERROR")
    error.sqlite_errorcode = code
    error.sqlite_errorname = "SQLITE_BUSY" if code == sqlite3.SQLITE_BUSY else "SQLITE_ERROR"
    return error


def test_task_projection_waits_for_100ms_writer(tmp_path):
    with indexes.task_ref_projection(tmp_path) as (scope, _):
        path = scope.projection
    locked, release = threading.Event(), threading.Event()
    failures = []

    def holder():
        try:
            with sqlite3.connect(path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                locked.set()
                assert release.wait(5)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=holder)
    thread.start()
    assert locked.wait(5)
    timer = threading.Timer(0.1, release.set)
    timer.start()
    try:
        with indexes.task_ref_projection(tmp_path) as (_, conn):
            assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        release.set()
        thread.join(5)
        timer.join(5)
    assert not thread.is_alive() and not failures


@pytest.mark.parametrize("kind", ["task", "session"])
def test_wal_and_normal_allow_reader_during_write(tmp_path, kind):
    if kind == "task":
        context = indexes.task_ref_projection(tmp_path)
    else:
        root = tmp_path / "threads"
        root.mkdir()
        index = SessionListIndex(root, kind="thread", reader=lambda path: ({}, None))
        context = index._connection()
    with context as item:
        writer = item[1] if kind == "task" else item
        path = item[0].projection if kind == "task" else index.path
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert writer.execute("PRAGMA synchronous").fetchone()[0] == 1
    writer = sqlite3.connect(path)
    reader = sqlite3.connect(path, timeout=0.05)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        table = "locator_meta" if kind == "task" else "meta"
        writer.execute(f"INSERT OR REPLACE INTO {table} VALUES ('SYNTHETIC_ONLY_KEY','value')")
        assert reader.execute(f"SELECT value FROM {table} WHERE key='SYNTHETIC_ONLY_KEY'").fetchone() is None
    finally:
        writer.rollback()
        writer.close()
        reader.close()


@pytest.mark.parametrize("code,expected", [(sqlite3.SQLITE_BUSY, 3), (sqlite3.SQLITE_LOCKED, 3), (sqlite3.SQLITE_ERROR, 1)])
def test_lookup_retries_only_busy_codes(tmp_path, monkeypatch, code, expected):
    original = indexes.task_ref_projection
    calls, waits = [], []
    error = _busy(code)

    @contextmanager
    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise error
        with original(*args, **kwargs) as value:
            yield value

    monkeypatch.setattr(indexes, "task_ref_projection", flaky)
    monkeypatch.setattr(time, "sleep", waits.append)
    if code == sqlite3.SQLITE_ERROR:
        with pytest.raises(sqlite3.OperationalError) as caught:
            indexes.lookup_task_ref(tmp_path, "SYNTHETIC_ONLY_TASK")
        assert caught.value is error and not waits
    else:
        result = indexes.lookup_task_ref(tmp_path, "SYNTHETIC_ONLY_TASK")
        assert result["status"] == "missing"
        assert waits == [0.05, 0.1]
    assert len(calls) == expected


def test_locator_reports_busy_not_missing(tmp_path, monkeypatch):
    error = _busy()
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(task_locator, "lookup_task_ref", fail)
    result = task_locator.resolve_task_location(tmp_path, "SYNTHETIC_ONLY_TASK")
    assert result["status"] == "partial" and result["reason"] == "sqlite_busy"


def test_exhausted_lookup_reraises_original_and_name_fallback(tmp_path, monkeypatch):
    error = sqlite3.OperationalError("SYNTHETIC_ONLY_ERROR")
    error.sqlite_errorname = "SQLITE_LOCKED"
    calls = []
    @contextmanager
    def fail(*args, **kwargs):
        calls.append(1)
        raise error
        yield
    monkeypatch.setattr(indexes, "task_ref_projection", fail)
    with pytest.raises(sqlite3.OperationalError) as caught:
        indexes.lookup_task_ref(tmp_path, "SYNTHETIC_ONLY_TASK", busy_retries=2, backoff_seconds=0)
    assert caught.value is error and len(calls) == 3


def test_busy_projection_must_not_be_truncated(tmp_path, monkeypatch):
    root = tmp_path / "threads"
    root.mkdir()
    index = SessionListIndex(root, kind="thread", reader=lambda path: ({}, None))
    with index._connection():
        pass
    original = index.path.read_bytes()
    error = _busy()
    def fail():
        raise error
    monkeypatch.setattr(index, "_open_projection", fail)
    with pytest.raises(sqlite3.OperationalError) as caught:
        with index._connection():
            pass
    assert caught.value is error
    assert index.path.read_bytes() == original
