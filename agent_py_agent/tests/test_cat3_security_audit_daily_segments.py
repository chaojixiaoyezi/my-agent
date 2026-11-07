"""在临时 owner 验证安全审计轮转、跨段投影与追加/压缩竞争。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.audit import logger as logger_module
from agent_py_agent.agent.audit.logger import AuditLogger
from agent_py_agent.agent.audit.paths import (
    audit_segment_day,
    audit_segment_dir,
    audit_segment_paths,
)
from agent_py_agent.agent.audit.query import AuditQuery
from agent_py_agent.agent.audit.records import AuditEntry
from agent_py_agent.agent.local_storage.owner_read_index import OwnerReadIndex
from agent_py_agent.agent.memory_store import retention_apply, retention_scan
from agent_py_agent.agent.user_space.storage_lanes import STORAGE_LANE_SLICE_BYTES

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def _home(root):
    return SimpleNamespace(owner_home_dir=root, owner_logs_dir=root / "logs",
        owner_audit_log_jsonl=root / "audit_log.jsonl", owner_compact_dir=root / "compact")


def _entry(name, *, days=0, user="alice", action="QUERY", status="success"):
    return AuditEntry(name, (NOW - timedelta(days=days)).timestamp(), action, user, "local", "task", "t", status)


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text("".join(json.dumps(row.to_dict()) + "\n" for row in rows), encoding="utf-8")
    path.chmod(0o600)


def _logger(root, relative="logs/audit/audit.jsonl"):
    return AuditLogger(SimpleNamespace(audit_log_path=relative, audit_enabled=True), root=root)


def _large(path, *, first=None, last=None, size=STORAGE_LANE_SLICE_BYTES):
    head = (json.dumps((first or _entry("first", days=2)).to_dict()) + "\n").encode()
    tail = (json.dumps((last or _entry("last", days=1)).to_dict()) + "\n").encode()
    # 稀疏中段避免为验证 rename 制造数万条无关事件；轮转只允许读取首尾窗口。
    with path.open("wb") as source:
        source.write(head)
        source.seek(size - len(tail) - 1)
        source.write(b"\n" + tail)
    path.chmod(0o600)


@pytest.mark.parametrize("relative", ["logs/audit/audit.jsonl", "audit_log.jsonl"])
def test_rotation_uses_last_day_and_preserves_inode(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(logger_module.time, "time", lambda: NOW.timestamp())
    writer = _logger(tmp_path, relative)
    path = writer._audit_file
    _large(path)
    original = path.stat()
    writer._write_to_file(_entry("new"))
    segments = audit_segment_paths(path)
    assert [segment.name for segment in segments] == [f"{path.stem}-2026-11-06.jsonl"]
    assert segments[0].stat().st_ino == original.st_ino
    assert segments[0].stat().st_size == original.st_size
    assert segments[0].stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["entry_id"] == "new"


def test_small_cross_day_and_current_day_large_file_do_not_rotate(tmp_path, monkeypatch):
    monkeypatch.setattr(logger_module.time, "time", lambda: NOW.timestamp())
    writer = _logger(tmp_path)
    path = writer._audit_file
    _write(path, [_entry("old", days=2)])
    writer._write_to_file(_entry("small"))
    assert not audit_segment_paths(path)
    _large(path, first=_entry("today"))
    inode = path.stat().st_ino
    writer._write_to_file(_entry("large"))
    assert not audit_segment_paths(path) and path.stat().st_ino == inode


def test_rotation_suffix_and_bounded_collision_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(logger_module.time, "time", lambda: NOW.timestamp())
    writer = _logger(tmp_path)
    path = writer._audit_file
    _large(path)
    sealed = path.with_name("audit-2026-11-06.jsonl")
    sealed.write_bytes(b"existing\n")
    writer._write_to_file(_entry("next"))
    assert sealed.read_bytes() == b"existing\n"
    assert path.with_name("audit-2026-11-06.1.jsonl").is_file()
    _large(path)
    monkeypatch.setattr(logger_module, "STORAGE_LANE_SLICE_ENTRIES", 2)
    inode = path.stat().st_ino
    writer._write_to_file(_entry("no_slot"))
    assert path.stat().st_ino == inode
    assert path.read_bytes().endswith((json.dumps(_entry("no_slot").to_dict(), ensure_ascii=False) + "\n").encode())


@pytest.mark.parametrize("edge", ["first", "last", "unfinished", "future", "nan"])
def test_invalid_or_future_edge_does_not_rotate(tmp_path, monkeypatch, edge):
    monkeypatch.setattr(logger_module.time, "time", lambda: NOW.timestamp())
    writer = _logger(tmp_path)
    path = writer._audit_file
    _large(path, first=_entry("future", days=-1) if edge == "future" else None)
    with path.open("r+b") as source:
        if edge == "first":
            source.write(b"broken\n")
        elif edge == "last":
            source.seek(-2, os.SEEK_END)
            source.write(b"x\n")
        elif edge == "unfinished":
            source.truncate(path.stat().st_size - 1)
        elif edge == "nan":
            source.write(b'{"timestamp":NaN}\n')
    inode = path.stat().st_ino
    writer._write_to_file(_entry("still_written"))
    assert not audit_segment_paths(path) and path.stat().st_ino == inode


def test_first_day_cache_keeps_only_latest_inode(tmp_path, monkeypatch):
    monkeypatch.setattr(logger_module.time, "time", lambda: NOW.timestamp())
    writer = _logger(tmp_path)
    path = writer._audit_file
    _large(path, first=_entry("today"))
    real = os.pread
    reads = []

    def counted(fd, amount, offset):
        reads.append((amount, offset))
        return real(fd, amount, offset)

    monkeypatch.setattr(logger_module.os, "pread", counted)
    writer._write_to_file(_entry("one"))
    writer._write_to_file(_entry("two"))
    assert reads == [(65536, 0)]
    replacement = path.with_name("replacement")
    _large(replacement)
    replacement.replace(path)
    writer._write_to_file(_entry("three"))
    assert audit_segment_paths(path)
    assert writer._first_audit_day is None
    assert all(amount <= 65536 for amount, _offset in reads)


def test_segment_paths_are_strict_regular_and_numerically_sorted(tmp_path):
    path = tmp_path / "audit.jsonl"
    names = ["audit-2026-11-05.jsonl", "audit-2026-11-06.10.jsonl", "audit-2026-11-06.2.jsonl",
             "audit-2026-11-06.jsonl", "other-2026-11-01.jsonl", "audit-2026-02-30.jsonl",
             "audit-2026-11-06.0.jsonl", "audit-2026-11-06.01.jsonl", "audit-2026-11-06.jsonl.bak"]
    for name in names:
        (tmp_path / name).write_text("{}\n")
    (tmp_path / "audit-2026-11-03.jsonl").mkdir()
    (tmp_path / "audit-2026-11-04.jsonl").symlink_to(tmp_path / names[0])
    assert audit_segment_dir(path) == tmp_path
    assert [item.name for item in audit_segment_paths(path)] == [names[0], names[3], names[2], names[1]]
    assert audit_segment_day(tmp_path / "audit-2026-02-30.jsonl") is None
    assert audit_segment_paths(tmp_path / "missing" / "audit.jsonl") == []


def test_plan_during_continuous_append_uses_bounded_prefix(tmp_path, monkeypatch):
    home = _home(tmp_path)
    writer = _logger(tmp_path)
    path = writer._audit_file
    _write(path, [_entry("old", days=200), _entry("kept")])
    original_size = path.stat().st_size
    requested, appended, stop = threading.Event(), threading.Event(), threading.Event()
    written, reads = [], []
    real = retention_scan._fingerprint_source

    def append_worker():
        while requested.wait(5):
            requested.clear()
            if stop.is_set():
                return
            row = _entry(f"append-{len(written)}")
            writer._write_to_file(row)
            written.append(row)
            appended.set()
        raise AssertionError("追加线程未收到握手")

    class RacingSource:
        def __init__(self, source):
            self.source = source

        def fileno(self):
            return self.source.fileno()

        def _read(self, method, amount):
            assert 0 < amount <= original_size
            appended.clear()
            requested.set()
            assert appended.wait(5)
            raw = getattr(self.source, method)(amount)
            reads.append(len(raw))
            return raw

        def read(self, amount):
            return self._read("read", amount)

        def readline(self, amount):
            return self._read("readline", amount)

    @contextmanager
    def racing_source(source_path):
        with real(source_path) as source:
            yield RacingSource(source)

    monkeypatch.setattr(retention_scan, "_fingerprint_source", racing_source)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(append_worker)
        try:
            errors = []
            actions = retention_scan._security_audit_actions(home, 180, NOW, errors)
        finally:
            stop.set()
            requested.set()
        future.result(timeout=5)
    assert not errors
    assert len(actions) == 1 and actions[0].operation == "security_audit_compact"
    assert actions[0].source_fingerprint == hashlib.sha256(path.read_bytes()[:original_size]).hexdigest()
    assert len(written) >= 3 and sum(reads) == 2 * original_size


@pytest.mark.parametrize("change", ["truncate", "replace"])
def test_prefix_snapshot_rejects_non_append_changes(tmp_path, monkeypatch, change):
    path = tmp_path / "audit.jsonl"
    _write(path, [_entry("old", days=200)])
    real = retention_scan._fingerprint_source

    @contextmanager
    def changing_source(source_path):
        with real(source_path) as source:
            class ChangedSource:
                def fileno(self):
                    return source.fileno()

                def readline(self, amount):
                    raw = source.readline(amount)
                    if change == "truncate":
                        path.write_bytes(b"")
                    elif change == "replace":
                        replacement = path.with_name("new")
                        replacement.write_bytes(path.read_bytes())
                        replacement.replace(path)
                    return raw

            yield ChangedSource()

    monkeypatch.setattr(retention_scan, "_fingerprint_source", changing_source)
    with pytest.raises(OSError):
        retention_scan.security_audit_content(path, NOW.timestamp())


def test_writer_chmod_before_append_does_not_invalidate_prefix(tmp_path, monkeypatch):
    home = _home(tmp_path)
    writer = _logger(tmp_path)
    path = writer._audit_file
    _write(path, [_entry("old", days=200), _entry("kept")])
    real = retention_scan._fingerprint_source

    @contextmanager
    def chmod_source(source_path):
        with real(source_path) as source:
            class ChmodSource:
                def fileno(self):
                    return source.fileno()

                def read(self, amount):
                    os.chmod(path, 0o600)
                    return source.read(amount)

                def readline(self, amount):
                    os.chmod(path, 0o600)
                    return source.readline(amount)

            yield ChmodSource()

    monkeypatch.setattr(retention_scan, "_fingerprint_source", chmod_source)
    errors = []
    assert len(retention_scan._security_audit_actions(home, 180, NOW, errors)) == 1
    assert not errors


def test_same_size_rewrite_invalidates_compaction_fingerprint(tmp_path):
    home = _home(tmp_path)
    writer = _logger(tmp_path)
    path = writer._audit_file
    _write(path, [_entry("old", days=200), _entry("kept")])
    action = retention_scan._security_audit_actions(home, 180, NOW, [])[0]
    rewritten = path.read_bytes().replace(b'"old"', b'"new"')
    path.write_bytes(rewritten)
    result = retention_apply._apply_action(home=home, action=action, now=NOW, allowed_external_roots=())
    assert result.status == "state_changed" and path.read_bytes() == rewritten


def test_compaction_holds_writer_locks_and_keeps_concurrent_appends(tmp_path, monkeypatch):
    home = _home(tmp_path)
    writer = _logger(tmp_path)
    path = writer._audit_file
    _write(path, [_entry("old", days=200), _entry("kept")])
    actions = retention_scan._security_audit_actions(home, 180, NOW, [])
    compacting, attempting, finished = threading.Event(), threading.Event(), threading.Event()
    real_write = retention_apply.write_private_text_file_atomic_unlocked
    real_lock = logger_module.path_thread_lock

    def observed_lock(key):
        attempting.set()
        return real_lock(key)

    def replacement(target, content):
        compacting.set()
        assert attempting.wait(5)
        assert not finished.is_set()
        real_write(target, content)

    def append_worker():
        assert compacting.wait(5)
        for index in range(8):
            writer._write_to_file(_entry(f"new-{index}"))
        finished.set()

    monkeypatch.setattr(logger_module, "path_thread_lock", observed_lock)
    monkeypatch.setattr(retention_apply, "write_private_text_file_atomic_unlocked", replacement)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(append_worker)
        result = retention_apply._apply_action(home=home, action=actions[0], now=NOW, allowed_external_roots=())
        future.result(timeout=5)
    assert result.status == "compacted" and finished.is_set()
    assert [json.loads(line)["entry_id"] for line in path.read_text().splitlines()] == ["kept", *[f"new-{i}" for i in range(8)]]
    assert not path.with_name("audit.jsonl.lock").exists()
    assert len(list(tmp_path.glob(".audit-*.lock"))) == 1


def test_compaction_flock_excludes_a_separate_writer_process(tmp_path, monkeypatch):
    writer = _logger(tmp_path)
    path = writer._audit_file
    _write(path, [_entry("old", days=200), _entry("kept")])
    home = _home(tmp_path)
    action = retention_scan._security_audit_actions(home, 180, NOW, [])[0]
    real_write = retention_apply.write_private_text_file_atomic_unlocked
    script = """
