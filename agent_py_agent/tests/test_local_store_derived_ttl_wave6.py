# LLM: 只使用临时SQLite/正文/归档；验收白名单、引用保护、事务/FTS一致性与重建TTL，不清理真实用户数据。
# 模块用途: Wave6 B3派生记录30天清理的专属回归。
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.local_storage import LocalStore

NOW = 2_000_000_000.0
CUTOFF = NOW - 30 * 86400


# LLM: 正文按产品API创建，仅调整测试库的确定时间；不手造FTS或绕过正式schema。
# 函数用途: 准备已老化记录，metadata=None表示可信终态，空字典专用于缺状态反例。
def record(store, sid, *, source="subagent_run", metadata=None, stamp=CUTOFF - 100):
    item = store.upsert_record(source_type=source, source_id=sid, title=sid,
        content=f"retention marker {sid}", metadata={"status": "done"} if metadata is None else metadata)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE records SET created_at=?,updated_at=? WHERE id=?", (stamp, stamp, item.id))
    return item


def prune(store, **kwargs):
    method = getattr(store, "prune_derived_records", None)
    assert callable(method), "missing B3 derived TTL protocol"
    return method(**kwargs)


def ids(store):
    with sqlite3.connect(store.db_path) as conn:
        return {row[0] for row in conn.execute("SELECT id FROM records")}


@pytest.mark.parametrize("fts", [True, False])
def test_prune_only_terminal_whitelist_and_keeps_fts_in_sync(tmp_path, fts):
    store = LocalStore(tmp_path / "local.db", enable_fts=fts)
    old = record(store, "expired")
    kept = [record(store, source, source=source) for source in
            ("memory", "conversation_message", "task_progress", "runtime_gate", "tool_outputs", "unknown_future_type")]
    kept += [record(store, "young", stamp=CUTOFF + 1), record(store, "boundary", stamp=CUTOFF)]
    result = prune(store, before=CUTOFF)
    assert result["deleted_ids"] == [old.id]
    assert ids(store) == {item.id for item in kept}
    assert not Path(old.content_path).is_absolute()
    assert not (store.root / old.content_path).exists()
    assert all((store.root / item.content_path).is_file() for item in kept)
    assert store.search("expired") == []
    if fts:
        assert store.fts_available, "this fixture must exercise real FTS5"
        with sqlite3.connect(store.db_path) as conn:
            assert conn.execute("SELECT count(*) FROM records_fts WHERE id=?", (old.id,)).fetchone()[0] == 0


@pytest.mark.parametrize("source", ["memory", "conversation_message", "task_progress", "note"])
def test_unsafe_source_filter_rejected_before_deletion(tmp_path, source):
    store = LocalStore(tmp_path / "local.db")
    item = record(store, "keep", source=source)
    with pytest.raises(ValueError):
        prune(store, before=CUTOFF, source_types=["subagent_run", source])
    assert ids(store) == {item.id}


@pytest.mark.parametrize("status", ["running", "pending", "planning", "queued", "waiting", "paused", "blocked", "starting", "cancelling", "new_status", None])
def test_active_or_unknown_run_protects_all_related_derived_records(tmp_path, status):
    store = LocalStore(tmp_path / "local.db")
    terminal = record(store, "run-a:result", source="subagent_runner_result", metadata={"run_id": "run-a", "status": "done"})
    active = record(store, "run-a", metadata={"status": status}, stamp=NOW)
    unrelated = record(store, "other")
    assert prune(store, before=CUTOFF)["deleted_ids"] == [unrelated.id]
    assert ids(store) == {terminal.id, active.id}


@pytest.mark.parametrize("flag", ["runtime_closeout_pending", "completion_notice_pending", "notification_pending", "wake_pending"])
def test_pending_closeout_and_notifications_protect_group(tmp_path, flag):
    store = LocalStore(tmp_path / "local.db")
    held = record(store, "a", metadata={"status": "DONE", flag: True})
    related = record(store, "a:old", source="subagent_work_log", metadata={"status": "ok", "run_id": "a"})
    assert prune(store, before=CUTOFF)["deleted_ids"] == []
    assert ids(store) == {held.id, related.id}


