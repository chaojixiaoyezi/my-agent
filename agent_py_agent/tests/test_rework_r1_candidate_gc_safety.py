"""候选节点回收检查点的安全边界：当前树、重新采用、事务保护、单次I/O错误恢复与检查点自身的回收。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.conversation.store_io import immutable_node_path
from agent_py_agent.agent.memory_store import (
    candidate_node_maintenance,
    retention_scan,
    retention_scan_budget,
)
from agent_py_agent.agent.memory_store.candidates import (
    CandidateService,
    _NullAdmission,
    _write_candidates_unlocked,
)
from agent_py_agent.agent.memory_store.retention import MemoryRetentionService
from agent_py_agent.agent.user_space.owner_maintenance import _partial_progress_token
from agent_py_agent.tests.test_memory_candidates import candidate as candidate_row
from agent_py_agent.tests.test_r8_candidate_pin_stream import _ledger
from agent_py_agent.tests.test_rework_r1_candidate_gc_progress import (
    SLICE_LIMIT,
    _config,
    _current_tree,
    _orphan,
    _records,
)

KEY = "candidate:immutable_nodes"
ROWS = 512


def _service(tmp_path, rows=ROWS):
    home, path = _ledger(tmp_path, rows)
    service = CandidateService(path)
    return home, path, service, MemoryRetentionService(home_paths=home, candidates=service, config=_config())


def _checkpoints(home):
    directory = candidate_node_maintenance.workspace(home)
    return sorted(path.name for path in directory.glob("*.marks")) if directory.exists() else []


def _referenced(entry):
    return sorted(item["epoch"] + ".marks" for item in (entry.get("mark"), entry.get("proof")) if item)


def _until(retention, now, done, between=None):
    for _ in range(SLICE_LIMIT):
        report = retention.apply(now=now)
        if done(report):
            return report
        if between is not None:
            between(report)
    pytest.fail("candidate node maintenance did not reach the expected state")


def test_every_slice_keeps_current_tree_and_readopted_node_while_root_changes(tmp_path):
    home, path, service, retention = _service(tmp_path)
    directory = _records(path)
    first_root = json.loads(path.read_bytes())["root"]["ref"]["ref"]
    with service._locked_candidates():
        planned = service._load_unlocked()
        planned.append(candidate_row(ROWS))
        future_root, writes, _ = planned.plan()
    future_name = future_root["ref"]["ref"]
    future = immutable_node_path(directory, future_name)
    future.parent.mkdir(mode=0o700, exist_ok=True)
    future.write_bytes(writes[future_name])
    later = datetime.now(timezone.utc) + timedelta(days=3)
    published = False

    for _ in range(SLICE_LIMIT):
        report = retention.apply(now=later)
        entry = report.scan_cursor[KEY]
        _current_tree(path)
        mark = entry.get("mark") or {}
        if not published and mark.get("head") and not mark["pending"] and (mark["root_page"] or mark["shard_prefix"]):
            # 清扫进行中由写者重新采用这个已满龄、尚未引用的节点；之后只有可达性能保护它。
            with service._locked_candidates():
                _write_candidates_unlocked(service.path, planned, admission=_NullAdmission())
            published = True
        if published and not immutable_node_path(directory, first_root).exists():
            break
    else:
        pytest.fail("superseded root was not reclaimed after the publication")

    assert published and future.is_file()
    assert future_name in _current_tree(path)
    assert service.get(candidate_row(ROWS).candidate_id).candidate_id == candidate_row(ROWS).candidate_id


def test_pending_transaction_blocks_deletion_without_discarding_collection_progress(tmp_path):
    home, path, service, retention = _service(tmp_path)
    orphan = _orphan(_records(path), "unreferenced")
    later = datetime.now(timezone.utc) + timedelta(days=3)
    report = _until(retention, later, lambda report: (report.scan_cursor[KEY].get("mark") or {}).get("marked", 0) > 128)
    before = report.scan_cursor[KEY]["mark"]
    transaction = home.owner_memory_curator_dir / "transactions" / "prepared-run"
    transaction.mkdir(parents=True)
    (transaction / "before-000.txt").write_bytes(path.read_bytes())

    for _ in range(4):
        report = retention.apply(now=later)
        assert report.scan_cursor[KEY]["status"] == "transaction_pending"
        assert report.scan_cursor[KEY]["mark"] == before
        assert orphan.exists()
    retention_scan._CANDIDATE_NODE_MARKS.clear()
    (transaction / "before-000.txt").unlink()
    transaction.rmdir()
    report = retention.apply(now=later)
    assert report.scan_cursor[KEY]["mark"]["epoch"] == before["epoch"]
    assert report.scan_cursor[KEY]["mark"]["marked"] >= before["marked"]
    _until(retention, later, lambda report: not orphan.exists())
    _current_tree(path)


def test_quiet_passes_reuse_the_single_head_epoch_and_a_publication_rolls_it_over_once(tmp_path, monkeypatch):
    home, path, service, retention = _service(tmp_path)
    later = datetime.now(timezone.utc) + timedelta(days=3)
    loads = []
    original = candidate_node_maintenance.MarkLoad.advance

    def advance(self, *args, **kwargs):
        loads.append(self.epoch)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(candidate_node_maintenance.MarkLoad, "advance", advance)

    def complete(report):
        return report.scan_cursor[KEY]["status"] == "complete"

    first = _until(retention, later, complete).scan_cursor[KEY]["mark"]
    second = _until(retention, later, complete).scan_cursor[KEY]["mark"]
    # 只见过一个头的周期，下一轮沿用同一集合与日志，只重新清扫，不重新标记整棵树。
    assert (second["epoch"], second["marked"], second["merged"]) == (first["epoch"], first["marked"], False)
    assert first["marked"] > 128 and loads == []

    service.transition(candidate_row(0).candidate_id, "approved", reviewer="gc-safety")
    third = _until(retention, later, complete).scan_cursor[KEY]["mark"]
    fourth = _until(retention, later, complete).scan_cursor[KEY]["mark"]
    # 并入第二个头的周期结束后换一个新周期，新周期本身又只见过一个头，随后照常沿用。
    assert third["epoch"] != first["epoch"] and fourth["epoch"] == third["epoch"] and not fourth["merged"]
    assert set(_checkpoints(home)) <= set(_referenced(retention_scan_budget.load_scan_cursor(home)[KEY]))
    _current_tree(path)


def _fail_once(monkeypatch, owner, name):
    original, calls = getattr(owner, name), []

    def failing(*args, **kwargs):
        if not calls:
            calls.append(name)
            raise OSError("simulated I/O error")
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, failing)
    return calls


@pytest.mark.parametrize("stage", ["append", "read", "cursor_save", "retire"])
def test_single_io_error_keeps_committed_checkpoint_and_collection_resumes(tmp_path, monkeypatch, stage):
    home, path, service, retention = _service(tmp_path)
    orphan = _orphan(_records(path), "unreferenced")
    later = datetime.now(timezone.utc) + timedelta(days=3)
    report = _until(retention, later, lambda report: (report.scan_cursor[KEY].get("mark") or {}).get("marked", 0) > 0)
    committed = report.scan_cursor[KEY]["mark"]
    owner, name = {
        "append": (candidate_node_maintenance, "append_marks"),
        "read": (candidate_node_maintenance, "read_marks"),
        "cursor_save": (retention_scan_budget, "save_scan_cursor"),
        "retire": (candidate_node_maintenance, "retire_marks"),
    }[stage]
    calls = _fail_once(monkeypatch, owner, name)
    if stage == "read":
        retention_scan._CANDIDATE_NODE_MARKS.clear()

    if stage == "cursor_save":
        with pytest.raises(OSError, match="simulated I/O error"):
            retention.apply(now=later)
        assert retention_scan_budget.load_scan_cursor(home)[KEY]["mark"] == committed
    else:
        report = retention.apply(now=later)
        assert any("simulated I/O error" in error.message for error in report.errors)
        if stage == "append":
            assert report.scan_cursor[KEY]["mark"] == committed
    assert calls and orphan.exists()

    _until(retention, later, lambda report: not orphan.exists())
    _current_tree(path)
    entry = retention_scan_budget.load_scan_cursor(home)[KEY]
    assert set(_checkpoints(home)) <= set(_referenced(entry))


@pytest.mark.parametrize("branch", ["direct", "transition"])
def test_checkpoint_history_is_bounded_and_removed_with_the_candidate_directory(tmp_path, branch):
    home, path, service, retention = _service(tmp_path)
    start = datetime.now(timezone.utc) + timedelta(days=3)
    for day in range(3):
        _until(retention, start + timedelta(days=day), lambda report: report.scan_cursor[KEY]["status"] == "complete")
        report = retention.apply(now=start + timedelta(days=day))
        entry = report.scan_cursor[KEY]
        assert len(json.dumps(entry)) < 32 * 1024
        assert entry["proof"]["observed_at"] == (start + timedelta(days=day)).timestamp()
        retention.apply(now=start + timedelta(days=day))
        assert set(_checkpoints(home)) <= set(_referenced(retention_scan_budget.load_scan_cursor(home)[KEY]))
        assert 1 <= len(_checkpoints(home)) <= 2

    for child in _records(path).rglob("*"):
        if child.is_file():
            child.unlink()
    for child in sorted(_records(path).iterdir(), reverse=True):
        child.rmdir()
    _records(path).rmdir()
    path.unlink()
    options = {"commit_transition": lambda operation: operation()} if branch == "transition" else {}
    report = retention.apply(now=start + timedelta(days=3), **options)
    assert KEY not in report.scan_cursor
    assert KEY not in retention_scan_budget.load_scan_cursor(home)
    assert _checkpoints(home) == []


def test_only_committed_mark_or_sweep_work_changes_the_progress_token(tmp_path, monkeypatch):
    home, path, service, retention = _service(tmp_path)
    later = datetime.now(timezone.utc) + timedelta(days=3)

    def token(report):
        return _partial_progress_token(SimpleNamespace(scan_cursor={KEY: report.scan_cursor[KEY]}, actions=()), {})

    seen = {"mark": 0, "sweep": 0}
    previous = retention.apply(now=later)
    for _ in range(SLICE_LIMIT):
        report = retention.apply(now=later)
        mark = report.scan_cursor[KEY].get("mark") or {}
        before = previous.scan_cursor[KEY].get("mark") or {}
        if mark.get("epoch") == before.get("epoch") and report.scan_cursor[KEY].get("migration", {}).get("complete"):
            if mark["marked"] > before["marked"]:
                seen["mark"] += 1
                assert token(report) != token(previous)
            elif not mark["pending"] and mark["root_page"] != before["root_page"]:
                seen["sweep"] += 1
                assert token(report) != token(previous)
        if seen["mark"] and seen["sweep"]:
            break
        previous = report
    assert seen["mark"] and seen["sweep"]

    def busy(target, **options):
        # 确定性的锁竞争：只让候选维护片的非阻塞取锁失败，其余调用保持原锁。
        if target == path and options.get("blocking") is False:
            raise BlockingIOError("simulated lock contention")
        return locked_json_path(target, **options)

    monkeypatch.setattr(retention_scan, "locked_json_path", busy)
    blocked = retention.apply(now=later)
    assert blocked.scan_partial
    assert blocked.scan_cursor[KEY] == report.scan_cursor[KEY]
    assert token(blocked) == token(report)
