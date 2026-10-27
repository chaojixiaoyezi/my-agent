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
    event = record(store, "event", source="gateway_event", metadata={"created_at": CUTOFF - 100, "status": "done"})
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


# LLM: R2软字节预算允许首个超额文件前进，文件数上限仍严格生效，不保留旧的永久饥饿断言。
# 函数用途: 验证软字节预算不会取消独立的文件数量上限。
def test_cleanup_count_and_bytes_have_independent_resource_budget(tmp_path, monkeypatch):
    from agent_py_agent.agent.local_storage import records

    store = LocalStore(tmp_path / "local.db")
    entries = [record(store, f"budget-{i}") for i in range(3)]
    monkeypatch.setattr(records, "_DERIVED_FILE_CLEANUP_MAX_FILES", 1, raising=False)
    monkeypatch.setattr(records, "_DERIVED_FILE_CLEANUP_MAX_BYTES", 1, raising=False)
    result = prune(store, before=CUTOFF)
    assert result["pending_files"] == 2
    assert sum((store.root / item.content_path).is_file() for item in entries) == 2
    monkeypatch.setattr(records, "_DERIVED_FILE_CLEANUP_MAX_BYTES", 10000, raising=False)
    assert prune(store, before=CUTOFF)["pending_files"] == 1


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


# LLM: 真实索引器必须把canonical收口欠账带入SQL，随后裁剪不得丢掉该运行。
# 函数用途: 捕获任务仍待收口、派生索引却漏标的删除漏洞。
def test_r2_indexed_task_projects_pending_closeout(tmp_path):
    manager, task = projection_case(tmp_path, "DONE", CUTOFF - 1)
    task.attributes["runtime_closeout_pending"] = {"stage": "notify"}
    manager.indexing.index_task(task)
    with sqlite3.connect(manager.local_store.db_path) as conn:
        conn.execute("UPDATE records SET created_at=?,updated_at=?", (CUTOFF - 1, CUTOFF - 1))
    item = manager.local_store.list_recent()[0]
    assert item.metadata.get("runtime_closeout_pending") is True
    assert prune(manager.local_store, before=CUTOFF)["deleted_ids"] == []


# LLM: 两个真实嵌套容器都不能丢失已有的四种收口标志。
# 函数用途: 防止嵌套未完成工作被顶层done遮住。
@pytest.mark.parametrize("container", ["attributes", "conversation_runtime"])
@pytest.mark.parametrize("flag", ["runtime_closeout_pending", "wake_pending", "notification_pending", "completion_notice_pending"])
def test_r2_nested_pending_flags_keep_record(tmp_path, container, flag):
    store = LocalStore(tmp_path / "local.db")
    item = record(store, "pending", metadata={"status": "done", container: {flag: True}})
    assert prune(store, before=CUTOFF)["deleted_ids"] == []
    assert ids(store) == {item.id}


# LLM: 各层结构化ID同时用于显式引用和活跃关联；不从正文推断。
# 函数用途: 覆盖根/父运行以及会话和attributes里的ID保护。
@pytest.mark.parametrize(("container", "key"), [(None, "root_id"), (None, "parent_id"),
    (None, "root_run_id"), (None, "parent_run_id"), ("conversation_runtime", "task_id"),
    ("conversation_runtime", "run_id"), ("conversation_runtime", "request_id"),
    ("attributes", "task_id"), ("attributes", "run_id")])
def test_r2_nested_identity_protects_explicit_refs_and_active_groups(tmp_path, container, key):
    store = LocalStore(tmp_path / "local.db")
    data = {key: "protected-root"} if container is None else {container: {key: "protected-root"}}
    held = record(store, "held", metadata={"status": "done", **data})
    assert prune(store, before=CUTOFF, protected_source_ids=["protected-root"])["deleted_ids"] == []
    active = record(store, "active", metadata={"status": "running", **data}, stamp=NOW)
    assert prune(store, before=CUTOFF)["deleted_ids"] == []
    assert ids(store) == {held.id, active.id}