@pytest.mark.parametrize("key", ["source_id", "run_id", "task_id", "request_id", "record_id"])
def test_explicit_evidence_references_prevent_deletion(tmp_path, key):
    store = LocalStore(tmp_path / "local.db")
    item = record(store, "source-a", metadata={"status": "done", "run_id": "run-a", "task_id": "task-a", "request_id": "req-a"})
    options = {"protected_record_ids": [item.id]} if key == "record_id" else {
        "protected_source_ids": [{"source_id": "source-a", "run_id": "run-a", "task_id": "task-a", "request_id": "req-a"}[key]]}
    assert prune(store, before=CUTOFF, **options)["deleted_ids"] == []
    assert ids(store) == {item.id}


def test_batch_order_dry_run_and_empty_filter_are_explicit(tmp_path):
    store = LocalStore(tmp_path / "local.db")
    entries = sorted([record(store, f"x-{index}") for index in range(4)], key=lambda item: item.id)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and p.suffix not in {".db", ".db-wal", ".db-shm"}}
    result = prune(store, before=CUTOFF, max_delete=2, dry_run=True)
    assert result["candidate_ids"] == [item.id for item in entries[:2]] and result["deleted_ids"] == []
    assert ids(store) == {item.id for item in entries}
    assert all(Path(path).read_bytes() == data for path, data in before.items())
    assert prune(store, before=CUTOFF, source_types=[])["deleted_ids"] == []
    assert prune(store, before=CUTOFF, max_delete=0)["deleted_ids"] == []
    assert prune(store, before=CUTOFF, max_delete=2)["deleted_ids"] == [item.id for item in entries[:2]]


def test_missing_or_malformed_status_is_retained_but_old_event_can_expire(tmp_path):
    store = LocalStore(tmp_path / "local.db")
    missing = record(store, "missing", metadata={})
    broken = record(store, "broken")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE records SET metadata_json='not-json' WHERE id=?", (broken.id,))
    event = record(store, "event", source="gateway_event", metadata={"created_at": CUTOFF - 100})
    assert prune(store, before=CUTOFF)["deleted_ids"] == [event.id]
    assert ids(store) == {missing.id, broken.id}


def test_trigger_retained_record_keeps_fts_and_body(tmp_path):
    store = LocalStore(tmp_path / "local.db")
    item = record(store, "retained")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("CREATE TRIGGER keep_record BEFORE DELETE ON records BEGIN SELECT RAISE(IGNORE); END")
    assert prune(store, before=CUTOFF)["deleted_ids"] == []
    assert ids(store) == {item.id} and (store.root / item.content_path).is_file()
    assert store.search("retained")[0].id == item.id


def test_shared_content_path_is_not_unlinked(tmp_path):
    store = LocalStore(tmp_path / "local.db")
    old = record(store, "old")
    memory = record(store, "memory", source="memory")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE records SET content_path=? WHERE id=?", (old.content_path, memory.id))
    assert prune(store, before=CUTOFF)["deleted_ids"] == [old.id]
    assert (store.root / old.content_path).is_file() and ids(store) == {memory.id}


def test_pending_file_cleanup_retries_after_committed_deletion(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / "local.db")
    item = record(store, "retry")
    original = os.unlink

    def fail(path, *args, **kwargs):
        if Path(path).name == f"{item.id}.txt":
            raise OSError("interrupted cleanup")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(os, "unlink", fail)
        result = prune(store, before=CUTOFF)
    assert result["deleted_ids"] == [item.id] and ids(store) == set()
    assert (store.root / item.content_path).is_file()
    assert result["pending_files"] == 1
    retry = prune(LocalStore(store.db_path), before=CUTOFF)
    assert retry["deleted_ids"] == [] and retry["pending_files"] == 0
    assert not (store.root / item.content_path).exists()


