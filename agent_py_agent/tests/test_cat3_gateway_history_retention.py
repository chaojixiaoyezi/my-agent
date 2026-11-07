"""历史日段追加、断点重放及owner留存通道的真实SQLite/文件回归。"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from agent_py_agent.agent.gateway_parts import gateway_history_index as history
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.memory_archive._storage_dates import _date_key
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent
from agent_py_agent.agent.user_space.storage_lanes import (
    StorageLaneBudget,
    StorageLaneRequest,
    validate_lane_receipt,
)


@pytest.fixture
def clock(monkeypatch):
    now = datetime(2026, 11, 7, 12, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(history.time, "time", lambda: now)
    return now


def _index(root, *, daily=True):
    index = history.GatewayHistoryIndex(gateway_paths_from_root(root))
    if daily:
        # 大多数用例直接准备兼容期已满事实；边界测试仍经真实128次追加跨过热段。
        with index.connection() as conn:
            index._set_meta(conn, "compat_count", 128)
    return index


def _row(index, request_id):
    with index.connection() as conn:
        return dict(conn.execute("SELECT rowid,* FROM history WHERE request_id=?", (request_id,)).fetchone())


def _payload(request_id, ended, status="done"):
    return {"id": request_id, "status": status, "ended_at": ended, "response": "中文正文"}


def _export_at(index, request_id, export_path):
    """构造历史版本的导出定位，不将测试修补引入生产迁移路径。"""
    row = _row(index, request_id)
    target = index.paths.root / export_path
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(row["body"] + "\n")
    with index.connection() as conn:
        conn.execute("UPDATE history SET export_path=? WHERE request_id=?", (export_path, request_id))
    return target


def test_after_compat_window_exports_append_to_one_daily_segment(tmp_path, clock):
    index = _index(tmp_path, daily=False)
    for number in range(128):
        assert index.append({"id": f"compat-{number}"})
    hot = index.paths.history.read_bytes()
    payloads = [_payload(f"daily-{number}", clock) for number in range(3)]
    offset = 0
    for payload in payloads:
        assert index.append(payload)
        row = _row(index, payload["id"])
        assert row["export_mode"] == "append"
        assert row["export_path"] == f"history_segments/{_date_key(clock)}.jsonl"
        assert row["export_offset"] == offset
        offset += len((row["body"] + "\n").encode("utf-8"))
        assert index.get(payload["id"]) == payload
    segment = index.paths.root / row["export_path"]
    assert segment.stat().st_size == offset
    assert [json.loads(line) for line in segment.read_text().splitlines()] == payloads
    assert list(segment.parent.glob("*.jsonl")) == [segment]
    assert index.page(after="compat-z") == payloads
    assert index.paths.history.read_bytes() == hot


def test_same_request_updates_append_versions_and_get_latest(tmp_path, clock):
    index = _index(tmp_path)
    first = _payload("same", clock)
    second = {**first, "response": "新的最终正文"}
    assert index.append(first)
    segment = index.paths.root / _row(index, "same")["export_path"]
    prefix = segment.read_bytes()
    assert index.append(second)
    assert _row(index, "same")["export_offset"] == len(prefix)
    assert segment.read_bytes().startswith(prefix)
    before = segment.read_bytes()
    assert index.append(second) is False
    assert segment.read_bytes() == before
    assert [json.loads(line) for line in before.decode().splitlines()] == [first, second]
    assert history.GatewayHistoryIndex(index.paths).get("same") == second
    assert index.page(after="") == [second]


def test_new_day_uses_new_segment_without_migrating_older_exports(tmp_path, clock, monkeypatch):
    index = _index(tmp_path)
    index.append(_payload("same", clock))
    original_path = index.paths.root / _row(index, "same")["export_path"]
    before = original_path.read_bytes()
    monkeypatch.setattr(history.time, "time", lambda: clock + 86400)
    latest = _payload("same", clock + 86400)
    assert index.append(latest)
    row = _row(index, "same")
    assert row["export_path"] == f"history_segments/{_date_key(clock + 86400)}.jsonl"
    assert row["export_offset"] == 0 and row["export_mode"] == "append"
    assert original_path.read_bytes() == before
    assert index.get("same") == latest


@pytest.mark.parametrize("written", ["none", "partial", "complete", "different-tail"])
def test_daily_export_replays_at_durable_offset(tmp_path, clock, monkeypatch, written):
    index = _index(tmp_path)
    first, pending, last = [_payload(name, clock) for name in ("first", "pending", "last")]
    index.append(first)
    segment = index.paths.root / _row(index, "first")["export_path"]
    prefix = segment.read_bytes()
    original = history.append_private_jsonl_records

    def interrupted(path, rows, **kwargs):
        data = (json.dumps(rows[0], ensure_ascii=False, sort_keys=True) + "\n").encode()
        if written == "complete":
            original(path, rows, **kwargs)
        elif written != "none":
            with path.open("ab") as handle:
                handle.write(data[:17] if written == "partial" else b"broken tail")
        raise OSError("模拟导出中断")

    with monkeypatch.context() as patch:
        patch.setattr(history, "append_private_jsonl_records", interrupted)
        with pytest.raises(OSError, match="模拟导出中断"):
            index.append(pending)
    assert _row(index, "pending")["export_pending"] == 1
    assert _row(index, "pending")["export_offset"] == len(prefix)
    # 重开对象先重放原意图，再接纳下一个请求；不重复完整行，不破坏前缀。
    reopened = history.GatewayHistoryIndex(index.paths)
    assert reopened.get("pending") == pending
    assert reopened.append(last)
    assert segment.read_bytes().startswith(prefix)
    assert [json.loads(line) for line in segment.read_text().splitlines()] == [first, pending, last]
    assert _row(index, "pending")["export_pending"] == 0


def test_daily_replay_does_not_fill_a_missing_committed_prefix(tmp_path, clock, monkeypatch):
    index = _index(tmp_path)
    index.append(_payload("first", clock))
    segment = index.paths.root / _row(index, "first")["export_path"]

    def fail(*args, **kwargs):
        raise OSError("导出未写")

    with monkeypatch.context() as patch:
        patch.setattr(history, "append_private_jsonl_records", fail)
        with pytest.raises(OSError):
            index.append(_payload("pending", clock))
    segment.unlink()
    with pytest.raises(ValueError, match="prefix is missing"):
        index.append(_payload("next", clock))
    assert not segment.exists()
    assert _row(index, "pending")["export_pending"] == 1


def test_old_single_request_export_is_read_without_rewrite_then_pruned(tmp_path, clock):
    index = _index(tmp_path)
    payload = _payload("old", clock - 90 * 86400)
    index.append(payload)
    name = hashlib.sha256(b"old").hexdigest() + ".jsonl"
    old_file = _export_at(index, "old", "history_segments/" + name)
    with index.connection() as conn:
        conn.execute("UPDATE history SET export_mode='replace' WHERE request_id='old'")
    before = old_file.read_bytes()
    assert index.get("old") == payload
    assert index.page(after="") == [payload]
    assert index.append(payload) is False
    assert old_file.read_bytes() == before
    report = index.prune_history_step(cutoff=clock - 30 * 86400, max_rows=8)
    assert report["items"] == 1 and report["complete"] and report["cursor"] == 0
    assert index.get("old") is None and not old_file.exists()


@pytest.mark.parametrize("status", ["done", "failed", "cancelled", "interrupted"])
def test_prune_accepts_only_actual_response_terminal_states(tmp_path, clock, status):
    index = _index(tmp_path)
    index.append(_payload("old", clock - 60 * 86400, status))
    index.prune_history_step(cutoff=clock - 30 * 86400, max_rows=8)
    assert index.get("old") is None


@pytest.mark.parametrize("changes", [
    {"status": "running"}, {"status": "unknown"}, {"status": "DONE"}, {"status": "completed"},
    {"status": ["done"]}, {"status": None}, {"ended_at": "2020-01-01T00:00:00Z"},
    {"ended_at": None}, {"ended_at": True}, {"ended_at": 0}, {"ended_at": -1},
    {"ended_at": float("nan")}, {"ended_at": float("inf")}, {"ended_at": 10 ** 400},
])
def test_prune_keeps_unknown_status_or_time(tmp_path, clock, changes):
    index = _index(tmp_path)
    index.append({**_payload("keep", clock - 60 * 86400), **changes})
    assert index.prune_history_step(cutoff=clock - 30 * 86400, max_rows=8)["complete"]
    assert _row(index, "keep")


def test_prune_keeps_recent_boundary_malformed_and_pending_rows(tmp_path, clock):
    index = _index(tmp_path)
    cutoff = clock - 30 * 86400
    for name, ended in (("recent", clock), ("boundary", cutoff), ("bad", 1), ("scalar", 1), ("pending", 1)):
        index.append(_payload(name, ended))
    with index.connection() as conn:
        conn.execute("UPDATE history SET body='{bad json' WHERE request_id='bad'")
        conn.execute("UPDATE history SET body='[]' WHERE request_id='scalar'")
        conn.execute("UPDATE history SET export_pending=1 WHERE request_id='pending'")
    report = index.prune_history_step(cutoff=cutoff, max_rows=8)
    assert report["items"] == 5 and report["complete"]
    with index.connection() as conn:
        assert conn.execute("SELECT count(*) FROM history").fetchone()[0] == 5


def test_daily_segment_is_removed_only_after_its_last_row_except_today(tmp_path, clock):
    index = _index(tmp_path)
    for name in ("old-a", "old-b", "today"):
        index.append(_payload(name, clock - 60 * 86400))
    today_segment = index.paths.root / _row(index, "today")["export_path"]
    old_path = f"history_segments/{_date_key(clock - 86400)}.jsonl"
    segment = _export_at(index, "old-a", old_path)
    _export_at(index, "old-b", old_path)
    first = index.prune_history_step(cutoff=clock - 30 * 86400, max_rows=1)
    assert first["items"] == 1 and first["complete"] is False and segment.exists()
    second = index.prune_history_step(cutoff=clock - 30 * 86400, max_rows=1)
    assert second["items"] == 1 and second["complete"] is False and not segment.exists()
    last = index.prune_history_step(cutoff=clock - 30 * 86400, max_rows=1)
    assert last["complete"] and last["cursor"] == 0 and today_segment.exists()


def test_recent_row_keeps_older_daily_segment_and_linked_leaf_is_rejected(tmp_path, clock):
    index = _index(tmp_path)
    index.append(_payload("expired", 1))
    index.append(_payload("recent", clock))
    old_path = "history_segments/2020-01-01.jsonl"
    segment = _export_at(index, "expired", old_path)
    _export_at(index, "recent", old_path)
    index.prune_history_step(cutoff=clock - 30 * 86400, max_rows=8)
    assert index.get("expired") is None and segment.exists()
    original = tmp_path / "original"
    segment.rename(original)
    segment.symlink_to(original)
    before = original.read_bytes()
    with pytest.raises((ValueError, OSError)):
        index.prune_history_step(cutoff=clock + 1, max_rows=8)
    assert index.get("recent") is not None and original.read_bytes() == before


def test_prune_and_concurrent_update_keep_latest_response(tmp_path, clock, monkeypatch):
    index = _index(tmp_path)
    index.append(_payload("same", 1))
    _export_at(index, "same", "history_segments/2020-01-01.jsonl")
    entered, release, updating = threading.Event(), threading.Event(), threading.Event()
    original = history._expired_history_body
    failures = []

    def pause(body, cutoff):
        entered.set()
        assert release.wait(5)
        return original(body, cutoff)

    def prune():
        try:
            index.prune_history_step(cutoff=clock - 30 * 86400, max_rows=1)
        except BaseException as error:
            failures.append(error)

    latest = _payload("same", clock)

    def update():
        try:
            updating.set()
            history.GatewayHistoryIndex(index.paths).append(latest)
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(history, "_expired_history_body", pause)
    pruning, writer = threading.Thread(target=prune), threading.Thread(target=update)
    pruning.start()
    try:
        assert entered.wait(5)
        writer.start()
        assert updating.wait(5)
    finally:
        release.set()
        pruning.join(10)
        if writer.ident is not None:
            writer.join(10)
    assert not pruning.is_alive() and not writer.is_alive() and failures == []
    assert index.get("same") == latest
    segment = index.paths.root / _row(index, "same")["export_path"]
    assert json.loads(segment.read_text().splitlines()[-1]) == latest


def test_rowid_cursor_survives_reopen_and_does_not_skip_retained_rows_next_pass(tmp_path, clock):
    index = _index(tmp_path)
    for number in range(5):
        index.append(_payload(str(number), clock if number % 2 else 1))
    cutoff = clock - 30 * 86400
    first = index.prune_history_step(cutoff=cutoff, max_rows=2)
    assert first["cursor"] == 2 and first["items"] == 2 and first["bytes"] > 0
    assert first["complete"] is False
    reopened = history.GatewayHistoryIndex(index.paths)
    second = reopened.prune_history_step(cutoff=cutoff, max_rows=2)
    assert second["cursor"] == 4 and second["items"] == 2 and second["complete"] is False
    third = reopened.prune_history_step(cutoff=cutoff, max_rows=2)
    assert third["cursor"] == 0 and third["items"] == 1 and third["complete"]
    assert {row["id"] for row in reopened.page(after="")} == {"1", "3"}
    assert reopened.prune_history_step(cutoff=clock + 1, max_rows=2)["complete"]
    assert reopened.page(after="") == []


def test_zero_rows_does_not_prune_and_compat_export_is_untouched(tmp_path, clock):
    index = _index(tmp_path, daily=False)
    index.append(_payload("compat", 1))
    before = index.paths.history.read_bytes()
    assert index.prune_history_step(cutoff=clock, max_rows=0)["items"] == 0
    assert index.get("compat") is not None
    index.prune_history_step(cutoff=clock, max_rows=1)
    assert index.get("compat") is None
    assert index.paths.history.read_bytes() == before


def test_cleanup_rejects_linked_segment_parent_and_rolls_back_rows(tmp_path, clock):
    index = _index(tmp_path / "gateway")
    index.append(_payload("old", 1))
    old_path = "history_segments/2020-01-01.jsonl"
    segment = _export_at(index, "old", old_path)
    outside = tmp_path / "outside"
    segment.parent.rename(outside)
    segment.parent.symlink_to(outside, target_is_directory=True)
    before = (outside / segment.name).read_bytes()
    with pytest.raises((ValueError, OSError)):
        index.prune_history_step(cutoff=clock, max_rows=1)
    assert (outside / segment.name).read_bytes() == before
    assert index.get("old") is not None
    with index.connection() as conn:
        assert index._meta(conn, "retention_cursor", "0") == "0"


def _request(tmp_path, clock, *, items=128, days=30, hold=False, commits=None):
    home = ensure_my_agent_home(tmp_path / "home")
    home.owner_retention_json.write_text(json.dumps({"audit_days": days}), encoding="utf-8")

    def commit(operation):
        commits.append(True)
        return operation()

    return StorageLaneRequest("gateway_history", home, clock, {}, StorageLaneBudget(items, None, None),
                              hold, commits is not None, None, commit if commits is not None else None)


def _lane_index(request):
    root = runtime_paths_for_agent(None, request.home.owner_home_dir, request.home)["gateway_workspace"]
    return _index(root)


def test_cold_owner_lane_never_creates_gateway_root(tmp_path, clock):
    request = _request(tmp_path, clock)
    root = runtime_paths_for_agent(None, request.home.owner_home_dir, request.home)["gateway_workspace"]
    assert not root.exists()
    assert validate_lane_receipt(history.maintain_gateway_history_lane(request)) == {
        "cursor": {}, "complete": True, "items": 0, "bytes": 0}
    assert not root.exists()


def test_existing_empty_gateway_lane_does_not_initialize_database(tmp_path, clock):
    request = _request(tmp_path, clock)
    root = runtime_paths_for_agent(None, request.home.owner_home_dir, request.home)["gateway_workspace"]
    root.mkdir(parents=True)
    report = validate_lane_receipt(history.maintain_gateway_history_lane(request))
    assert report["complete"] and report["items"] == 0 and list(root.iterdir()) == []


@pytest.mark.parametrize("days,hold", [(0, False), (-1, False), (30, True)])
def test_lane_respects_disabled_policy_and_legal_hold(tmp_path, clock, days, hold):
    request = _request(tmp_path, clock, days=days, hold=hold)
    index = _lane_index(request)
    index.append(_payload("old", 1))
    report = validate_lane_receipt(history.maintain_gateway_history_lane(request))
    assert report["complete"] and report["items"] == 0
    assert index.get("old") is not None


def test_lane_owner_policy_budget_write_gate_and_resume(tmp_path, clock):
    commits = []
    request = _request(tmp_path, clock, items=1, commits=commits)
    index = _lane_index(request)
    index.append(_payload("expired", clock - 60 * 86400))
    index.append(_payload("recent", clock - 20 * 86400))
    first = validate_lane_receipt(history.maintain_gateway_history_lane(request))
    assert first["items"] == 1 and first["complete"] is False
    assert index.get("expired") is None and index.get("recent") is not None
    # request游标仅是投影；新对象的续扫事实来自数据库meta，不重头扫描。
    second = validate_lane_receipt(history.maintain_gateway_history_lane(replace(request, cursor={})))
    assert second["items"] == 1 and second["complete"] and second["cursor"] == {"retention_cursor": 0}
    assert len(commits) == 2
    assert index.get("recent") is not None


@pytest.mark.parametrize("expired_time", [False, True])
def test_lane_does_not_write_when_items_or_time_budget_exhausted(tmp_path, clock, expired_time):
    request = _request(tmp_path, clock, items=0 if not expired_time else 128, commits=[])
    if expired_time:
        request = replace(request, budget=StorageLaneBudget(128, None, time.monotonic() - 1))
    index = _lane_index(request)
    index.append(_payload("old", 1))
    report = validate_lane_receipt(history.maintain_gateway_history_lane(request))
    assert report["items"] == 0 and report["complete"] is False
    assert index.get("old") is not None


def test_lane_checks_deadline_between_rows(tmp_path, clock, monkeypatch):
    commits = []
    request = _request(tmp_path, clock, commits=commits)
    index = _lane_index(request)
    for number in range(3):
        index.append(_payload(str(number), 1))
    # 不用sleep；第一个真实提交消耗本拍时间，下一行必须保留给下一拍。
    instant = [time.monotonic()]
    monkeypatch.setattr(history.time, "monotonic", lambda: instant[0])

    def commit(operation):
        commits.append(True)
        result = operation()
        instant[0] += 1
        return result

    report = validate_lane_receipt(history.maintain_gateway_history_lane(replace(request, commit=commit)))
    assert report["items"] == 1 and not report["complete"] and len(commits) == 1
    assert len(index.page(after="")) == 2