# LLM: 数据库的物理FTS存在与当前检索开关无关，真实重开后验证两边同步。
# 函数用途: 防止禁用FTS时TTL遗留全文孤儿。
def test_r2_disabled_fts_still_deletes_existing_fts_rows(tmp_path):
    store = LocalStore(tmp_path / "local.db")
    item = record(store, "fts-disabled")
    reopened = LocalStore(store.db_path, enable_fts=False)
    assert not reopened.fts_available
    assert prune(reopened, before=CUTOFF)["deleted_ids"] == [item.id]
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM records_fts WHERE id=?", (item.id,)).fetchone() == (0,)


# LLM: 用真实只读view制造FTS删除错误，不mock数据库事务或跳过故障。
# 函数用途: FTS删不动时必须保留records与正文，并在回执反映跳过。
def test_r2_fts_delete_failure_rolls_back_savepoint_and_keeps_body(tmp_path):
    store = LocalStore(tmp_path / "local.db")
    item = record(store, "fts-failure")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("ALTER TABLE records_fts RENAME TO archived_fts")
        conn.execute("CREATE VIEW records_fts AS SELECT * FROM archived_fts")
    reopened = LocalStore(store.db_path, enable_fts=False)
    result = prune(reopened, before=CUTOFF)
    assert result["deleted_ids"] == [] and ids(store) == {item.id}
    assert result["skipped_protected_or_active"] >= 1
    assert (store.root / item.content_path).is_file()
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM records_fts WHERE id=?", (item.id,)).fetchone() == (1,)


# LLM: 创建时间不是终态时间，文件mtime只在缺少有效终态/更新时间时兜底。
# 函数用途: 防止长寿任务被创建时间误判为过期。
@pytest.mark.parametrize("fallback", [None, NOW])
def test_r2_created_at_alone_does_not_expire_terminal_metadata(fallback):
    from agent_py_agent.agent.local_storage.records import derived_terminal_expired

    assert not derived_terminal_expired({"status": "done", "created_at": CUTOFF - 1}, CUTOFF, fallback)


# LLM: 明确终态时间的所有兼容键都应优先于更旧的创建时间和文件mtime。
# 函数用途: 保护刚结束或刚更新的老任务。
@pytest.mark.parametrize("key", ["finished_at", "terminated_at", "settled_at", "closed_at", "recorded_at", "timestamp"])
def test_r2_recent_terminal_time_alias_overrides_old_creation(key):
    from agent_py_agent.agent.local_storage.records import derived_terminal_expired

    assert not derived_terminal_expired({"status": "done", "created_at": 1, key: NOW}, CUTOFF, CUTOFF - 1)


# LLM: 未知事件状态永不合成done；明确终态事件仍按正常TTL裁剪。
# 函数用途: 让gateway_event遵守与其它来源相同的终态证明门。
@pytest.mark.parametrize("status", [None, "", "done", "completed"])
def test_r2_gateway_event_requires_explicit_terminal_status(tmp_path, status):
    store = LocalStore(tmp_path / "local.db")
    metadata = {} if status is None else {"status": status}
    item = record(store, "event", source="gateway_event", metadata=metadata)
    assert prune(store, before=CUTOFF)["deleted_ids"] == ([item.id] if status else [])


# LLM: 事件名只能来自显式宿主终态集合，不能把任意包含completed的字符串当完成。
# 函数用途: 在事件写入时保存可用于TTL判定的明确终态。
@pytest.mark.parametrize(("event", "expected"), [("gateway_request_completed", "completed"),
    ("gateway_request_failed", "failed"), ("gateway_start", ""), ("not_completed", "")])
def test_r2_gateway_event_writer_projects_known_terminal_event(tmp_path, event, expected):
    from agent_py_agent.agent.gateway_parts.logging import log_gateway_event

    store = LocalStore(tmp_path / "local.db")
    log_gateway_event(SimpleNamespace(local_store=store, store_event_mirror_enabled=True), event, {})
    assert store.list_recent()[0].metadata["status"] == expected


