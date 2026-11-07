"""R5 活跃集、历史定位和通知认领合同，只在临时目录中操作记录。"""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.tooling import process_session_retention as retention
from agent_py_agent.agent.tooling import process_session_store as module
from agent_py_agent.agent.tooling.process_session_records import PROCESS_SESSION_SCHEMA
from agent_py_agent.agent.user_space.storage_lanes import StorageLaneBudget, StorageLaneRequest


def _record(session_id="bg-a", **changes):
    return {
        "schema": PROCESS_SESSION_SCHEMA, "session_id": session_id, "revision": 0,
        "access_scope": {"owner_id": "owner-a", "conversation_id": "thread-a", "owner_home": "/owners/example"},
        "execution_scope": {"owner_home": "/owners/example", "thread_id": "thread-a",
                            "root_task_id": "task-a", "run_id": "run-a", "attempt_id": "attempt-a"},
        "activation_scope": None, "retain_until_consumed": False,
        "completion_target": {}, "completion_notice_id": "", "command": "fixture", "cwd": "/workspace",
        "output_file": "", "host_state_file": "", "launcher_pid": 100,
        "launcher_birth_token": "launcher-birth", "pid": 101, "pid_birth_token": "host-birth",
        "child_pid": 102, "child_pid_birth_token": "child-birth", "reserved_at": 1.0,
        "started_at": 2.0, "finished_at": None, "exit_code": None, "status": "running",
        "child_launch_started": True, "stop_requested": False, "handoff_confirmed": False,
        **changes,
    }


def _target():
    return {"store_root": "/conversations", "thread_id": "thread-a", "task_id": "task-a", "run_id": "run-a"}


def _ended(session_id="bg-a", **changes):
    return _record(session_id, **{"status": "exited", "finished_at": time.time(), "exit_code": 0, **changes})


@pytest.fixture(autouse=True)
def _isolated_caches():
    caches = (module._PROCESS_RECORDS_PROJECTION_CACHE, module._PROCESS_RECORD_FILES_CACHE,
              module._PROCESS_RECORDS_SCAN_BATCHES)
    for cache in caches:
        cache.clear()
    yield
    for cache in caches:
        cache.clear()


@pytest.mark.parametrize("history_count", [1000, 10000])
def test_pending_scan_is_bounded_by_active_set(tmp_path, monkeypatch, history_count):
    store = module.ProcessSessionStore(tmp_path / "sessions")
    store.root.mkdir()
    # 批量铺设合法已发表记录，避免测试准备重复提交 redo；归档仍走真实 Store 事务。
    for index in range(history_count):
        record = _ended(f"bg-old-{index}", revision=1, completion_target=_target(), completion_notice_id="consumed")
        store.record_path(record["session_id"]).write_text(json.dumps(record), encoding="utf-8")
        if (index + 1) % 1000 == 0:
            # 模拟持续运行中分批收尾；每批搬家都走真实事务，不让准备工作超过单次扫描预算。
            assert store.prune_finished(max_age_seconds=None, is_notice_consumed=lambda _: True) == []
    for index in range(5):
        store.write(_record(f"bg-live-{index}", completion_target=_target()))
    assert store.prune_finished(max_age_seconds=None, is_notice_consumed=lambda _: True) == []
    assert len(list((store.root / "finished").glob("bg-*.json"))) == history_count
    counts = {"entries": 0, "json": 0}
    original_scandir, original_read = os.scandir, module.read_process_record

    @contextmanager
    def counted_scandir(path):
        assert os.fspath(path) == os.fspath(store.root), "热路径不得进入 finished/"
        with original_scandir(path) as entries:
            def counted():
                for entry in entries:
                    counts["entries"] += 1
                    yield entry
            yield counted()

    def counted_read(root, session_id):
        assert root == store.root
        counts["json"] += 1
        return original_read(root, session_id)

    monkeypatch.setattr(module, "os", SimpleNamespace(**{**vars(os), "scandir": counted_scandir}))
    monkeypatch.setattr(module, "read_process_record", counted_read)
    report = store.list_pending_completion_records_report()
    assert len(report.records) == 5 and report.load_errors == []
    assert counts["entries"] <= 7 and counts["json"] <= 5


def test_archive_keeps_load_and_private_file_identity(tmp_path):
    store = module.ProcessSessionStore(tmp_path)
    saved = store.write(_ended())
    before = store.record_path("bg-a").stat()
    assert store.prune_finished(max_age_seconds=None) == []
    history = store.root / "finished" / "bg-a.json"
    assert not store.record_path("bg-a").exists()
    assert history.stat().st_ino == before.st_ino
    assert stat.S_IMODE(history.stat().st_mode) == 0o600
    assert stat.S_IMODE(history.parent.stat().st_mode) == 0o700
    assert not (store.root / "bg-a.json.lock").exists()
    assert (store.root / ".process-sessions.lock").exists()
    assert store.list_records() == ([], [])
    assert store.load("bg-a").record == saved


def test_unsettled_and_retained_records_stay_active(tmp_path):
    store = module.ProcessSessionStore(tmp_path)
    rows = [
        _record("bg-live", completion_target=_target()),
        _ended("bg-notice", completion_target=_target()),
        _ended("bg-unconsumed", completion_target=_target(), completion_notice_id="unconsumed"),
        _ended("bg-pending", completion_notification_status="pending"),
        _ended("bg-retained", retain_until_consumed=True),
        _ended("bg-consumed", completion_target=_target(), completion_notice_id="consumed"),
    ]
    for row in rows:
        store.write(row)
    store.prune_finished(max_age_seconds=None, is_notice_consumed=lambda notice: notice == "consumed")
    assert {row["session_id"] for row in store.list_records()[0]} == {row["session_id"] for row in rows[:-1]}
    assert store.load("bg-consumed").record["completion_notice_id"] == "consumed"


