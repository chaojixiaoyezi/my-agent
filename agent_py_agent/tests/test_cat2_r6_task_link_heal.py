"""R6：只在维护周期修复坏活动链接和幽灵历史索引，读取仍遵守 RES-W5。"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.store_tasks import TaskStore
from agent_py_agent.agent.gateway_parts.request_errors import ConversationPersistenceError
from agent_py_agent.agent.gateway_parts.request_execution import _require_gateway_conversation_ready
from agent_py_agent.tests.test_res_w5_gateway_context_degrade import _context, _link
from agent_py_agent.tests.test_res_w5_gateway_context_degrade import case as _case

case = _case


@pytest.mark.parametrize("raw", [b"{broken", b"\xff"], ids=["json", "utf8"])
def test_gc_heals_corrupt_active_link_and_prunes_only_missing_history(case, raw):
    _link(case, "live", "active")
    _link(case, "old", "completed")
    _link(case, "other", "active")
    _context(case)
    path = case.store.storage.task_path("live")
    path.write_bytes(raw)
    case.store.storage.task_path("old").unlink()
    thread_path = case.store.storage.thread_path(case.thread.thread_id)
    before = json.loads(thread_path.read_text())
    with pytest.raises(ConversationPersistenceError):
        _require_gateway_conversation_ready(case.request, _context(case))
    assert json.loads(thread_path.read_text()) == before

    summary = case.store.gc_stale_ledger_records(now=time.time())
    print("R6_GC_SUMMARY=" + json.dumps(summary, sort_keys=True))
    _require_gateway_conversation_ready(case.request, _context(case))
    assert summary["quarantined_task_links"] == summary["pruned_task_ids"] == 1
    assert (path.parent / ".ledger_archive" / path.name).read_bytes() == raw
    assert not path.exists()
    thread = case.store.threads.require(case.thread.thread_id)
    assert thread.task_ids == thread.active_task_ids == ("live", "other")
    assert json.loads(thread_path.read_text()) == {**before, "task_ids": ["live", "other"]}


def test_gc_keeps_identity_mismatch_fail_closed(case):
    link = _link(case, "live", "active")
    path = case.store.storage.task_path("live")
    raw = json.dumps({**link.to_dict(), "task_id": "foreign-task"})
    path.write_text(raw)
    summary = case.store.gc_stale_ledger_records(now=time.time())
    assert summary["quarantined_task_links"] == summary["pruned_task_ids"] == 0
    assert path.read_text() == raw
    assert not (path.parent / ".ledger_archive" / path.name).exists()
    with pytest.raises(ConversationPersistenceError):
        _require_gateway_conversation_ready(case.request, _context(case))


@pytest.mark.parametrize("fault", ["permission", "symlink"])
def test_gc_never_quarantines_permission_errors_or_symlinks(case, monkeypatch, fault):
    _link(case, "live", "active")
    path = case.store.storage.task_path("live")
    if fault == "permission":
        raw = path.read_bytes() + b"\n"
        path.write_bytes(raw)
        original_read = Path.read_text

        def unreadable(target, *args, **kwargs):
            if target == path:
                raise PermissionError("simulated task link read error")
            return original_read(target, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", unreadable)
    else:
        raw = b"{broken"
        target = path.parent / "preserved-target"
        target.write_bytes(raw)
        path.unlink()
        path.symlink_to(target)
    summary = case.store.gc_stale_ledger_records(now=time.time())
    assert summary["quarantined_task_links"] == summary["pruned_task_ids"] == 0
    assert path.read_bytes() == raw
    assert path.is_symlink() == (fault == "symlink")
    with pytest.raises(ConversationPersistenceError):
        _require_gateway_conversation_ready(case.request, _context(case))


def test_gc_rechecks_ghost_file_after_directory_snapshot(case, monkeypatch):
    _link(case, "old", "completed")
    path = case.store.storage.task_path("old")
    thread_path = case.store.storage.thread_path(case.thread.thread_id)
    before = thread_path.read_bytes()
    original = os.listdir
    snapshots = []

    def without_recreated_link(directory):
        names = original(directory)
        if Path(directory) == case.store.storage.tasks_dir:
            snapshots.append(names)
            return [name for name in names if name != path.name]
        return names

    monkeypatch.setattr(os, "listdir", without_recreated_link)
    summary = case.store.gc_stale_ledger_records(now=time.time())
    assert snapshots and path.is_file()
    assert summary["quarantined_task_links"] == summary["pruned_task_ids"] == 0
    assert thread_path.read_bytes() == before


def test_busy_task_lock_defers_quarantine_until_next_gc(case, caplog):
    _link(case, "live", "active")
    path = case.store.storage.task_path("live")
    path.write_text("{broken")
    held, release = threading.Event(), threading.Event()
    errors = []

    def hold_lock():
        try:
            with locked_json_path(path):
                held.set()
                assert release.wait(10)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=hold_lock)
    worker.start()
    try:
        assert held.wait(5)
        with caplog.at_level(logging.WARNING):
            summary = case.store.gc_stale_ledger_records(now=time.time())
        assert summary["quarantined_task_links"] == 0 and path.read_text() == "{broken"
        assert any(record.getMessage().startswith("TASK_LINK_HEAL_FAILED ")
                   and "error_type=BlockingIOError" in record.getMessage() for record in caplog.records)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and errors == []
    assert case.store.gc_stale_ledger_records(now=time.time())["quarantined_task_links"] == 1
    assert not path.exists()


def test_one_thread_heal_failure_does_not_block_another(case, monkeypatch, caplog):
    _link(case, "first-old", "completed")
    case.store.storage.task_path("first-old").unlink()
    other_thread = case.store.threads.create(title="另一条会话")
    other = SimpleNamespace(agent=case.agent, store=case.store, thread=other_thread)
    _link(other, "second-live", "active")
    _link(other, "second-old", "completed")
    live = case.store.storage.task_path("second-live")
    live.write_text("{broken")
    case.store.storage.task_path("second-old").unlink()
    original = TaskStore._prune_ghost_task_ids

    def prune(store, thread_id, ghosts):
        if thread_id == case.thread.thread_id:
            raise OSError("simulated thread write error")
        return original(store, thread_id, ghosts)

    monkeypatch.setattr(TaskStore, "_prune_ghost_task_ids", prune)
    with caplog.at_level(logging.WARNING):
        summary = case.store.gc_stale_ledger_records(now=time.time())
    assert summary["quarantined_task_links"] == summary["pruned_task_ids"] == 1
    assert case.store.threads.require(case.thread.thread_id).task_ids == ("first-old",)
    assert case.store.threads.require(other_thread.thread_id).task_ids == ("second-live",)
    assert not live.exists()
    assert any(record.getMessage().startswith("TASK_LINK_HEAL_FAILED ")
               and case.thread.thread_id in record.getMessage() for record in caplog.records)


def test_quarantine_rechecks_link_repaired_before_lock(case, monkeypatch):
    link = _link(case, "live", "active")
    path = case.store.storage.task_path("live")
    path.write_text("{broken")
    original = case.store.tasks.load_report
    repaired = []

    def repair_after_read(task_id):
        result = original(task_id)
        if result[1] is not None and not repaired:
            path.write_text(json.dumps(link.to_dict()))
            repaired.append(True)
        return result

    monkeypatch.setattr(case.store.tasks, "load_report", repair_after_read)
    assert case.store.gc_stale_ledger_records(now=time.time())["quarantined_task_links"] == 0
    assert repaired and path.is_file()
    _require_gateway_conversation_ready(case.request, _context(case))


def test_ghost_prune_preserves_newly_active_id_and_concurrent_thread_fields(case, monkeypatch):
    from agent_py_agent.agent.conversation import store_tasks as module

    _link(case, "old", "completed")
    case.store.storage.task_path("old").unlink()
    thread_path = case.store.storage.thread_path(case.thread.thread_id)
    original = module.update_json_file_atomic
    concurrent = []

    def activate_before_update(path, updater, **kwargs):
        if path == thread_path and not concurrent:
            case.store.threads.update_atomic(case.thread.thread_id, lambda latest: replace(
                latest, active_task_ids=("old",), title="并发保留标题", metadata={"keep": True},
                updated_at=latest.updated_at + 1))
            concurrent.append(thread_path.read_bytes())
        return original(path, updater, **kwargs)

    monkeypatch.setattr(module, "update_json_file_atomic", activate_before_update)
    assert case.store.gc_stale_ledger_records(now=time.time())["pruned_task_ids"] == 0
    assert concurrent and thread_path.read_bytes() == concurrent[0]
    latest = case.store.threads.require(case.thread.thread_id)
    assert latest.task_ids == latest.active_task_ids == ("old",)
    assert latest.title == "并发保留标题" and latest.metadata == {"keep": True}


def test_thread_scan_failure_keeps_completed_gc_counts(case, monkeypatch, caplog):
    def unavailable(**kwargs):
        raise PermissionError("simulated directory read error")

    monkeypatch.setattr(case.store.threads, "list_report", unavailable)
    with caplog.at_level(logging.WARNING):
        summary = case.store.gc_stale_ledger_records(now=time.time())
    assert summary["quarantined_task_links"] == summary["pruned_task_ids"] == 0
    assert "archived_policies" in summary and "archived_claims" in summary
    assert "TASK_LINK_HEAL_SKIPPED error_type=PermissionError" in caplog.text


def test_gc_wall_time_for_1000_threads_with_200_historical_ids(tmp_path, capsys):
    """只报告实测耗时；有现成 tmpfs 时使用它，否则如实标记临时磁盘目录。"""
    ram_root = Path("/dev/shm")
    use_tmpfs = ram_root.is_dir() and os.access(ram_root, os.W_OK)
    with tempfile.TemporaryDirectory(prefix="cat2-r6-", dir=ram_root if use_tmpfs else tmp_path) as root:
        store = ConversationStore(Path(root) / "conversations")
        for number in range(1000):
            thread_id = f"thread-r6-{number:04}"
            thread = ConversationThread(thread_id=thread_id, canonical_user_id="r6-timing", updated_at=1,
                task_ids=tuple(f"{thread_id}-old-{item:03}" for item in range(200)))
            store.storage.thread_path(thread_id).write_text(json.dumps(thread.to_dict()))
        started = time.perf_counter()
        summary = store.gc_stale_ledger_records(now=time.time())
        elapsed = time.perf_counter() - started
        with capsys.disabled():
            print("R6_GC_TIMING=" + json.dumps({"threads": 1000, "ids_per_thread": 200,
                "filesystem": "tmpfs" if use_tmpfs else "temporary_directory", "seconds": elapsed,
                "summary": summary}, sort_keys=True))
        assert summary["quarantined_task_links"] == 0 and summary["pruned_task_ids"] == 200000