# LLM: 用真实大于默认软预算的正文测试流式hash；禁止read_bytes以免偷读整文件。
# 函数用途: 保证超大单文件每轮仍能前进，同时不放开后续文件预算。
def test_r2_oversized_blob_is_streamed_and_one_blob_makes_progress(tmp_path, monkeypatch):
    import hashlib

    from agent_py_agent.agent.local_storage import records

    store = LocalStore(tmp_path / "local.db")
    entries = [record(store, f"huge-{i}") for i in range(2)]
    body = b"x" * (8 * 1024 * 1024 + 1)
    for item in entries:
        (store.root / item.content_path).write_bytes(body)
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("UPDATE records SET content_hash=? WHERE id=?", (hashlib.sha256(body).hexdigest(), item.id))
    monkeypatch.setattr(Path, "read_bytes", lambda *args: pytest.fail("must stream, not materialize blobs"))
    result = prune(store, before=CUTOFF)
    assert set(result["deleted_ids"]) == {item.id for item in entries}
    assert result["pending_files"] == 1
    assert sum((store.root / item.content_path).is_file() for item in entries) == 1
    assert prune(store, before=CUTOFF)["pending_files"] == 0
    assert records._DERIVED_FILE_CLEANUP_MAX_BYTES == 8 * 1024 * 1024


# LLM: 默认保护集必须来自当前owner规范只读pin扫描，不创建home或只信调用方列表。
# 函数用途: 用真实恢复state引用验证自动保护与显式引用合并。
@pytest.mark.parametrize("direct_reference", [False, True])
def test_r2_default_owner_pins_protect_derived_record(tmp_path, direct_reference):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path / "home")
    store = LocalStore(home.owner_data_dir / "local_store.sqlite3")
    held = record(store, "held-auto", metadata={"status": "done", "conversation_runtime": {"task_id": "active-task"}})
    explicit = record(store, "held-explicit")
    state = home.owner_tasks_dir / "2026-10-26" / "active-task" / "work" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"task_id": "active-task", "status": "RUNNING",
                                 "source_refs": [held.id] if direct_reference else []}))
    prune(store, before=CUTOFF, protected_record_ids=[explicit.id])
    assert ids(store) == {held.id, explicit.id}


