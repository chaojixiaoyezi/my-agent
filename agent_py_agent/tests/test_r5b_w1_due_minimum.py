"""最早到期查询保留租约语义，旧库迁移与规模增长均使用真实 SQLite 验证。"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import closing

import pytest

from agent_py_agent.agent.scheduler import due_index
from agent_py_agent.agent.scheduler.due_index import SchedulerDueIndex


def _rows(count):
    for number in range(count):
        lease = 5000 + number if number % 2 == 0 else 0
        yield ("feishu", "user", f"u{number:07}", 1000 + number, lease, 50)


def _insert_rows(path, rows):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executemany("INSERT INTO scheduler_due_owners VALUES (?, ?, ?, ?, ?, ?)", rows)


def _measured_minimum(index, monkeypatch, owner=None):
    steps = 0
    original = index._connect

    def progress():
        nonlocal steps
        steps += 1
        return 0

    def connect():
        conn = original()
        # 只计量被测查询，不把连接准备和建索引的成本混入热查询。
        conn.set_progress_handler(progress, 1)
        return conn

    with monkeypatch.context() as patch:
        patch.setattr(index, "_connect", connect)
        result = index.next_due_at(owner)
    return result, steps


@pytest.mark.parametrize(("values", "expected"), [
    ((), None),
    (((100, 0),), 100),
    (((100, 150),), 150),
    (((100, 75),), 100),
    (((100, 0), (100, 0)), 100),
    (((100, 150), (120, 0)), 120),
], ids=["empty", "unleased", "future-lease", "expired-lease", "equal-deadlines", "mixed-owners"])
def test_minimum_preserves_global_and_owner_lease_semantics(tmp_path, values, expected):
    index = SchedulerDueIndex(tmp_path / "due.db")
    _insert_rows(index.path, (
        ("feishu", "user", f"u{number}", due, lease, 50)
        for number, (due, lease) in enumerate(values)
    ))
    assert index.next_due_at() == expected
    for number, (due, lease) in enumerate(values):
        owner = {"provider": "feishu", "kind": "user", "id": f"u{number}"}
        assert index.next_due_at(owner) == max(due, lease)
        assert index.next_due_at(("feishu", "user", f"u{number}")) == max(due, lease)
    assert index.next_due_at(("feishu", "user", "missing")) is None


@pytest.mark.parametrize("count", [10_000, 500_000])
def test_minimum_query_vm_steps_stay_bounded(tmp_path, monkeypatch, record_testsuite_property, count):
    index = SchedulerDueIndex(tmp_path / "due.db")
    _insert_rows(index.path, _rows(count))
    minimum, steps = _measured_minimum(index, monkeypatch)
    assert minimum == 1001, "最早一行被未来租约挡住，应返回下一位未被租约挡住的 owner"
    record_testsuite_property(f"minimum_vm_steps_{count}", steps)
    assert 0 < steps < 100
    minimum, steps = _measured_minimum(index, monkeypatch, ("feishu", "user", "u0000000"))
    assert minimum == 5000
    record_testsuite_property(f"owner_vm_steps_{count}", steps)
    assert 0 < steps < 100


@pytest.mark.parametrize("warm_cache", [False, True], ids=["cold", "cached-v1"])
def test_existing_v1_database_gains_the_minimum_index(tmp_path, monkeypatch, warm_cache):
    path = tmp_path / "legacy.db"
    # 固定升级前的真实 schema，不能用当前建表函数准备旧库。
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("""CREATE TABLE scheduler_due_owners (
            provider TEXT NOT NULL, owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL,
            next_due_at REAL NOT NULL, lease_until REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL, PRIMARY KEY (provider, owner_kind, owner_id))""")
        conn.execute("""CREATE TABLE scheduler_due_meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)""")
        conn.execute("""CREATE INDEX scheduler_due_owner_ready_idx
            ON scheduler_due_owners (next_due_at, lease_until)""")
        conn.execute("INSERT INTO scheduler_due_meta VALUES ('retained', 'complete', 50)")
        conn.execute("PRAGMA user_version=1")
    _insert_rows(path, _rows(10_000))
    if warm_cache:
        with monkeypatch.context() as patch:
            patch.setattr(due_index, "_SCHEMA_VERSION", 1)
            SchedulerDueIndex(path)
    reopened = SchedulerDueIndex(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == due_index._SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM scheduler_due_owners").fetchone()[0] == 10_000
        assert conn.execute("SELECT value FROM scheduler_due_meta WHERE key='retained'").fetchone()[0] == "complete"
    minimum, steps = _measured_minimum(reopened, monkeypatch)
    assert minimum == 1001
    assert 0 < steps < 100
    assert reopened.next_due_at(("feishu", "user", "u0000000")) == 5000


def test_lease_reclaim_stale_sync_and_cancellation_interleave_with_queries(tmp_path):
    index = SchedulerDueIndex(tmp_path / "due.db")
    writer = SchedulerDueIndex(index.path)
    owner = {"provider": "feishu", "kind": "user", "id": "alice"}

    def store(deadline):
        jobs = {"job-1": {"status": "active", "next_run_at": deadline}} if deadline else {}
        return {"owner": owner, "jobs": jobs, "runs": {}}

    index.sync_owner(owner, store(100), now=50)
    claimed, renewed, cancelled = (threading.Event() for _ in range(3))
    allow_renew, allow_cancel = threading.Event(), threading.Event()
    failures = []

    def mutate():
        try:
            assert [row.owner_id for row in writer.claim_due_owners(now=100, lease_seconds=10)] == ["alice"]
            claimed.set()
            assert allow_renew.wait(10)
            assert [row.owner_id for row in writer.claim_due_owners(now=110, lease_seconds=20)] == ["alice"]
            renewed.set()
            assert allow_cancel.wait(10)
            writer.sync_owner(owner, store(0), now=120)
        except BaseException as exc:
            failures.append(exc)
        finally:
            claimed.set()
            renewed.set()
            cancelled.set()

    thread = threading.Thread(target=mutate)
    thread.start()
    try:
        assert claimed.wait(10) and not failures
        assert index.next_due_at() == index.next_due_at(owner) == 110
        assert index.claim_due_owners(now=105) == []
        allow_renew.set()
        assert renewed.wait(10) and not failures
        assert index.next_due_at() == index.next_due_at(owner) == 130
        # 晚到的旧 owner 快照仍须经过原更新时间 CAS，不能把新租约和 deadline 写回旧值。
        index.sync_owner(owner, store(90), now=49)
        assert index.next_due_at() == index.next_due_at(owner) == 130
        assert index.claim_due_owners(now=115) == []
        allow_cancel.set()
        assert cancelled.wait(10) and not failures
        assert index.next_due_at() is None
        assert index.next_due_at(owner) is None
        assert index.claim_due_owners(now=200) == []
    finally:
        allow_renew.set()
        allow_cancel.set()
        thread.join(10)
    assert not thread.is_alive()
    assert failures == []
