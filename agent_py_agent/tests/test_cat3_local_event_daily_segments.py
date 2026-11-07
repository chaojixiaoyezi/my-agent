"""本地事件只在原锁内按天 rename，保留字节、SQL 事实和有界写入成本。"""
from __future__ import annotations

import json
import os
import stat
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.global_write_commit import global_write_commit_scope
from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.local_storage import events as events_module
from agent_py_agent.agent.local_storage import schema as schema_module
from agent_py_agent.agent.local_storage.events import (
    event_segments_dir,
    iter_event_jsonl_paths,
    rotate_event_jsonl_if_due_unlocked,
)
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.memory_archive._storage_dates import _date_key
from agent_py_agent.agent.user_space.storage_lanes import (
    STORAGE_LANE_SLICE_BYTES,
    STORAGE_LANE_SLICE_ENTRIES,
)


@pytest.fixture(autouse=True)
def event_clock(monkeypatch):
    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 7)

    now = datetime(2026, 11, 7, 12, tzinfo=timezone.utc).timestamp()
    # 只替换事件模块的时钟引用，不污染锁和其它测试使用的全局 time 模块。
    monkeypatch.setattr(events_module, "date", Today)
    monkeypatch.setattr(events_module, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(events_module, "_event_first_day_cache", {})


def _line(record):
    return (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _seed_large(path, *, first=None, last=None, size=STORAGE_LANE_SLICE_BYTES, partial=b""):
    first = _line({"created_at": "2026-11-05", "event_type": "first"}) if first is None else first
    last = _line({"created_at": "2026-11-06", "event_type": "last"}) if last is None else last
    middle = {"created_at": "2026-11-05", "payload": ""}
    middle["payload"] = "x" * (size - len(first) - len(_line(middle)) - len(last) - len(partial))
    content = first + _line(middle) + last + partial
    assert len(content) == size
    path.write_bytes(content)
    path.chmod(0o600)
    return content


def _rotate(path):
    with locked_json_path(path):
        return rotate_event_jsonl_if_due_unlocked(path)


def _sql_events(store):
    with store._connection() as conn:
        return [tuple(row) for row in conn.execute("SELECT event_id, event_type FROM events ORDER BY seq")]


def test_missing_and_small_cross_day_active_files_do_not_rotate(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    assert not event_segments_dir(store.events_path).exists()
    assert _rotate(store.events_path) is None
    original = _line({"created_at": "2026-11-05"})
    store.events_path.write_bytes(original)
    event = store.record_event("small")
    assert store.events_path.read_bytes().startswith(original)
    assert json.loads(store.events_path.read_bytes().splitlines()[-1])["event_id"] == event.event_id
    assert not event_segments_dir(store.events_path).exists()


@pytest.mark.parametrize("initial_mode", [0o600, 0o644])
def test_cross_day_threshold_renames_inode_and_preserves_sql_rows(tmp_path, initial_mode):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    before = store.record_event("before")
    original = _seed_large(store.events_path)
    store.events_path.chmod(initial_mode)
    original_inode = store.events_path.stat().st_ino
    lock = store.events_path.with_name(store.events_path.name + ".lock")
    lock_inode = lock.stat().st_ino

    after = store.record_event("after", payload={"正文": "新事件"})

    sealed = event_segments_dir(store.events_path) / "2026-11-06.jsonl"
    assert sealed.read_bytes() == original
    assert sealed.stat().st_ino == original_inode
    assert store.events_path.stat().st_ino != original_inode
    assert lock.stat().st_ino == lock_inode
    assert json.loads(store.events_path.read_text(encoding="utf-8"))["event_id"] == after.event_id
    assert _sql_events(store) == [(before.event_id, "before"), (after.event_id, "after")]
    assert [event.event_id for event in store.timeline()] == [after.event_id, before.event_id]
    if os.name != "nt":
        assert stat.S_IMODE(sealed.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(sealed.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.events_path.stat().st_mode) == 0o600


def test_today_large_file_stays_active_and_reuses_first_day_cache(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    original = _seed_large(path, first=_line({"created_at": "2026-11-07"}))
    assert _rotate(path) is None

    def forbidden_open(*args, **kwargs):
        raise AssertionError("今日首行已缓存，不应重复读取活动文件")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", forbidden_open)
        assert _rotate(path) is None
    assert path.read_bytes() == original
    assert not event_segments_dir(path).exists()


def test_cached_first_day_is_rechecked_after_midnight(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    _seed_large(path, first=_line({"created_at": "2026-11-07"}), last=_line({"created_at": "2026-11-07"}))
    assert _rotate(path) is None

    class Tomorrow(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 8)

    monkeypatch.setattr(events_module, "date", Tomorrow)
    assert _rotate(path) == event_segments_dir(path) / "2026-11-07.jsonl"


def test_first_day_cache_tracks_only_latest_inode(tmp_path):
    paths = [tmp_path / name for name in ("first.jsonl", "second.jsonl")]
    for path in paths:
        _seed_large(path, first=_line({"created_at": "2026-11-07"}))
        assert _rotate(path) is None
        info = path.stat()
        assert events_module._event_first_day_cache == {(info.st_dev, info.st_ino): "2026-11-07"}
    paths[0].rename(tmp_path / "removed.jsonl")
    _seed_large(paths[0])
    assert _rotate(paths[0]) == event_segments_dir(paths[0]) / "2026-11-06.jsonl"


def test_collision_uses_suffix_without_overwriting_existing_segment(tmp_path):
    path = tmp_path / "events.jsonl"
    original = _seed_large(path)
    segments = event_segments_dir(path)
    segments.mkdir(mode=0o700)
    existing = segments / "2026-11-06.jsonl"
    existing.write_bytes(b"keep\n")
    assert _rotate(path) == segments / "2026-11-06.1.jsonl"
    assert existing.read_bytes() == b"keep\n"
    assert (segments / "2026-11-06.1.jsonl").read_bytes() == original


def test_collision_attempts_are_bounded_without_directory_scan(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    original = _seed_large(store.events_path)
    segments = event_segments_dir(store.events_path)
    segments.mkdir(mode=0o700)
    for number in range(STORAGE_LANE_SLICE_ENTRIES + 1):
        suffix = f".{number}" if number else ""
        (segments / f"2026-11-06{suffix}.jsonl").write_bytes(b"keep\n")
    probes = []
    actual_lexists = os.path.lexists

    def lexists(path):
        if Path(path).parent == segments:
            probes.append(path)
        return actual_lexists(path)

    def forbidden_scan(*args, **kwargs):
        raise AssertionError("轮转不能枚举历史封存目录")

    monkeypatch.setattr(events_module.os.path, "lexists", lexists)
    monkeypatch.setattr(events_module.os, "scandir", forbidden_scan)
    event = store.record_event("collision_full")
    assert len(probes) == STORAGE_LANE_SLICE_ENTRIES + 1
    assert store.events_path.read_bytes().startswith(original)
    assert _sql_events(store) == [(event.event_id, "collision_full")]


@pytest.mark.parametrize("edge", ["first", "last"])
@pytest.mark.parametrize("bad_line", [
    b"broken json\n",
    b"\xff\n",
    b"[]\n",
    b"{}\n",
    b'{"created_at": false}\n',
    b'{"created_at": "bad-date"}\n',
    b"[" * 2000 + b"0" + b"]" * 2000 + b"\n",
    _line({"created_at": "2026-11-05", "payload": "x" * (64 * 1024)}),
])
def test_bad_or_oversized_boundary_line_keeps_active_and_allows_append(tmp_path, edge, bad_line):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    original = _seed_large(store.events_path, **{edge: bad_line})
    event = store.record_event("boundary_bad")
    content = store.events_path.read_bytes()
    assert content.startswith(original)
    assert json.loads(content[len(original):])["event_id"] == event.event_id
    assert _sql_events(store) == [(event.event_id, "boundary_bad")]
    assert not event_segments_dir(store.events_path).exists()


def test_tail_uses_last_complete_line_without_losing_partial_bytes(tmp_path):
    path = tmp_path / "events.jsonl"
    original = _seed_large(path, partial=b'{"created_at": "2026-11-07"')
    sealed = _rotate(path)
    assert sealed == event_segments_dir(path) / "2026-11-06.jsonl"
    assert sealed.read_bytes() == original


@pytest.mark.parametrize("created_at", [
    1793923200,
    1793923200.5,
    "2026-11-06",
    "2026-11-06T23:45:00Z",
    "2026-11-06T23:45:00-08:00",
    "2026-11-06T12:00:00",
])
def test_segment_dates_reuse_archive_date_semantics(tmp_path, created_at):
    path = tmp_path / "events.jsonl"
    _seed_large(path, last=_line({"created_at": created_at}))
    assert _rotate(path).name == f"{_date_key(created_at)}.jsonl"


def test_explicit_iteration_orders_regular_segments_then_active(tmp_path):
    path = tmp_path / "custom.audit.jsonl"
    assert event_segments_dir(path) == tmp_path / "custom.audit-daily"
    assert iter_event_jsonl_paths(path) == []
    path.write_bytes(b"active\n")
    assert iter_event_jsonl_paths(path) == [path]
    segments = event_segments_dir(path)
    segments.mkdir()
    names = ["2026-11-06.jsonl", "2026-11-05.jsonl", "2026-11-06.1.jsonl"]
    for name in names:
        (segments / name).write_bytes(b"sealed\n")
    (segments / "ignored.txt").write_bytes(b"ignored\n")
    (segments / "directory.jsonl").mkdir()
    expected = [segments / name for name in sorted(names)]
    assert iter_event_jsonl_paths(path) == [*expected, path]
    path.unlink()
    assert iter_event_jsonl_paths(path) == expected


def test_deleted_segment_directory_is_recreated_on_next_rotation(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    _seed_large(store.events_path)
    store.record_event("first_rotation")
    segments = event_segments_dir(store.events_path)
    (segments / "2026-11-06.jsonl").unlink()
    segments.rmdir()
    original = _seed_large(store.events_path)
    event = store.record_event("recreated")
    assert (segments / "2026-11-06.jsonl").read_bytes() == original
    assert json.loads(store.events_path.read_text(encoding="utf-8"))["event_id"] == event.event_id


def test_coordinator_rotates_inside_original_write_gate_and_rejects_other_path(tmp_path, monkeypatch):
    inside = False

    def door(operation):
        nonlocal inside
        assert not inside
        inside = True
        try:
            return operation()
        finally:
            inside = False

    with global_write_commit_scope(door):
        store = LocalStore(tmp_path / "local.db", enable_fts=False, coordinator_io=True)
    original = _seed_large(store.events_path)
    inode = store.events_path.stat().st_ino
    observed = []
    actual_rotate = schema_module.rotate_event_jsonl_if_due_unlocked

    def rotate(path):
        assert inside
        assert path == store.events_path
        observed.append(path)
        return actual_rotate(path)

    monkeypatch.setattr(schema_module, "rotate_event_jsonl_if_due_unlocked", rotate)
    with global_write_commit_scope(door):
        record = store.upsert_record(source_type="note", source_id="one", title="事件", content="正文")
        assert store.get_record(record.id) is not None
        assert len(_sql_events(store)) == 1
    sealed = event_segments_dir(store.events_path) / "2026-11-06.jsonl"
    assert observed == [store.events_path]
    assert sealed.read_bytes() == original
    assert sealed.stat().st_ino == inode
    assert json.loads(store.events_path.read_text(encoding="utf-8"))["record_id"] == record.id
    with pytest.raises(ValueError, match="^local_store_event_path_changed$"):
        store._append_coordinator_event_records(tmp_path / "other.jsonl", [{"created_at": 1}])
    assert not (tmp_path / "other.jsonl").exists()


@pytest.mark.parametrize("coordinator", [False, True])
def test_original_event_lock_stays_held_between_rename_and_append(tmp_path, monkeypatch, coordinator):
    def door(operation):
        return operation()

    with global_write_commit_scope(door):
        store = LocalStore(tmp_path / "local.db", enable_fts=False, coordinator_io=coordinator)
    _seed_large(store.events_path)
    renamed, release, done = threading.Event(), threading.Event(), threading.Event()
    errors = []
    module = schema_module if coordinator else events_module
    actual_rotate = module.rotate_event_jsonl_if_due_unlocked

    def rotate(path):
        result = actual_rotate(path)
        assert result is not None
        renamed.set()
        assert release.wait(5), "测试未释放已封存的写入者"
        return result

    def writer():
        try:
            with global_write_commit_scope(door):
                if coordinator:
                    store.upsert_record(source_type="note", source_id="locked", title="锁", content="正文")
                else:
                    store.record_event("locked")
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    monkeypatch.setattr(module, "rotate_event_jsonl_if_due_unlocked", rotate)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert renamed.wait(5), "写入者未到达真实 rename 后的交错点"
        assert not store.events_path.exists()
        # 从另一个线程取得原路径锁必须立即失败，不能把锁随活动 inode 移走。
        with pytest.raises(BlockingIOError), locked_json_path(store.events_path, blocking=False):
            pass
    finally:
        release.set()
        thread.join(5)
    assert done.is_set()
    assert errors == []
    assert len(store.events_path.read_bytes().splitlines()) == 1
