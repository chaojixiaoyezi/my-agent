"""W6：真实事件写入、可恢复追赶、owner 轮转与空闲零提交。"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import replace

import pytest

from agent_py_agent.agent.local_storage import LocalStore, maintenance
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.storage_lanes import (
    StorageLaneBudget,
    StorageLaneRequest,
    validate_lane_receipt,
)


def _request(home, *, cursor=None, coordinator=False, commit=None, legal_hold=False, budget=None):
    return StorageLaneRequest(
        kind="local_events", home=home, now=time.time() + 60, cursor=cursor or {},
        budget=budget or StorageLaneBudget(None, None, None), legal_hold=legal_hold,
        coordinator=coordinator, pressure=None, commit=commit,
    )


def _store(home, scope="a"):
    return LocalStore(home.owner_workspace_dir / "runtime" / "workspaces" / scope / "local_store" / "local.db")


def _lane(request):
    function = getattr(maintenance, "maintain_local_events_lane", None)
    assert callable(function), "W6 must provide the real local-events lane"
    return validate_lane_receipt(function(request))


def _db_ids(store):
    with sqlite3.connect(store.db_path) as conn:
        return [row[0] for row in conn.execute("SELECT event_id FROM events ORDER BY seq")]


def _jsonl_ids(store):
    return [json.loads(line)["event_id"] for line in store.events_path.read_bytes().splitlines()]


# 沿用 R1 的公开追加方式，不跨测试文件导入，也不绕过 SQL/JSONL writer。
def _append(store, appended, tag):
    writer = LocalStore(store.db_path, enable_fts=store.enable_fts)
    appended.append(writer.record_event(tag, payload={"tag": tag}).event_id)


def test_fresh_store_is_incremental_and_existing_none_is_unchanged(tmp_path):
    fresh = LocalStore(tmp_path / "fresh" / "local.db")
    with fresh._connection() as conn:
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    legacy = tmp_path / "legacy.db"
    with sqlite3.connect(legacy) as conn:
        conn.execute("CREATE TABLE preserved(value TEXT)")
        conn.execute("INSERT INTO preserved VALUES ('keep')")
    store = LocalStore(legacy)
    with store._connection() as conn:
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
        assert conn.execute("SELECT value FROM preserved").fetchone()[0] == "keep"


def test_catchup_real_public_events_and_jsonl_converge(tmp_path, monkeypatch):
    assert callable(getattr(maintenance, "maintain_local_events_lane", None))
    monkeypatch.setattr(maintenance, "STEADY_STATE_EVENTS_KEEP_LAST", 200)
    monkeypatch.setattr(maintenance, "LOCAL_STORE_CATCHUP_BATCH_ROWS", 500)
    home = home_paths(tmp_path / "home")
    store = _store(home)
    appended = []
    # 单连接批次只减少连接开销，每条仍走公开 record_event 的真实 SQL/JSONL 提交。
    with store.connection_batch():
        for index in range(12_200):
            appended.append(store.record_event("seed", payload={"index": index}).event_id)
    reports, cursor = [], {}
    for _ in range(128):
        report = _lane(_request(home, cursor=cursor))
        reports.append(report)
        cursor = report["cursor"]
        if report["complete"]:
            break
    else:
        pytest.fail("catch-up never finished within 128 bounded slices")
    assert reports[0]["complete"] is False
    assert all(report["complete"] is False for report in reports[:-1])
    assert sum(report.get("deleted_items", 0) for report in reports) == 12_000
    assert _db_ids(store) == appended[-200:]
    assert _jsonl_ids(store) == appended[-200:]
    with store._connection() as conn:
        lo, hi = conn.execute("SELECT MIN(seq),MAX(seq) FROM events").fetchone()
        assert hi - lo + 1 == 200
        assert conn.execute("SELECT 1 FROM metadata WHERE key='events_prune_cursor'").fetchone() is None
    assert reports[-1]["backlog"] == 0


def test_legal_hold_makes_no_commits_or_deletions(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "STEADY_STATE_EVENTS_KEEP_LAST", 1)
    home = home_paths(tmp_path / "home")
    store = _store(home)
    appended = []
    for index in range(4):
        _append(store, appended, f"hold-{index}")
    before = store.events_path.read_bytes()
    calls = []
    report = _lane(_request(home, legal_hold=True, coordinator=True,
                            commit=lambda operation: calls.append(operation) or operation()))
    assert report["complete"] is True
    assert report["deferred_reason"] == "legal_hold"
    assert calls == []
    assert _db_ids(store) == appended
    assert store.events_path.read_bytes() == before
    assert report.get("deleted_items", 0) == report.get("freed_bytes", 0) == 0


def test_workspace_databases_round_robin_and_clean_cycle(tmp_path):
    home = home_paths(tmp_path / "home")
    stores = [_store(home, name) for name in ("a", "b")]
    expected = [store.db_path.relative_to(home.owner_home_dir).as_posix() for store in stores]
    first = _lane(_request(home))
    second = _lane(_request(home, cursor=first["cursor"]))
    third = _lane(_request(home, cursor=second["cursor"]))
    assert [first["cursor"]["after"], second["cursor"]["after"], third["cursor"]["after"]] == [*expected, expected[0]]
    assert first["complete"] is False
    assert second["complete"] is True
    for report in (first, second, third):
        assert len(json.dumps(report["cursor"]).encode()) < 16 * 1024
        assert report["items"] == 1
        assert report["cursor"]["phase"] in {"events", "vacuum"}


@pytest.mark.parametrize("live_wal", [False, True])
def test_idle_coordinator_zero_commits_including_live_wal(tmp_path, live_wal):
    home = home_paths(tmp_path / "home")
    store = _store(home)
    calls = []
    conn = sqlite3.connect(store.db_path)
    try:
        if live_wal:
            conn.execute("INSERT INTO events(event_id,event_type,created_at) VALUES('retained','note',1)")
            conn.commit()
        report = _lane(_request(home, coordinator=True,
                                commit=lambda operation: calls.append(operation) or operation()))
        assert calls == []
        assert report["complete"] is True
        assert report["deleted_items"] == report["freed_bytes"] == 0
    finally:
        conn.close()


def test_empty_owner_is_idle_without_creating_directories(tmp_path):
    home = home_paths(tmp_path / "absent")
    report = _lane(_request(home))
    assert report["complete"] is True
    assert report["items"] == report["bytes"] == 0
    assert not home.root.exists()


def test_pending_ledger_without_cursor_is_resumed(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "STEADY_STATE_EVENTS_KEEP_LAST", 1)
    home = home_paths(tmp_path / "home")
    store = _store(home)
    ids = []
    for index in range(3):
        _append(store, ids, f"pending-{index}")
    with store._connection() as conn:
        conn.execute("DELETE FROM events WHERE event_id IN (?,?)", ids[:2])
        conn.execute("INSERT INTO metadata(key,value) VALUES('events_prune_pending_ids',?)", (json.dumps(ids[:2]),))
        conn.commit()
    cursor = {}
    for _ in range(8):
        report = _lane(_request(home, cursor=cursor))
        cursor = report["cursor"]
        if report["complete"]:
            break
    assert report["complete"] is True
    assert _db_ids(store) == _jsonl_ids(store) == ids[-1:]


def test_legacy_prune_step_still_requires_explicit_maintenance(tmp_path):
    store = LocalStore(tmp_path / "local.db")
    result = store.prune_events_step(before=time.time() + 60)
    assert result["physical_shrink"] == {"performed": False, "reason_code": "explicit_maintenance_required"}


def test_expired_deadline_still_performs_one_event_step(tmp_path, monkeypatch):
    assert callable(getattr(maintenance, "maintain_local_events_lane", None))
    monkeypatch.setattr(maintenance, "STEADY_STATE_EVENTS_KEEP_LAST", 1)
    home = home_paths(tmp_path / "home")
    store = _store(home)
    ids = []
    for index in range(3):
        _append(store, ids, f"budget-{index}")
    report = _lane(replace(_request(home), budget=StorageLaneBudget(None, None, time.monotonic() - 1)))
    assert report["deleted_items"] == 2
    assert _db_ids(store) == ids[-1:]


def test_precheck_seq_extrema_are_bounded_index_lookups(tmp_path, monkeypatch):
    home = home_paths(tmp_path / "home")
    store = _store(home)
    with store.connection_batch():
        for index in range(2000):
            store.record_event("indexed", payload={"index": index})
    actual_connect = sqlite3.connect
    vm_ticks = []

    def connect(*args, **kwargs):
        conn = actual_connect(*args, **kwargs)
        if kwargs.get("uri"):
            def progress():
                vm_ticks.append(1)
                return int(len(vm_ticks) >= 10)
            conn.set_progress_handler(progress, 100)
        return conn

    monkeypatch.setattr(maintenance.sqlite3, "connect", connect)
    failure = None
    try:
        snapshot = maintenance._local_events_precheck(store.db_path)
    except sqlite3.OperationalError as exc:
        failure, snapshot = exc, None
    assert failure is None, "idle precheck exceeded its bounded VM budget"
    assert snapshot[0] == 0
    assert len(vm_ticks) < 10


def test_byte_limited_lane_resumes_jsonl_across_real_append(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "STEADY_STATE_EVENTS_KEEP_LAST", 20)
    home = home_paths(tmp_path / "home")
    store = _store(home)
    with store.connection_batch():
        ids = [store.record_event("seed", payload={"padding": "p" * 700}).event_id for _ in range(50)]
    budget = StorageLaneBudget(None, 1024, None)
    first = _lane(_request(home, budget=budget))
    assert first["complete"] is False
    assert first["bytes"] <= 1024
    assert _db_ids(store) == ids[-20:]
    assert len(_jsonl_ids(store)) == 50
    appended = []
    _append(store, appended, "while-pending")
    cursor = first["cursor"]
    for _ in range(128):
        report = _lane(_request(home, cursor=cursor, budget=budget))
        assert report["bytes"] <= 1024
        cursor = report["cursor"]
        if report["complete"]:
            break
    assert report["complete"] is True
    assert _db_ids(store) == _jsonl_ids(store) == ids[-19:] + appended


def test_dirty_cycle_cannot_complete_on_next_idle_database(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "STEADY_STATE_EVENTS_KEEP_LAST", 1)
    home = home_paths(tmp_path / "home")
    first_store, _second_store = _store(home, "a"), _store(home, "b")
    for _ in range(3):
        first_store.record_event("due")
    first = _lane(_request(home))
    second = _lane(_request(home, cursor=first["cursor"]))
    assert first["deleted_items"] == 2
    assert first["complete"] is second["complete"] is False
    third = _lane(_request(home, cursor=second["cursor"]))
    fourth = _lane(_request(home, cursor=third["cursor"]))
    assert third["complete"] is False
    assert fourth["complete"] is True


def test_commit_source_oserror_propagates_same_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "STEADY_STATE_EVENTS_KEEP_LAST", 1)
    home = home_paths(tmp_path / "home")
    store = _store(home)
    for _ in range(3):
        store.record_event("due")
    error = OSError("original commit source rejected")
    def commit(_operation):
        raise error
    with pytest.raises(OSError) as caught:
        _lane(_request(home, coordinator=True, commit=commit))
    assert caught.value is error


def test_confirmed_prune_counts_survive_later_vacuum_io_error(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "STEADY_STATE_EVENTS_KEEP_LAST", 1)
    home = home_paths(tmp_path / "home")
    store = _store(home)
    for _ in range(3):
        store.record_event("due")
    def fail(_self, **_options):
        raise OSError("vacuum phase I/O error")
    monkeypatch.setattr(LocalStore, "incremental_vacuum_step", fail)
    report = _lane(_request(home))
    assert report["complete"] is False
    assert report["deleted_items"] == 2
    assert report["bytes"] > 0
    assert report["errors"][0]["code"] == "local_store_maintenance"
    assert len(_db_ids(store)) == 1


def test_expired_budget_does_not_start_additional_vacuum(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "STEADY_STATE_EVENTS_KEEP_LAST", 1)
    home = home_paths(tmp_path / "home")
    store = _store(home)
    for _ in range(3):
        store.record_event("due")
    def forbidden(_self, **_options):
        raise AssertionError("expired slice must not start another maintenance phase")
    monkeypatch.setattr(LocalStore, "incremental_vacuum_step", forbidden)
    report = _lane(_request(home, budget=StorageLaneBudget(None, None, time.monotonic() - 1)))
    assert report["deleted_items"] == 2
    assert report["deferred_reason"] == "budget_exhausted"


def test_sparse_seq_at_keep_limit_is_idle_without_commits(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "STEADY_STATE_EVENTS_KEEP_LAST", 3)
    home = home_paths(tmp_path / "home")
    store = _store(home)
    for _ in range(3):
        store.record_event("retained")
    with store._connection() as conn:
        conn.execute("UPDATE events SET seq=seq*100000")
        conn.commit()
    calls = []
    report = _lane(_request(home, coordinator=True,
                            commit=lambda operation: calls.append(operation) or operation()))
    assert calls == []
    assert report["complete"] is True
    assert report["backlog"] == 0
    assert len(_db_ids(store)) == 3


def test_jsonl_only_store_never_deletes_unowned_mirror_records(tmp_path):
    home = home_paths(tmp_path / "home")
    directory = home.owner_home_dir / "data" / "local_store"
    directory.mkdir(parents=True)
    mirror = directory / "events.jsonl"
    original = b'{"event_id":"mirror-only","payload":{"preserve":true}}\n'
    mirror.write_bytes(original)
    first = _lane(_request(home))
    second = _lane(_request(home, cursor=first["cursor"]))
    assert first["deleted_items"] == second["deleted_items"] == 0
    assert second["complete"] is True
    assert mirror.read_bytes() == original