def test_schema_upgrade_installs_composite_index_and_recent_tie_order(tmp_path):
    store = LocalStore(tmp_path / "local.db")
    entries = [record(store, f"index-{index}") for index in range(3)]
    with sqlite3.connect(store.db_path) as conn:
        names = {row[1] for row in conn.execute("PRAGMA index_list(records)")}
        assert "idx_records_source_updated_id" in names
        conn.execute("DROP INDEX idx_records_source_updated_id")
        conn.execute("DELETE FROM metadata WHERE key LIKE '%schema%'")
    store = LocalStore(store.db_path)
    with sqlite3.connect(store.db_path) as conn:
        assert "idx_records_source_updated_id" in {row[1] for row in conn.execute("PRAGMA index_list(records)")}
    assert [item.id for item in store.list_recent(source_type="subagent_run")] == sorted([item.id for item in entries], reverse=True)


@pytest.mark.parametrize(("status", "stamp", "admitted"), [
    ("done", CUTOFF - 1, False), ("failed", CUTOFF - 1, False),
    ("running", CUTOFF - 100, True), ("blocked", CUTOFF - 100, True),
    ("done", CUTOFF, True), ("done", NOW, True), ("done", None, True),
])
def test_gateway_rebuild_obeys_original_terminal_age(tmp_path, monkeypatch, status, stamp, admitted):
    from agent_py_agent.agent.gateway_parts import logging

    store = LocalStore(tmp_path / "local.db")
    monkeypatch.setattr(logging, "time", SimpleNamespace(time=lambda: NOW))
    payload = {"id": "gw-history", "status": status}
    if stamp is not None:
        payload.update(created_at=stamp, ended_at=stamp)
    assert logging._index_gateway_payload(SimpleNamespace(local_store=store), payload) is admitted
    assert bool(ids(store)) is admitted


def test_gateway_rebuild_uses_file_mtime_only_when_payload_has_no_time(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import logging

    monkeypatch.setattr(logging, "time", SimpleNamespace(time=lambda: NOW))
    archive = tmp_path / "gw-old.json"
    archive.write_text('{"id":"gw-old","status":"done"}')
    os.utime(archive, (CUTOFF - 1, CUTOFF - 1))
    original = archive.read_bytes()
    store = LocalStore(tmp_path / "local.db")
    options = logging.GatewayIndexPayloadOptions(request_path=archive)
    assert logging._index_gateway_payload(SimpleNamespace(local_store=store), json.loads(original), options) is False
    assert ids(store) == set() and archive.read_bytes() == original


# LLM: 任务和索引均走原产品对象，仅隔离时钟；不启动Agent、模型或外部服务。
# 函数用途: 构造可重建的真实子代理任务及其LocalStore索引服务。
def projection_case(tmp_path, status, stamp):
    from agent_py_agent.agent.subagents.models import SubAgentTask
    from agent_py_agent.agent.subagents.services.indexing.service import SubAgentIndexingService

    store = LocalStore(tmp_path / "local.db")
    task = SubAgentTask(id="old-run", goal="archive", thought="", plan=[], status=status,
        created_at=stamp, updated_at=stamp, ended_at=stamp, task_dir=str(tmp_path))
    manager = SimpleNamespace(local_store=store, conversation_store=None, owner_home_dir="", home_paths=None)
    manager.indexing = SubAgentIndexingService(manager)
    manager.load = lambda run_id: task
    return manager, task


@pytest.mark.parametrize(("status", "stamp", "admitted"), [
    ("DONE", CUTOFF - 1, False), ("FAILED", CUTOFF - 1, False),
    ("RUNNING", CUTOFF - 1, True), ("BLOCKED", CUTOFF - 1, True),
    ("DONE", CUTOFF, True), ("DONE", NOW, True), ("DONE", 0.0, True),
])
def test_subagent_rebuild_filters_fulltext_but_keeps_light_registry(tmp_path, monkeypatch, status, stamp, admitted):
    from agent_py_agent.agent.subagents.services.persistence import projections

    manager, task = projection_case(tmp_path, status, stamp)
    monkeypatch.setattr(projections, "time", SimpleNamespace(time=lambda: NOW))
    projections.rebuild_derived_projections(manager, task.id)
    assert bool(ids(manager.local_store)) is admitted
    with sqlite3.connect(manager.local_store.db_path) as conn:
        assert conn.execute("SELECT status FROM task_registry WHERE task_id=?", (task.id,)).fetchone() == (status,)


def test_projection_ledger_is_bounded_private_and_keeps_latest_records(tmp_path):
    from agent_py_agent.agent.subagents.services.persistence import projections

    path = tmp_path / "projection_ledger.jsonl"
    original = [json.dumps({"position": index}) + "\n" for index in range(2005)]
    path.write_text("".join(original))
    projections._append_projection_ledger(tmp_path, [projections.ProjectionRecord("latest", "ok", NOW)])
    lines = path.read_text().splitlines()
    assert len(lines) == 2000
    assert json.loads(lines[0]) == {"position": 6}
    assert json.loads(lines[-1])["step"] == "latest"
    assert path.stat().st_mode & 0o777 == 0o600


def test_projection_ledger_concurrent_appends_do_not_lose_lines(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.subagents.services.persistence import projections

    def append(index):
        projections._append_projection_ledger(tmp_path, [projections.ProjectionRecord(str(index), "ok", NOW)])

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append, range(24)))
    lines = (tmp_path / "projection_ledger.jsonl").read_text().splitlines()
    assert len(lines) == 24 and {json.loads(line)["step"] for line in lines} == {str(index) for index in range(24)}


