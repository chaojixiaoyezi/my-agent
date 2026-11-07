"""Rework R1 / GPT-P1-01: bounded event maintenance keeps a resumable cursor across normal appends.

Only temporary stores are used. Every scenario drives the real LocalStore step and checks durable
state (SQLite rows, events.jsonl bytes, pending ledger, cursor, staging files) afterwards.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time

import pytest

from agent_py_agent.agent.local_storage import LocalStore, maintenance
from agent_py_agent.agent.user_space.owner_maintenance import _maintain_event_store

_STEP_BUDGET = 4096
_TAIL_KEPT = 300
_TAIL_COUNT = 400
_TAIL_TARGETS = {f"seed-{index:06d}" for index in range(_TAIL_KEPT, _TAIL_COUNT)}


def _line(event_id: str, fill: str = "p", width: int = 100) -> bytes:
    return (json.dumps({"event_id": event_id, "payload": {"padding": fill * width}}) + "\n").encode()


# 函数用途: 直接写入SQLite行和同序JSONL，避免逐条fsync拖慢大样本准备。
def _seed(store: LocalStore, rows: list[tuple[str, float]], width: int) -> list[bytes]:
    with sqlite3.connect(store.db_path) as conn:
        conn.executemany("INSERT INTO events(event_id,event_type,payload_json,created_at) VALUES(?, 'seed', '{}', ?)",
                         rows)
    lines = [_line(event_id, width=width) for event_id, _created in rows]
    store.events_path.write_bytes(b"".join(lines))
    return lines


# 函数用途: 最旧的100条放在文件尾部，使已扫描前缀全是保留行，前缀被改写时输出可被区分。
def _seed_tail_targets(store: LocalStore) -> list[bytes]:
    rows = [(f"seed-{index:06d}", 1000.0 + index if index < _TAIL_KEPT else 1.0 + index - _TAIL_KEPT)
            for index in range(_TAIL_COUNT)]
    return _seed(store, rows, 100)


def _step(store: LocalStore) -> dict:
    return LocalStore(store.db_path, enable_fts=False).prune_events_step(
        before=2_000_000_000, keep_last=_TAIL_KEPT, max_delete=_TAIL_COUNT - _TAIL_KEPT, max_bytes=_STEP_BUDGET)


def _jsonl_ids(store: LocalStore) -> list[str]:
    return [json.loads(line)["event_id"] for line in store.events_path.read_bytes().splitlines()]


def _db_ids(store: LocalStore) -> list[str]:
    with sqlite3.connect(store.db_path) as conn:
        return [row[0] for row in conn.execute("SELECT event_id FROM events ORDER BY seq")]


def _metadata(store: LocalStore, key: str):
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return None if row is None else json.loads(row[0])


def _step_temps(store: LocalStore) -> list:
    return sorted(store.events_path.parent.glob(".events-prune-*.step"))


def _assert_settled(store: LocalStore) -> None:
    assert _metadata(store, "events_prune_pending_ids") is None
    assert _metadata(store, "events_prune_cursor") is None
    assert _step_temps(store) == []


# 函数用途: 经原record_event追加一条真实事件；沿用同库FTS设置，避免切换设置触发额外重建事件。
def _append(store: LocalStore, appended: list[str], tag: str) -> None:
    writer = LocalStore(store.db_path, enable_fts=store.enable_fts)
    appended.append(writer.record_event(tag, payload={"tag": tag}).event_id)


@pytest.mark.parametrize("coordinator", [False, True], ids=["local", "coordinator"])
def test_owner_event_maintenance_completes_with_one_append_per_tick(tmp_path, coordinator):
    # Cat3-R4：活动文件不逐行扫描，SQL 有界推进后原字节留待轮转。
    from agent_py_agent.tests.test_local_store_coordinator_io import Door

    directory = tmp_path / "local_store"
    store = LocalStore(directory / "local.db")
    lines = _seed(store, [(f"seed-{index:06d}", 1.0 + index * 1e-6) for index in range(20_500)], 400)
    size = store.events_path.stat().st_size
    assert size > 4 * 1024 * 1024
    budget = 256 * 1024
    bound = math.ceil(size / budget) + 4
    now = time.time() + 60
    appended, reports, offsets = [], [], []
    for tick in range(bound):
        options = {"commit_transition": Door()} if coordinator else {}
        report = _maintain_event_store(directory, now=now, db_path=store.db_path, **options)
        assert report["executed"] is True, report
        assert report["scanned_bytes"] <= budget
        reports.append(report)
        if not report["partial"]:
            break
        offsets.append(report["cursor"]["offset"])
        _append(store, appended, f"tick-{tick}")
    else:
        pytest.fail(f"bounded event maintenance did not finish within {bound} ticks")
    # 每拍之间都有一次真实追加，游标仍严格前进，从不回到0。
    assert offsets == sorted(set(offsets)) and len(offsets) == len(appended)
    assert sum(report["deleted_db"] for report in reports) == 500
    expected = [f"seed-{index:06d}" for index in range(500, 20_500)] + appended
    assert _db_ids(store) == expected
    assert _jsonl_ids(store) == [f"seed-{index:06d}" for index in range(20_500)] + appended
    assert store.events_path.read_bytes().startswith(b"".join(lines))
    _assert_settled(store)


@pytest.mark.parametrize("change", ["replace", "truncate", "regrow", "rewrite_prefix"])
def test_source_change_restarts_scan_without_reusing_stale_output(tmp_path, change):
    # Cat3-R4：没有重写游标或旧输出；外部替换/截断的活动流完整保留。
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    lines = _seed_tail_targets(store)
    for _ in range(3):
        assert not _step(store)["partial"]
    assert _metadata(store, "events_prune_cursor") is None
    assert _step_temps(store) == []
    if change == "replace":
        # 第40行位于文件头样本与边界样本之间，只能靠dev/ino身份识别换代。
        current = [*lines[:40], lines[40].replace(b"p" * 100, b"q" * 100), *lines[41:]]
        replacement = store.events_path.with_name("replacement.jsonl")
        replacement.write_bytes(b"".join(current))
        os.replace(replacement, store.events_path)
    elif change == "truncate":
        current = lines[:50]
        with store.events_path.open("r+b") as handle:
            handle.truncate(len(b"".join(current)))
    elif change == "regrow":
        current = [_line(f"regrow-{index:04d}", "r") for index in range(120)]
        with store.events_path.open("r+b") as handle:
            handle.truncate(0)
            handle.write(b"".join(current))
    else:
        current = [lines[0].replace(b"p" * 100, b"q" * 100), *lines[1:]]
        with store.events_path.open("r+b") as handle:
            handle.write(current[0])
    first = _step(store)
    assert not first["partial"] and first["cursor"] == {}
    assert _step_temps(store) == []
    for _ in range(64):
        if not _step(store)["partial"]:
            break
    else:
        pytest.fail("event maintenance did not finish after the source change")
    assert store.events_path.read_bytes() == b"".join(current)
    assert _db_ids(store) == [f"seed-{index:06d}" for index in range(100, _TAIL_COUNT)]
    _assert_settled(store)


@pytest.mark.parametrize("fault", ["publish", "final_save", "partial_save"])
def test_simulated_io_error_during_step_publication_recovers(tmp_path, monkeypatch, fault):
    # Cat3-R4：逐行发表及部分游标已退役，旧错误叶不得再被触发。
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    lines = _seed_tail_targets(store)
    failures = []
    def failing(*args, **kwargs):
        failures.append(fault)
        raise OSError("retired event rewrite")
    monkeypatch.setattr(maintenance,
        "write_private_text_file_atomic_unlocked" if fault == "publish" else "_event_step_save", failing)
    report = _step(store)
    assert report["deleted_db"] == 100 and report["deleted_jsonl"] == 0
    assert not report["partial"] and report["scanned_bytes"] == 0
    assert failures == []
    assert _db_ids(store) == [f"seed-{index:06d}" for index in range(100, _TAIL_COUNT)]
    assert store.events_path.read_bytes() == b"".join(lines)
    _assert_settled(store)


def test_append_during_unlocked_scan_is_not_blocked_and_kept_once(tmp_path, monkeypatch):
    # Cat3-R4：追加不等待活动流重写；只在原维护旁锁前交错，活动流全量保留。
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    _seed_tail_targets(store)
    original_lock = maintenance.locked_json_path
    raced: list[str] = []
    reports: list[dict] = []
    raced_tick: list[int] = []

    # 函数用途: 扫描已到达锁外EOF、尚未取JSONL锁时，从另一线程写入一条真实事件。
    def locked(path, **kwargs):
        if path == store.events_path.with_name(store.events_path.name + ".prune") and not raced:
            worker = threading.Thread(target=_append, args=(store, raced, "racing"))
            worker.start()
            worker.join(10)
            assert raced, "append was blocked while the scan ran outside the JSONL lock"
            raced_tick.append(len(reports))
        return original_lock(path, **kwargs)

    monkeypatch.setattr(maintenance, "locked_json_path", locked)
    for _ in range(64):
        reports.append(_step(store))
        assert reports[-1]["scanned_bytes"] <= _STEP_BUDGET
        if not reports[-1]["partial"]:
            break
    else:
        pytest.fail("event maintenance did not finish")
    # 新尾在同一拍剩余预算内于锁内读入，并随该拍发表完整保留一次。
    final = reports[-1]
    assert raced_tick == [len(reports) - 1]
    assert final["cursor"] == {}
    expected = [f"seed-{index:06d}" for index in range(100, _TAIL_COUNT)] + raced
    assert _jsonl_ids(store) == [f"seed-{index:06d}" for index in range(_TAIL_COUNT)] + raced
    assert _db_ids(store) == expected
    _assert_settled(store)