import sys
from types import SimpleNamespace
from agent_py_agent.agent.audit.logger import AuditLogger
from agent_py_agent.agent.audit.records import AuditEntry
writer = AuditLogger(SimpleNamespace(audit_log_path='logs/audit/audit.jsonl'), root=sys.argv[1])
try:
    writer._write_to_file(AuditEntry('child', 1794052800, 'QUERY', 'alice', 'local', 'task', 't', 'success'), blocking=False)
except BlockingIOError:
    sys.exit(23)
"""

    def child_write():
        return subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=15)

    def replacement(target, content):
        result = child_write()
        assert result.returncode == 23, result.stderr
        real_write(target, content)

    monkeypatch.setattr(retention_apply, "write_private_text_file_atomic_unlocked", replacement)
    assert retention_apply._apply_action(home=home, action=action, now=NOW, allowed_external_roots=()).status == "compacted"
    result = child_write()
    assert result.returncode == 0, result.stderr
    assert [json.loads(line)["entry_id"] for line in path.read_text().splitlines()] == ["kept", "child"]


def test_segments_expire_by_name_without_opening_content(tmp_path, monkeypatch):
    home = _home(tmp_path)
    path = home.owner_logs_dir / "audit" / "audit.jsonl"
    path.parent.mkdir(parents=True)
    old = path.with_name("audit-2026-05-10.jsonl")
    boundary = path.with_name("audit-2026-05-11.jsonl")
    recent = path.with_name("audit-2026-11-06.jsonl")
    for segment in (old, boundary, recent):
        segment.write_bytes(b"not-json\n")
    real_open = os.open
    opened = []

    def counted(name, *args, **kwargs):
        opened.append(name)
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr(retention_scan.os, "open", counted)
    errors = []
    actions = retention_scan._security_audit_actions(home, 180, NOW, errors)
    assert not errors and not opened
    assert [action.path for action in actions] == [old]
    assert actions[0].operation == "delete_file" and actions[0].category == "security_audit"
    result = retention_apply._apply_action(home=home, action=actions[0], now=NOW, allowed_external_roots=())
    assert result.status == "deleted" and not old.exists()
    assert boundary.exists() and recent.exists() and not opened
    assert not retention_scan._security_audit_actions(home, 0, NOW, [])


def test_changed_segment_is_not_deleted_and_large_active_is_not_read(tmp_path, monkeypatch):
    home = _home(tmp_path)
    writer = _logger(tmp_path)
    path = writer._audit_file
    segment = path.with_name("audit-2020-01-01.jsonl")
    segment.write_bytes(b"old\n")
    _large(path)

    def forbidden(*args, **kwargs):
        raise AssertionError("大活动文件或封存段不能打开正文")

    monkeypatch.setattr(retention_scan, "_fingerprint_source", forbidden)
    errors = []
    actions = retention_scan._security_audit_actions(home, 180, NOW, errors)
    assert not errors and len(actions) == 1
    segment.write_bytes(b"changed\n")
    assert retention_apply._apply_action(home=home, action=actions[0], now=NOW,
        allowed_external_roots=()).status == "state_changed"
    assert segment.exists()


def test_segment_delete_cannot_target_raw_curator_audit(tmp_path):
    home = _home(tmp_path)
    writer = _logger(tmp_path)
    segment = writer._audit_file.with_name("audit-2020-01-01.jsonl")
    segment.write_bytes(b"sealed\n")
    action = retention_scan._security_audit_actions(home, 180, NOW, [])[0]
    raw = tmp_path / "audit" / "audit-2020-01-01.jsonl"
    raw.parent.mkdir()
    raw.write_bytes(b"curator source\n")
    forged = replace(action, path=raw, source_fingerprint=retention_scan.security_audit_segment_fingerprint(raw))
    result = retention_apply._apply_action(home=home, action=forged, now=NOW, allowed_external_roots=())
    assert result.status == "state_changed" and raw.read_bytes() == b"curator source\n"


def _query_fixture(root):
    path = root / "logs" / "audit" / "audit.jsonl"
    sources = [path, path.with_name("audit-2026-11-06.2.jsonl"), path.with_name("audit-2026-11-05.jsonl")]
    groups = [
        [_entry("a", user="alice"), _entry("b", user="bob", status="denied")],
        [_entry("c", days=1, user="alice"), _entry("d", days=1, user="carol", action="LOGIN")],
        [_entry("e", days=2, user="dave"), _entry("f", days=2, user="alice", action="LOGIN")],
    ]
    for source, rows in zip(sources, groups, strict=True):
        _write(source, rows)
    query = AuditQuery(SimpleNamespace(audit_log_path=str(path)), root=root)
    return query, sources, [row for rows in groups for row in rows]


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 6, 7])
def test_query_pagination_totals_summary_and_recent_users(tmp_path, limit):
    query, _sources, rows = _query_fixture(tmp_path)
    first = query.query_report(limit=limit)
    found = list(first["entries"])
    cursor = first["next_cursor"]
    while cursor is not None:
        assert set(cursor) == {"segment", "cursor"}
        page = query.query_report(limit=limit, view=first["view"], after_cursor=cursor)
        assert page["coverage"]["status"] == "complete" and page["total_count"] == len(rows)
        found.extend(page["entries"])
        cursor = page["next_cursor"]
    assert [row.entry_id for row in found] == [row.entry_id for row in rows]
    for offset in range(8):
        assert query.query_report(view=first["view"], offset=offset, limit=limit)["entries"] == rows[offset:offset + limit]
    summary = query.summary()
    assert summary["total_actions"] == len(rows)
    assert summary["by_action"] == dict(Counter(row.action for row in rows))
    assert summary["by_status"] == dict(Counter(row.status for row in rows))
    assert query.summary("alice")["total_actions"] == 3
    recent = query.recent_users(3)
    expected = {}
    for row in rows:
        expected[row.user_id] = max(expected.get(row.user_id, 0), row.timestamp)
    assert recent == sorted((dict(user_id=user, last_action_time=stamp) for user, stamp in expected.items()),
        key=lambda row: row["last_action_time"], reverse=True)[:3]
    assert query.recent_users(0) == []
    assert query.query_report(limit=0)["entries"] == []


def test_legacy_cursor_and_fixed_multiview_exclude_appends(tmp_path):
    query, sources, rows = _query_fixture(tmp_path)
    views = query._capture_view()
    legacy_page = query._read_index.query(views[0], limit=1, descending=True)
    assert "segment" not in legacy_page["next_cursor"]
    _write(sources[0], [*rows[:2], _entry("new")])
    # 原地等前缀追加仍允许固定视图继续，跨段不得混入这条新事实。
    page = query.query_report(view=views, after_cursor=legacy_page["next_cursor"], limit=10)
    assert page["total_count"] == len(rows)
    assert [row.entry_id for row in page["entries"]] == [row.entry_id for row in rows[1:]]
    old_single = query.query_report(view=views[0], after_cursor=legacy_page["next_cursor"])
    assert [row.entry_id for row in old_single["entries"]] == ["b"]
    assert query.query_report(view=views, user_id="alice")["entries"] == [row for row in rows if row.user_id == "alice"]


def test_query_crosses_sql_page_limit_within_an_older_segment(tmp_path):
    query, sources, rows = _query_fixture(tmp_path)
    middle = [_entry(f"middle-{index}", days=1) for index in range(1005)]
    _write(sources[1], middle)
    expected = rows[:2] + middle + rows[-2:]
    report = query.query_report(limit=2000)
    assert report["coverage"]["status"] == "complete" and report["total_count"] == len(expected)
    assert report["entries"] == expected and report["next_cursor"] is None


def test_worst_coverage_keeps_totals_and_rollups_unknown(tmp_path):
    query, sources, _rows = _query_fixture(tmp_path)
    sources[-1].write_bytes(b"\xff\n")
    page = query.query_report(limit=1)
    assert page["coverage"]["status"] == "unavailable" and page["total_count"] is None
    assert query.summary_report()["value"] is None
    assert query.recent_users_report()["value"] is None
    assert query.query_report(after_cursor={"segment": "missing", "cursor": None})["coverage"]["reason"] == "invalid_cursor"


@pytest.mark.parametrize("view", [{"status": "complete"}, {"status": "unavailable", "reason": "source_absent"}, 1])
def test_unsigned_views_cannot_supply_coverage(tmp_path, view):
    query, _sources, _rows = _query_fixture(tmp_path)
    page = query.query_report(view=view)
    assert page["total_count"] is None and page["coverage"]["reason"] == "invalid_view"
    for report in (query.summary_report(view=view), query.recent_users_report(view=view)):
        assert report["value"] is None and report["coverage"]["status"] == "unavailable"


def test_single_sealed_view_can_be_queried(tmp_path):
    query, _sources, rows = _query_fixture(tmp_path)
    view = query._capture_view()[1]
    report = query.query_report(view=view)
    assert report["total_count"] == 2 and report["entries"] == rows[2:4]


def test_deleted_segment_forgets_cached_rows_and_checkpoint(tmp_path, monkeypatch):
    query, sources, _rows = _query_fixture(tmp_path)
    first = query.query_report()
    segment_view = first["view"][1]
    sid = segment_view["source_id"]
    database = tmp_path / "cache" / "owner_read_index.sqlite3"
    forgotten = []
    real_forget = query._read_index.forget

    def observed_forget(domain, parts):
        forgotten.append((domain, tuple(parts)))
        return real_forget(domain, parts)

    monkeypatch.setattr(query._read_index, "forget", observed_forget)
    sources[1].unlink()
    current = query.query_report()
    assert current["coverage"]["status"] == "complete" and current["total_count"] == 4
    assert forgotten == [("audit", tuple(segment_view["source_parts"]))]
    with sqlite3.connect(database) as connection:
        for table in ("ori_audit", "ori_spans", "ori_sources"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table} WHERE sid=?", (sid,)).fetchone()[0] == 0
    assert query.query_report(view=first["view"])["total_count"] is None


def test_forget_is_transactional_and_does_not_create_missing_cache(tmp_path):
    root = tmp_path / "missing"
    root.mkdir(mode=0o700)
    index = OwnerReadIndex(root)
    assert index.forget("audit", ("audit.jsonl",)) is False
    assert not (root / "cache").exists()
    query, _sources, _rows = _query_fixture(tmp_path / "owner")
    view = query.query_report()["view"][1]
    sid = view["source_id"]
    database = tmp_path / "owner" / "cache" / "owner_read_index.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TRIGGER refuse_delete BEFORE DELETE ON ori_spans BEGIN SELECT RAISE(ABORT, 'blocked'); END")
    with pytest.raises(sqlite3.IntegrityError):
        query._read_index.forget("audit", view["source_parts"])
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ori_audit WHERE sid=?", (sid,)).fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM ori_sources WHERE sid=?", (sid,)).fetchone()[0] == 1
        connection.execute("DROP TRIGGER refuse_delete")
        connection.execute("INSERT INTO ori_meta VALUES (?,?)", ("append:" + sid, "proof"))
        connection.execute("INSERT INTO ori_meta VALUES (?,?)", ("proof:" + sid, "proof"))
    assert query._read_index.forget("audit", view["source_parts"])
    assert not query._read_index.forget("audit", view["source_parts"])
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ori_meta WHERE key IN (?,?)", ("append:" + sid, "proof:" + sid)).fetchone()[0] == 0


def test_cleanup_unlinks_segments_even_when_active_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_py_agent.agent.audit.query.time.time", lambda: NOW.timestamp())
    query, sources, _rows = _query_fixture(tmp_path)
    old = sources[0].with_name("audit-2020-01-01.jsonl")
    old.write_bytes(b"unknown bytes\n")
    assert query.cleanup_old_entries(180) == 1
    assert not old.exists()
    assert sources[1].exists() and sources[2].exists()
    old.write_bytes(b"unknown bytes\n")
    sources[0].unlink()
    assert query.cleanup_old_entries(180) == 1 and not old.exists()


@pytest.mark.parametrize("trusted", [False, True])
def test_custom_path_keeps_old_writer_and_cleanup_behavior(tmp_path, monkeypatch, trusted):
    monkeypatch.setattr(logger_module.time, "time", lambda: NOW.timestamp())
    path = tmp_path / "custom.jsonl"
    writer = AuditLogger(SimpleNamespace(audit_log_path=str(path), audit_enabled=True), root=tmp_path if trusted else None)
    _large(path)
    inode = path.stat().st_ino
    writer._write_to_file(_entry("custom"))
    assert not audit_segment_paths(path) and path.stat().st_ino == inode
    segment = path.with_name("custom-2020-01-01.jsonl")
    segment.write_bytes(b"do not touch\n")
    _write(path, [_entry("old", days=200), _entry("fresh")])
    query = AuditQuery(writer.config, root=tmp_path if trusted else None)
    assert query.cleanup_old_entries(180) == 1
    assert segment.read_bytes() == b"do not touch\n"
    assert query.query_report()["total_count"] == (1 if trusted else None)


def test_dispatcher_without_explicit_root_retains_legacy_behavior(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.audit_dispatch import _logger_for, reset_for_test

    monkeypatch.setattr(logger_module.time, "time", lambda: NOW.timestamp())
    path = tmp_path / "logs" / "audit" / "audit.jsonl"
    reset_for_test()
    try:
        writer = _logger_for(SimpleNamespace(audit_log_path=str(path), audit_enabled=True))
        assert writer._trusted_root is None
        _large(path)
        inode = path.stat().st_ino
        writer._write_to_file(_entry("legacy_dispatcher"))
        assert path.stat().st_ino == inode and not audit_segment_paths(path)
    finally:
        reset_for_test()