@pytest.mark.parametrize("entry", [{}, {"id": "bad", "source_ids": None}])
def test_bad_file_debt_is_retained_without_blocking_other_cleanup(tmp_path, entry):
    store = LocalStore(tmp_path / "local.db")
    item = record(store, "valid-cleanup")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("INSERT INTO metadata(key,value) VALUES('derived_prune_pending_files',?)", (json.dumps([entry]),))
    result = prune(store, before=CUTOFF)
    assert result["deleted_ids"] == [item.id] and result["pending_files"] == 1
    assert not (store.root / item.content_path).exists()


def test_referenced_file_debt_remains_retryable(tmp_path):
    store = LocalStore(tmp_path / "local.db")
    item = record(store, "shared-old")
    memory = record(store, "permanent", source="memory")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE records SET content_path=? WHERE id=?", (item.content_path, memory.id))
    assert prune(store, before=CUTOFF)["pending_files"] == 1
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DELETE FROM records WHERE id=?", (memory.id,))
    assert prune(store, before=CUTOFF)["pending_files"] == 0
    assert not (store.root / item.content_path).exists()


def test_changed_file_debt_is_not_silently_acknowledged(tmp_path):
    store = LocalStore(tmp_path / "local.db")
    item = record(store, "changed")
    path = store.root / item.content_path
    path.write_text("different unowned content")
    result = prune(store, before=CUTOFF)
    assert result["pending_files"] == 1 and path.read_text() == "different unowned content"


def test_reactivated_group_protects_pending_file_debt(tmp_path, monkeypatch):
    from agent_py_agent.agent.local_storage import records

    store = LocalStore(tmp_path / "local.db")
    old = record(store, "run-p:old", source="subagent_work_log", metadata={"status": "ok", "run_id": "run-p"})
    with monkeypatch.context() as m:
        m.setattr(records, "unlink_file_beneath", lambda *args: (_ for _ in ()).throw(OSError("pending")))
        assert prune(store, before=CUTOFF)["pending_files"] == 1
    record(store, "run-p", metadata={"status": "RUNNING"}, stamp=NOW)
    assert prune(store, before=CUTOFF)["pending_files"] == 1
    assert (store.root / old.content_path).is_file()