# LLM: 仅已有DB且保留主链允许执行时触发，拒绝/法律保留/未到期都不能旁路清理。
# 函数用途: 验证真正owner维护入口会调用派生TTL且不触及永久来源。
def test_r2_owner_maintenance_runs_derived_ttl_cleanup(tmp_path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due

    home = ensure_my_agent_home(tmp_path / "home")
    store = LocalStore(home.owner_data_dir / "local_store.sqlite3")
    old, permanent = record(store, "maint-old"), record(store, "maint-memory", source="memory")
    assert run_owner_retention_if_due(home, now=NOW).ran
    assert ids(store) == {permanent.id} and not (store.root / old.content_path).exists()
    marker = json.loads((home.owner_data_dir / "maintenance.json").read_text())
    assert marker["derived_records_pruned"]["deleted_ids"] == [old.id]


# LLM: 新增维护只能使用已有库；冷owner和越界链接不触发LocalStore初始化。
# 函数用途: 核实维护不会创建冷数据库或跟随不安全数据库路径。
@pytest.mark.parametrize("linked", [False, True])
def test_r2_maintenance_does_not_create_cold_or_follow_linked_database(tmp_path, linked):
    from datetime import datetime, timezone

    from agent_py_agent.agent.user_space.owner_maintenance import _prune_local_store_derived_records

    home = tmp_path / "cold"
    db = home / "data" / "local_store.sqlite3"
    if linked:
        other = LocalStore(tmp_path / "external.db")
        item = record(other, "external")
        db.parent.mkdir(parents=True)
        db.symlink_to(other.db_path)
    result = _prune_local_store_derived_records(home, now=datetime.fromtimestamp(NOW, timezone.utc))
    assert result["executed"] is False
    if linked:
        assert ids(other) == {item.id}
    else:
        assert not home.exists()


# LLM: 原主链拒绝、法律保留或未到期时，新增维护也必须完全停在原门后。
# 函数用途: 防止辅助TTL绕过保留策略或到期条件。
@pytest.mark.parametrize("reason", ["legal_hold", "invalid_policy", "not_due"])
def test_r2_owner_retention_gates_also_protect_derived_ttl(tmp_path, reason):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due

    home = ensure_my_agent_home(tmp_path / "home")
    store = LocalStore(home.owner_data_dir / "local_store.sqlite3")
    item = record(store, "must-stay")
    policy = json.loads(home.owner_retention_json.read_text())
    if reason == "legal_hold":
        policy["legal_hold"] = True
        home.owner_retention_json.write_text(json.dumps(policy))
    elif reason == "invalid_policy":
        home.owner_retention_json.write_text("{broken")
    else:
        (home.owner_data_dir / "maintenance.json").write_text(json.dumps({"last_attempt_at": NOW}))
    run_owner_retention_if_due(home, now=NOW)
    assert ids(store) == {item.id} and (store.root / item.content_path).is_file()


# LLM: 普通数据库损坏只报告未执行，不能把失败写成裁剪成功或影响原维护。
# 函数用途: 验证best-effort故障回执不破坏坏库原始字节。
def test_r2_owner_prune_reports_corrupt_database_without_raising(tmp_path):
    from datetime import datetime, timezone

    from agent_py_agent.agent.user_space.owner_maintenance import _prune_local_store_derived_records

    db = tmp_path / "data" / "local_store.sqlite3"
    db.parent.mkdir()
    db.write_bytes(b"not sqlite")
    result = _prune_local_store_derived_records(tmp_path, now=datetime.fromtimestamp(NOW, timezone.utc))
    assert result["executed"] is False and result["reason"] == "error"
    assert db.read_bytes() == b"not sqlite"


# LLM: 空字典在canonical协议里仍表示待收口，历史嵌套元数据也必须保持这条语义。
# 函数用途: 防止未携带内容的收口标记被按假值删掉。
@pytest.mark.parametrize("container", [None, "attributes", "conversation_runtime"])
def test_r2_empty_closeout_dictionary_still_protects_record(tmp_path, container):
    store = LocalStore(tmp_path / "local.db")
    pending = {"runtime_closeout_pending": {}}
    item = record(store, "empty-pending", metadata={"status": "done", **({container: pending} if container else pending)})
    assert prune(store, before=CUTOFF)["deleted_ids"] == []
    assert ids(store) == {item.id}


# LLM: 新保留上下文保持原关键词调用兼容，但不能令未知字段被静默忽略。
# 函数用途: 验证接口整理不会把拼错的保护字段当成成功保护。
def test_r2_retention_context_rejects_unknown_keyword(tmp_path):
    store = LocalStore(tmp_path / "local.db")
    item = record(store, "must-stay-on-typo")
    with pytest.raises(TypeError, match="unexpected keyword argument 'protected_ref'"):
        store.prune_derived_records(before=CUTOFF, protected_ref={item.id})
    assert ids(store) == {item.id}


# LLM: 只包裹声明的结构化容器，供真实SQL保护与关联回归复用，不修改产品规则。
# 函数用途: 为回归组装顶层或至多两层的元数据。
def r3_nested_metadata(path, fields):
    for key in reversed(path):
        fields = {key: fields}
    return fields


# LLM: 容器欠账不仅保护自身，还必须阻止同运行的终态全文被删；无关项仍可删。
# 函数用途: 复现新增容器及attributes/metadata内一层嵌套遗漏。
@pytest.mark.parametrize("path", [("wake",), ("notification",), ("closeout",), ("metadata",), ("scope",),
    ("attributes", "wake"), ("attributes", "notification"), ("attributes", "closeout"),
    ("attributes", "metadata"), ("attributes", "scope"), ("metadata", "wake"),
    ("metadata", "notification"), ("metadata", "closeout"), ("metadata", "scope"),
    ("metadata", "attributes"), ("metadata", "conversation_runtime")])
def test_r3_nested_pending_containers_protect_related_records(tmp_path, path):
    store = LocalStore(tmp_path / "local.db")
    held = record(store, "held", metadata={"status": "done", **r3_nested_metadata(path, {"wake_pending": True})})
    sibling = record(store, "sibling", source="subagent_work_log", metadata={"status": "done", "run_id": "held"})
    unrelated = record(store, "unrelated")
    assert prune(store, before=CUTOFF)["deleted_ids"] == [unrelated.id]
    assert ids(store) == {held.id, sibling.id}
    assert (store.root / held.content_path).is_file() and (store.root / sibling.content_path).is_file()


@pytest.mark.parametrize(("key", "value"), [("runtime_closeout_pending", {}), ("pending_closeout", {}),
    ("closeout_pending", {}), ("pending_closeout", True), ("closeout_pending", True),
    ("notify_pending", True), ("parent_notified", False), ("completion_notified", False),
    ("closeout_status", "pending"), pytest.param("closeout", {"closeout_pending": True}, id="nested-closeout-pending")])
def test_r3_pending_aliases_keep_terminal_record(tmp_path, key, value):
    store = LocalStore(tmp_path / "local.db")
    held = record(store, "held", metadata={"status": "done", key: value})
    assert prune(store, before=CUTOFF)["deleted_ids"] == []
    assert ids(store) == {held.id} and (store.root / held.content_path).is_file()


@pytest.mark.parametrize("key", ["closeout_status", "notification_status", "completion_notification_status", "completion_delivery_status"])
@pytest.mark.parametrize("value", ["pending", "closed", "settled", ""])
def test_r3_closeout_status_aliases_respect_settled_and_empty_states(tmp_path, key, value):
    store = LocalStore(tmp_path / "local.db")
    item = record(store, "status-alias", metadata={"status": "done", key: value})
    deleted = prune(store, before=CUTOFF)["deleted_ids"]
    assert deleted == ([] if value == "pending" else [item.id])


# LLM: 使用真实task→索引器→SQLite→TTL链路，False和空字典必须原样透传，不以bool洗掉别名语义。
# 函数用途: 覆盖canonical attributes中全部新增收口/通知/唤醒字段。
@pytest.mark.parametrize(("key", "value"), [("wake_pending", True), ("notification_pending", True),
    ("notify_pending", True), ("pending_closeout", {}), ("closeout_pending", {}),
    ("parent_notified", False), ("completion_notified", False), ("closeout_status", "pending"),
    ("notification_status", "pending")])
def test_r3_indexed_task_forwards_present_pending_attributes(tmp_path, key, value):
    manager, task = projection_case(tmp_path, "DONE", CUTOFF - 1)
    task.attributes[key] = value
    manager.indexing.index_task(task)
    with sqlite3.connect(manager.local_store.db_path) as conn:
        conn.execute("UPDATE records SET created_at=?,updated_at=?", (CUTOFF - 1, CUTOFF - 1))
    item = manager.local_store.list_recent()[0]
    assert key in item.metadata and item.metadata[key] == value
    assert prune(manager.local_store, before=CUTOFF)["deleted_ids"] == []
    assert ids(manager.local_store) == {item.id}


def test_r3_indexed_task_does_not_invent_missing_pending_attributes(tmp_path):
    manager, task = projection_case(tmp_path, "DONE", CUTOFF - 1)
    manager.indexing.index_task(task)
    item = manager.local_store.list_recent()[0]
    assert not ({"wake_pending", "notification_pending", "notify_pending", "pending_closeout", "closeout_pending",
                 "parent_notified", "completion_notified", "closeout_status", "notification_status"} & item.metadata.keys())


# LLM: 显式refs和活跃图分开运行，避免前一个断言失败掩盖另一条保护路径。
# 函数用途: 验证任务根/父和metadata/scope身份在不同来源间保护关联记录。
@pytest.mark.parametrize(("path", "key"), [((), "root_task_id"), ((), "parent_task_id"),
    (("metadata",), "run_id"), (("scope",), "task_id"),
    (("attributes", "metadata"), "root_task_id"), (("metadata", "scope"), "parent_task_id")])
@pytest.mark.parametrize("mode", ["explicit", "active"])
def test_r3_task_and_nested_identity_protects_refs_and_active_groups(tmp_path, path, key, mode):
    store = LocalStore(tmp_path / "local.db")
    held = record(store, "held", metadata={"status": "done", **r3_nested_metadata(path, {key: "protected-task"})})
    unrelated = record(store, "unrelated")
    options = {"protected_refs": {"protected-task"}}
    expected = {held.id}
    if mode == "active":
        active = record(store, "active", metadata={"status": "RUNNING", "task_id": "protected-task"}, stamp=NOW)
        expected.add(active.id)
        options = {}
    assert prune(store, before=CUTOFF, **options)["deleted_ids"] == [unrelated.id]
    assert ids(store) == expected and (store.root / held.content_path).is_file()


# LLM: 先通过真实裁剪提交两条正文欠账，缩小预算但不mock哈希或SQL；失败首项不能饿死后项。
# 函数用途: 覆盖超软预算与剩余正预算不足，以及hash冲突和unlink临时错误两种失败。
@pytest.mark.parametrize("head_size", [6, 9])
@pytest.mark.parametrize("failure", ["hash", "unlink"])
def test_r3_failed_pending_head_rotates_behind_budget_deferred_file(tmp_path, monkeypatch, head_size, failure):
    import hashlib

    from agent_py_agent.agent.local_storage import records

    store = LocalStore(tmp_path / "local.db")
    head, later = sorted([record(store, "first"), record(store, "second")], key=lambda item: item.id)
    for item, body in ((head, b"x" * head_size), (later, b"valid")):
        (store.root / item.content_path).write_bytes(body)
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("UPDATE records SET content_hash=? WHERE id=?", (hashlib.sha256(body).hexdigest(), item.id))
    with monkeypatch.context() as setup:
        setup.setattr(records, "_DERIVED_FILE_CLEANUP_MAX_FILES", 0)
        assert prune(store, before=CUTOFF)["pending_files"] == 2
    assert ids(store) == set()
    if failure == "hash":
        (store.root / head.content_path).write_bytes(b"y" * head_size)
    else:
        original = records.unlink_file_beneath

        def fail_head(root, parts):
            if parts[-1] == f"{head.id}.txt":
                raise OSError("transient unlink failure")
            return original(root, parts)

        monkeypatch.setattr(records, "unlink_file_beneath", fail_head)
    monkeypatch.setattr(records, "_DERIVED_FILE_CLEANUP_MAX_BYTES", 8)
    assert prune(store, before=CUTOFF)["pending_files"] == 2
    with sqlite3.connect(store.db_path) as conn:
        pending = json.loads(conn.execute("SELECT value FROM metadata WHERE key='derived_prune_pending_files'").fetchone()[0])
    assert [entry["id"] for entry in pending] == [later.id, head.id]
    assert prune(LocalStore(store.db_path), before=CUTOFF)["pending_files"] == 1
    assert not (store.root / later.content_path).exists()
    assert (store.root / head.content_path).is_file()


# LLM: 数量上限外的未选中项也必须先于失败项；只用真实SQL提交欠账和内容冲突，不mock清理结果。
# 函数用途: 补独立评审指出的文件数预算轮转边界，验证欠账不丢失且下一轮前进。
def test_r3_failed_head_rotates_behind_files_outside_count_limit(tmp_path, monkeypatch):
    from agent_py_agent.agent.local_storage import records

    store = LocalStore(tmp_path / "local.db")
    head, second, third = sorted([record(store, f"count-{i}") for i in range(3)], key=lambda item: item.id)
    with monkeypatch.context() as setup:
        setup.setattr(records, "_DERIVED_FILE_CLEANUP_MAX_FILES", 0)
        assert prune(store, before=CUTOFF)["pending_files"] == 3
    (store.root / head.content_path).write_text("changed hash; keep this debt")
    monkeypatch.setattr(records, "_DERIVED_FILE_CLEANUP_MAX_FILES", 1)
    assert prune(store, before=CUTOFF)["pending_files"] == 3
    with sqlite3.connect(store.db_path) as conn:
        pending = json.loads(conn.execute("SELECT value FROM metadata WHERE key='derived_prune_pending_files'").fetchone()[0])
    assert [entry["id"] for entry in pending] == [second.id, third.id, head.id]
    assert prune(LocalStore(store.db_path), before=CUTOFF)["pending_files"] == 2
    assert not (store.root / second.content_path).exists()
    assert (store.root / third.content_path).is_file() and (store.root / head.content_path).is_file()
    assert prune(store, before=CUTOFF)["pending_files"] == 1
    assert not (store.root / third.content_path).exists()
    assert (store.root / head.content_path).is_file()


# LLM: 五个真实既存SQLite覆盖兼容名及两个workspace；transition只运输原写叶，不替代TTL或维护算法。
# 函数用途: 验证单进程和多进程维护都裁剪全部owner库，永久数据保留且事件维护逐库调用。
@pytest.mark.parametrize("transition_mode", [False, True])
def test_h07_maintenance_visits_all_existing_owner_databases(tmp_path, monkeypatch, transition_mode):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due

    home = ensure_my_agent_home(tmp_path / "home")
    root = home.owner_home_dir
    paths = [root / name for name in ("data/local_store.sqlite3", "data/local_store/local.db",
        "local_store/local.db", "workspace/runtime/workspaces/a/local_store/local.db",
        "workspace/runtime/workspaces/b/local_store/local.db")]
    stores = [LocalStore(path) for path in paths]
    kept = []
    for store in stores:
        record(store, "expired")
        kept.append(record(store, "permanent", source="memory"))
    calls, commits = [], []
    original = LocalStore.maintain_events

    def maintain(store, **kwargs):
        calls.append(store.db_path)
        return original(store, **kwargs)

    def commit(operation):
        commits.append(True)
        return operation()

    monkeypatch.setattr(LocalStore, "maintain_events", maintain)
    options = {"include_global_indexes": False, "commit_transition": commit} if transition_mode else {}
    assert run_owner_retention_if_due(home, now=NOW, **options).ran
    assert set(calls) == set(paths) and len(calls) == len(paths)
    for store, permanent in zip(stores, kept, strict=True):
        assert ids(store) == {permanent.id}
        assert (store.root / permanent.content_path).is_file()
    marker = json.loads((home.owner_data_dir / "maintenance.json").read_text())
    assert marker["derived_records_pruned"]["executed"] is True
    if transition_mode:
        assert commits


# LLM: 用原task和真实重建链，顶层及attributes嵌套欠账不能被旧done时间遮蔽；不改LocalStore终态定义。
# 函数用途: 覆盖共享欠账别名和本轮明示的delivery/consume/callback pending准入。
@pytest.mark.parametrize("location", ["top", "attributes", "nested"])
@pytest.mark.parametrize(("flag", "value"), [("wake_pending", True), ("notification_pending", True),
    ("completion_notice_pending", True), ("notify_pending", True), ("pending_closeout", {}),
    ("parent_notified", False), ("notification_status", "pending"), ("delivery_pending", True),
    ("consume_pending", True), ("callback_pending", True)])
def test_h08_pending_task_projection_is_not_expired(tmp_path, monkeypatch, location, flag, value):
    from agent_py_agent.agent.subagents.services.persistence import projections

    manager, task = projection_case(tmp_path, "DONE", CUTOFF - 1)
    if location == "top":
        setattr(task, flag, value)
    elif location == "attributes":
        task.attributes[flag] = value
    else:
        task.attributes["metadata"] = {flag: value}
    monkeypatch.setattr(projections, "time", SimpleNamespace(time=lambda: NOW))
    assert not projections._task_projection_expired(task)
    projections.rebuild_derived_projections(manager, task.id)
    assert ids(manager.local_store)


# LLM: 不启用时连local_store属性也不访问；配置和显式属性均可启用，属性False优先。
# 函数用途: 验证Gateway事件镜像默认关闭且开关不会误关原请求日志路径。
@pytest.mark.parametrize(("attribute", "configured", "enabled"), [
    (None, False, False), (False, True, False), (True, False, True), (None, True, True)])
def test_holistic_gateway_event_mirror_gate(attribute, configured, enabled):
    from agent_py_agent.agent.gateway_parts import logging

    calls = []
    agent = SimpleNamespace(config=SimpleNamespace(store_event_mirror_enabled=configured),
        local_store=SimpleNamespace(log_record=lambda **kwargs: calls.append(kwargs)))
    if attribute is not None:
        agent.store_event_mirror_enabled = attribute
    logging.log_gateway_event(agent, "gateway_request_completed", {"id": "synthetic"})
    assert bool(calls) is enabled
    if enabled:
        assert calls[0]["source_type"] == "gateway_event"
        assert calls[0]["metadata"]["status"] == "completed"