def test_pending_reader_returns_while_writer_holds_transaction(tmp_path):
    store = module.ProcessSessionStore(tmp_path)
    saved = store.write(_record(completion_target=_target()))
    locked, release, reader_done = threading.Event(), threading.Event(), threading.Event()

    def writer():
        with store.transaction():
            locked.set()
            assert release.wait(10)

    def reader():
        try:
            return store.list_pending_completion_records_report()
        finally:
            reader_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(writer)
        try:
            assert locked.wait(5)
            reading = pool.submit(reader)
            assert reader_done.wait(3), "通知候选查询不能排队等待写者的目录锁"
            assert reading.result().records == [saved]
        finally:
            release.set()
        writing.result()


def test_completion_claim_rereads_one_record_under_lock(tmp_path):
    store = module.ProcessSessionStore(tmp_path)
    store.write(_ended(completion_target=_target()))
    ready = [threading.Event(), threading.Event()]
    claim = threading.Event()

    def contender(index):
        candidate = store.list_pending_completion_records_report().records[0]
        ready[index].set()
        assert claim.wait(5)
        with store.transaction() as transaction:
            latest = transaction.load(candidate["session_id"])
            if latest is None or latest["revision"] != candidate["revision"] or latest["completion_notice_id"]:
                return False
            transaction.write({**latest, "completion_notice_id": f"notice-{index}"})
            return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(contender, index) for index in range(2)]
        try:
            assert all(event.wait(5) for event in ready)
        finally:
            claim.set()
        assert sorted(future.result() for future in futures) == [False, True]
    assert store.list_pending_completion_records_report().records == []


def test_late_snapshot_cannot_restore_archived_record(tmp_path):
    store = module.ProcessSessionStore(tmp_path)
    snapshot = store.write(_record())
    captured, resume = threading.Event(), threading.Event()

    def late_writer():
        captured.set()
        assert resume.wait(5)
        with pytest.raises(module.ProcessSessionRevisionConflict):
            store.write(snapshot)

    with ThreadPoolExecutor(max_workers=1) as pool:
        writing = pool.submit(late_writer)
        try:
            assert captured.wait(5)
            terminal = store.write({**snapshot, "status": "exited", "finished_at": time.time(), "exit_code": 0})
            store.prune_finished(max_age_seconds=None)
        finally:
            resume.set()
        writing.result()
    assert not store.record_path("bg-a").exists()
    assert store.load("bg-a").record == terminal
    # 即使版本恰好相同，已归档句柄也不能被当作新的活跃记录发表。
    with pytest.raises(module.ProcessSessionRevisionConflict):
        store.write(terminal)


def test_disappeared_file_is_skipped_without_history_fallback(tmp_path, monkeypatch):
    store = module.ProcessSessionStore(tmp_path)
    store.write(_record(completion_target=_target()))
    original = module._read_listed_record

    def disappearing(root, name, *args, **kwargs):
        (root / name).unlink()
        return original(root, name, *args, **kwargs)

    monkeypatch.setattr(module, "_read_listed_record", disappearing)
    assert store.list_pending_completion_records_report() == module.ProcessSessionListReport([], [])


def test_retention_expires_only_finished_records(tmp_path):
    now = time.time()
    store = module.ProcessSessionStore(tmp_path / "sessions")
    store.write(_ended("bg-old", finished_at=now - 2 * 86400))
    store.write(_ended("bg-consumed", finished_at=now - 2 * 86400,
                       completion_target=_target(), completion_notice_id="consumed"))
    store.write(_ended("bg-young", finished_at=now - 60))
    store.prune_finished(max_age_seconds=None, is_notice_consumed=lambda _: True)
    active = store.write(_ended("bg-root", finished_at=now - 3 * 86400))
    pending = store.write(_ended("bg-pending", finished_at=now - 3 * 86400, completion_target=_target()))
    home = SimpleNamespace(owner_home_dir=tmp_path)
    request = StorageLaneRequest("process_sessions", home, now, {}, StorageLaneBudget(None, None, None),
                                 False, False, None, None)
    maintenance = retention._Pass(request, store.root)
    assert retention._prune(maintenance)
    assert maintenance.result["deleted_items"] == 2
    assert not store.load("bg-old").record
    assert not store.load("bg-consumed").record
    assert store.load("bg-young").record
    assert store.load("bg-root").record == active
    assert store.load("bg-pending").record == pending


@pytest.mark.parametrize("r0_present", [False, True])
def test_both_root_caches_follow_derived_capacity(tmp_path, monkeypatch, r0_present):
    monkeypatch.setattr(module, "resident_owner_capacity", lambda: 2)
    calls = []

    def resident_owners(floor):
        calls.append(floor)
        return max(floor, 7)

    monkeypatch.setattr(module, "cache_capacity", SimpleNamespace(resident_owners=resident_owners) if r0_present else None)
    expected = 7 if r0_present else 2
    roots = []
    for index in range(9):
        root = tmp_path / str(index)
        root.mkdir()
        roots.append(str(root))
        module._store_process_records_projection(root, module._process_dir_stat_key(root), [], [])
        module._store_process_record_files(root, module._ProcessRecordFiles({}, 0.0))
    assert list(module._PROCESS_RECORDS_PROJECTION_CACHE) == roots[-expected:]
    assert list(module._PROCESS_RECORD_FILES_CACHE) == roots[-expected:]
    assert (set(calls) == {2}) if r0_present else not calls