def test_cleanup_count_and_bytes_have_independent_resource_budget(tmp_path, monkeypatch):
    from agent_py_agent.agent.local_storage import records

    store = LocalStore(tmp_path / "local.db")
    entries = [record(store, f"budget-{i}") for i in range(3)]
    monkeypatch.setattr(records, "_DERIVED_FILE_CLEANUP_MAX_FILES", 1, raising=False)
    monkeypatch.setattr(records, "_DERIVED_FILE_CLEANUP_MAX_BYTES", 1, raising=False)
    result = prune(store, before=CUTOFF)
    assert result["pending_files"] == 3
    assert all((store.root / item.content_path).is_file() for item in entries)
    monkeypatch.setattr(records, "_DERIVED_FILE_CLEANUP_MAX_BYTES", 10000, raising=False)
    assert prune(store, before=CUTOFF)["pending_files"] == 2


def test_actual_subagent_closeout_attribute_prevents_expiry(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.services.persistence import projections
    from agent_py_agent.agent.subagents.services.runtime_closeout import RUNTIME_CLOSEOUT_ATTR

    manager, task = projection_case(tmp_path, "DONE", CUTOFF - 1)
    task.attributes[RUNTIME_CLOSEOUT_ATTR] = {"schema_version": "subagent-runtime-closeout.v1"}
    monkeypatch.setattr(projections, "time", SimpleNamespace(time=lambda: NOW))
    projections.rebuild_derived_projections(manager, task.id)
    assert ids(manager.local_store), "canonical closeout-pending tasks must keep their fulltext projection"


@pytest.mark.parametrize(("column", "value"), [
    ("updated_at", NOW), ("content_hash", "changed-version"), ("content_path", "files/replaced.txt"),
    ("metadata_json", '{"status":"RUNNING"}'), ("source_type", "memory"),
])
def test_snapshot_version_change_is_not_deleted(tmp_path, monkeypatch, column, value):
    from agent_py_agent.agent.local_storage import records

    store = LocalStore(tmp_path / "local.db")
    item = record(store, "versioned")
    original = records._derived_transaction
    changed = False

    def concurrent_change(target, operation):
        nonlocal changed
        if not changed:
            changed = True
            with sqlite3.connect(target.db_path) as conn:
                conn.execute(f"UPDATE records SET {column}=? WHERE id=?", (value, item.id))
        return original(target, operation)

    monkeypatch.setattr(records, "_derived_transaction", concurrent_change)
    assert prune(store, before=CUTOFF)["deleted_ids"] == []
    assert ids(store) == {item.id} and (store.root / item.content_path).is_file()


@pytest.mark.parametrize("symlink", [False, True])
def test_outside_content_path_or_link_is_never_unlinked(tmp_path, symlink):
    store = LocalStore(tmp_path / "local.db")
    item = record(store, "unsafe")
    outside = tmp_path / "must-not-delete.txt"
    outside.write_text(item.content)
    path = store.root / item.content_path
    if symlink:
        path.unlink()
        path.symlink_to(outside)
    else:
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("UPDATE records SET content_path=? WHERE id=?", (str(outside), item.id))
    result = prune(store, before=CUTOFF)
    assert result["pending_files"] == 1 and outside.read_text() == item.content


def test_concurrent_memory_upsert_cannot_lose_its_uncommitted_body(tmp_path, monkeypatch):
    from agent_py_agent.agent.local_storage import records

    store = LocalStore(tmp_path / "local.db")
    item = record(store, "reused")
    started, finished, errors = threading.Event(), threading.Event(), []
    original = records.unlink_file_beneath
    workers = []

    def writer():
        started.set()
        try:
            other = LocalStore(store.db_path)
            other.upsert_record(source_type="memory", source_id="permanent", record_id=item.id,
                title="keep", content="new permanent body")
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    def cleanup(root, parts):
        worker = threading.Thread(target=writer)
        workers.append(worker)
        worker.start()
        assert started.wait(3)
        assert not finished.wait(0.05), "upsert must wait before writing its new body"
        original(root, parts)

    monkeypatch.setattr(records, "unlink_file_beneath", cleanup)
    try:
        assert prune(store, before=CUTOFF)["deleted_ids"] == [item.id]
    finally:
        for worker in workers:
            worker.join(5)
    assert finished.is_set() and not errors
    actual = store.get_record(item.id)
    assert actual.source_type == "memory" and actual.content == "new permanent body"


def test_coordinator_prune_keeps_original_scope_and_fts_transaction(tmp_path):
    from agent_py_agent.agent.common.global_write_commit import (
        GlobalWriteCommitUnavailable,
        global_write_commit_scope,
    )
    from agent_py_agent.tests.test_local_store_coordinator_io import make_store

    ordinary = LocalStore(tmp_path / "index.db")
    item = record(ordinary, "coordinator")
    store, door = make_store(tmp_path)
    with pytest.raises(GlobalWriteCommitUnavailable, match="original_global_write_scope_required"):
        prune(store, before=CUTOFF)
    assert ids(store) == {item.id}
    with global_write_commit_scope(door):
        result = prune(store, before=CUTOFF)
    assert result["deleted_ids"] == [item.id] and result["pending_files"] == 0
    assert ids(store) == set() and not (store.root / item.content_path).exists()


# LLM: 用真实SQLite、索引器与固定时钟覆盖裁剪后CLI重建；永久来源和canonical字节不能随TTL丢失。
# 函数用途: 防止旧终态全文复活，并保护截止边界、活跃任务、未知时间和待收口任务。
@pytest.mark.parametrize(("status", "stamp", "closeout", "admitted"), [
    ("DONE", CUTOFF - 1, False, False), ("FAILED", CUTOFF - 1, False, False),
    ("DONE", CUTOFF, False, True), ("DONE", NOW, False, True),
    ("RUNNING", CUTOFF - 1, False, True), ("BLOCKED", CUTOFF - 1, False, True),
    ("PAUSED", CUTOFF - 1, False, True), ("DONE", 0.0, False, True),
    ("DONE", CUTOFF - 1, True, True),
])
def test_cli_rebuild_does_not_resurrect_pruned_derived_fulltext(tmp_path, monkeypatch, status, stamp, closeout, admitted):
    from agent_py_agent.agent.subagents.services.persistence import projections
    from agent_py_agent.cli.local_doctor import rebuild_subagent_index

    manager, task = projection_case(tmp_path, status, stamp)
    manager.list_runs = lambda: [task]
    if closeout:
        task.attributes["runtime_closeout_pending"] = {}
    monkeypatch.setattr(projections, "time", SimpleNamespace(time=lambda: NOW))
    previous = record(manager.local_store, task.id)
    permanent = record(manager.local_store, "keep-memory", source="memory")
    assert prune(manager.local_store, before=CUTOFF)["deleted_ids"] == [previous.id]
    paths = ("WORK_LOG.md", "execution_context.json", "reports/runner_result.json", "reports/patch_review.json")
    original = {}
    for relative in paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"canonical":"keep unchanged"}\n')
        original[path] = path.read_bytes()
    rebuilt = rebuild_subagent_index(SimpleNamespace(subagents=manager, local_store=manager.local_store))
    assert rebuilt == (5 if admitted else 1)
    with sqlite3.connect(manager.local_store.db_path) as conn:
        actual = {row[0] for row in conn.execute("SELECT source_type FROM records")}
    expected = {"memory", "subagent_patch_review"}
    if admitted:
        expected.update({"subagent_run", "subagent_work_log", "subagent_execution_context", "subagent_runner_result"})
    assert actual == expected and permanent.id in ids(manager.local_store)
    assert all(path.read_bytes() == data for path, data in original.items())
