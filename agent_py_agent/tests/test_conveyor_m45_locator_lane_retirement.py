"""用真实目录和可控时钟验证投影枚举的条目预算与续点。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.memory_store import retention_scan
from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget
from agent_py_agent.agent.subagents.services.persistence import locator_hygiene as hygiene
from agent_py_agent.agent.user_space import storage_lanes
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.storage_lanes import (
    STORAGE_LANE_SLICE_ENTRIES,
    StorageLaneBudget,
    StorageLaneRequest,
    validate_lane_receipt,
)
from agent_py_agent.tests.test_nw2_w8_subagent_supervision_and_hygiene import (
    _home_fixture,
    _lane_request,
    _write_projection,
)


def _request(home, cursor, *, entries):
    return StorageLaneRequest("subagents", home, 1_700_000_000, cursor, StorageLaneBudget(entries, None, None),
                              False, False, None, None)


def _observe_projections(monkeypatch, home, *, clock, pin_seconds=0.0):
    visited = []

    def pins(*_args, **_kwargs):
        clock[0] += pin_seconds
        return retention_scan._RetentionPins(home.owner_home_dir), True

    def classify(_home, run_dir, **_kwargs):
        visited.append(run_dir.name)
        return {"classification": "active_or_protected"}

    monkeypatch.setattr(storage_lanes, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(retention_scan, "current_retention_pins", pins)
    monkeypatch.setattr(hygiene, "_classify", classify)
    return visited


def test_large_locator_slice_charges_pin_time(tmp_path, monkeypatch):
    home = home_paths(tmp_path / "isolated")
    agents = home.owner_home_dir / "agents"
    agents.mkdir(parents=True)
    for index in range(50_000):
        (agents / f"run-{index:05}").mkdir()
    clock = [0.0]
    visited = _observe_projections(monkeypatch, home, clock=clock, pin_seconds=0.026)

    receipt = validate_lane_receipt(hygiene.maintain_subagents_lane(_request(home, {}, entries=6)))

    assert not visited, (receipt, visited)
    assert receipt["items"] <= 6
    assert not receipt["complete"]

    visited = _observe_projections(monkeypatch, home, clock=clock)
    following = validate_lane_receipt(hygiene.maintain_subagents_lane(
        _request(home, receipt["cursor"], entries=6),
    ))
    assert following["items"] == len(visited) == 6
    assert following["cursor"]["directory"]["last_scanned"] == 1
    assert not following["complete"]


def test_locator_slices_visit_each_entry_once_per_cycle(tmp_path, monkeypatch):
    home = home_paths(tmp_path / "isolated")
    agents = home.owner_home_dir / "agents"
    agents.mkdir(parents=True)
    names = {f"run-{index:02}" for index in range(17)}
    for name in sorted(names, reverse=True):
        (agents / name).mkdir()
    (agents / "ordinary-file").touch()
    (agents / "linked-run").symlink_to(agents / "run-00", target_is_directory=True)
    visited = _observe_projections(monkeypatch, home, clock=[0.0])
    cursor = {"phase": "projections", "after": "obsolete-lexical-cursor", "cycle_generation": 4}
    charged = 0
    for _ in range(25):
        receipt = validate_lane_receipt(hygiene.maintain_subagents_lane(_request(home, cursor, entries=3)))
        assert receipt["items"] <= 3
        charged += receipt["items"]
        cursor = json.loads(json.dumps(receipt["cursor"]))
        if receipt["complete"]:
            break
    assert receipt["complete"]
    assert len(visited) == len(set(visited)) == len(names)
    assert set(visited) == names
    assert charged >= len(names) + 2
    assert receipt["cycle_generation"] == cursor["cycle_generation"] == 5


def test_locator_stops_reading_wake_entries_at_slice_deadline(tmp_path, monkeypatch):
    home = home_paths(tmp_path / "isolated")
    (home.owner_home_dir / "agents" / "run-one").mkdir(parents=True)
    queue = home.owner_home_dir / "conversations" / "wake_queue" / "normal"
    queue.mkdir(parents=True)
    for index in range(12):
        (queue / f"wake-{index}.json").write_text('{"status":"pending"}')
    clock, seen = [0.0], []
    _observe_projections(monkeypatch, home, clock=clock)
    read_projection = hygiene.read_projection_object

    def slow_read(path, *args, **kwargs):
        if path.parent == queue:
            seen.append(path.name)
            clock[0] += 0.010
        return read_projection(path, *args, **kwargs)

    def classify(_home, _run, *, wake_lookup, **_kwargs):
        assert wake_lookup() is None
        return {"classification": "active_or_protected"}

    monkeypatch.setattr(hygiene, "_classify", classify)
    monkeypatch.setattr(hygiene, "read_projection_object", slow_read)
    receipt = validate_lane_receipt(hygiene.maintain_subagents_lane(_request(home, {}, entries=32)))

    assert len(seen) == 3
    assert receipt["items"] == 1 + 12 + len(seen)
    assert receipt["protected"]["projection"] == 1
    assert not receipt["complete"]


def _age(path, seconds=3600.0):
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def _dedupe_queue(home, count, *, phase="published"):
    # 按原发布记录形状留存已处理回执（retain_handled），引用指向其它run，不应保护候选。
    queue = home.owner_home_dir / "conversations" / "wake_queue" / "dedupe"
    queue.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        record = {"schema_version": "wake_publication.v2", "phase": phase, "retain_handled": True,
                  "wake_signal_id": f"wake-{index:05}",
                  "signal": {"source_agent_id": f"other-run-{index:05}", "status": "pending"}}
        (queue / f"thread.key-{index:05}.json").write_text(json.dumps(record), encoding="utf-8")
    _age(queue)
    return queue


def _drain_until_removed(home, now, run_dir, *, slices=100):
    cursor = {}
    for count in range(1, slices + 1):
        receipt = validate_lane_receipt(hygiene.maintain_subagents_lane(
            _lane_request(home, now, cursor=cursor, max_items=STORAGE_LANE_SLICE_ENTRIES)))
        cursor = json.loads(json.dumps(receipt["cursor"]))
        if not run_dir.exists():
            return count
    return None


def _count_queue_reads(monkeypatch, queue):
    reads = []
    original = hygiene.read_projection_object

    def counted(path, *args, **kwargs):
        if path.parent == queue:
            reads.append(path.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(hygiene, "read_projection_object", counted)
    return reads


def test_wake_cover_resumes_across_production_slices_and_reclaims(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import retention_scan_budget

    home, now = _home_fixture(tmp_path)
    run_dir = _write_projection(home.owner_home_dir, tmp_path, now, run_id="old-run", days=40.0)
    queue = _dedupe_queue(home, 2000)

    # Expire real slices by work performed so parallel filesystem setup cannot skew this test.
    clock = [0.0]
    read_projection = hygiene.read_projection_object

    def timed_read(path, *args, **kwargs):
        if path.parent == queue:
            clock[0] += 0.001
        return read_projection(path, *args, **kwargs)

    lookups = []
    with monkeypatch.context() as timed:
        timed.setattr(retention_scan_budget, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        timed.setattr(hygiene, "read_projection_object", timed_read)
        for _ in range(100):
            budget = RetentionScanBudget.default()
            lookups.append(hygiene._collect_active_wake_ids(home.owner_home_dir, time_expired=budget.exhausted))
            if lookups[-1] is not None:
                break
    assert lookups[-1] == set(), lookups[-3:]

    reads = _count_queue_reads(monkeypatch, queue)
    assert _drain_until_removed(home, now, run_dir) is not None
    # 已覆盖且未变的回执不再重读；回执本身不被本lane改动。
    assert reads == []
    assert len(list(queue.iterdir())) == 2000


def test_wake_cover_rereads_only_new_entries_and_new_prepared_wake_protects(tmp_path, monkeypatch):
    home, now = _home_fixture(tmp_path)
    run_dir = _write_projection(home.owner_home_dir, tmp_path, now, run_id="late-run", days=40.0)
    queue = _dedupe_queue(home, 50)
    lookup = lambda: hygiene._collect_active_wake_ids(home.owner_home_dir, time_expired=lambda: False)  # noqa: E731
    assert lookup() == set()

    (queue / "thread.key-new.json").write_text(json.dumps({
        "schema_version": "wake_publication.v2", "phase": "prepared", "retain_handled": True,
        "signal": {"source_agent_id": "late-run", "status": "pending"},
    }), encoding="utf-8")
    reads = _count_queue_reads(monkeypatch, queue)
    assert "late-run" in lookup()
    _age(queue, 7200.0)
    assert "late-run" in lookup()
    assert reads == ["thread.key-new.json"]
    assert _drain_until_removed(home, now, run_dir, slices=5) is None
    assert run_dir.exists()


@pytest.mark.parametrize("damage", ["symlinked_folder", "unknown_phase", "unreadable_entry", "stat_error"])
def test_wake_cover_protects_on_unverifiable_evidence(tmp_path, monkeypatch, damage):
    home, now = _home_fixture(tmp_path)
    run_dir = _write_projection(home.owner_home_dir, tmp_path, now, run_id="kept-run", days=40.0)
    queue = _dedupe_queue(home, 3)
    if damage == "symlinked_folder":
        normal = home.owner_home_dir / "conversations" / "wake_queue" / "normal"
        if normal.exists():
            normal.rmdir()
        outside = tmp_path / "outside-queue"
        outside.mkdir()
        normal.symlink_to(outside, target_is_directory=True)
    elif damage == "unknown_phase":
        (queue / "thread.key-odd.json").write_text(json.dumps({"phase": "rewinding"}), encoding="utf-8")
    elif damage == "stat_error":
        denied = queue / "thread.key-00001.json"
        original = os.lstat

        def lstat(path, *args, **kwargs):
            if os.fspath(path) == os.fspath(denied) and not lstat.listed:
                lstat.listed = True
                return original(path, *args, **kwargs)
            if os.fspath(path) == os.fspath(denied):
                raise PermissionError("simulated I/O error")
            return original(path, *args, **kwargs)

        lstat.listed = False
        monkeypatch.setattr(os, "lstat", lstat)
    else:
        (queue / "thread.key-torn.json").write_text('{"phase": "prep', encoding="utf-8")
    assert hygiene._collect_active_wake_ids(home.owner_home_dir, time_expired=lambda: False) is None
    assert _drain_until_removed(home, now, run_dir, slices=5) is None
    assert run_dir.exists()


def test_wake_cover_rereads_entry_replaced_during_read(tmp_path, monkeypatch):
    home, _now = _home_fixture(tmp_path)
    queue = _dedupe_queue(home, 2)
    target = queue / "thread.key-00000.json"
    original = hygiene.read_projection_object
    replaced = []

    def read(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == target and not replaced:
            # 读后被原子替换为引用目标run的prepared记录（新inode）；旧读结果不能记入覆盖。
            fresh = queue / ".replacement"
            fresh.write_text(json.dumps({"phase": "prepared", "signal": {"source_agent_id": "swapped-run"}}),
                             encoding="utf-8")
            os.replace(fresh, target)
            replaced.append(path)
        return value

    monkeypatch.setattr(hygiene, "read_projection_object", read)
    refs = hygiene._collect_active_wake_ids(home.owner_home_dir, time_expired=lambda: False)
    assert replaced and refs is not None and "swapped-run" in refs


def test_wake_cover_retries_unchanged_receipt_after_read_error(tmp_path, monkeypatch):
    home, now = _home_fixture(tmp_path)
    run_dir = _write_projection(home.owner_home_dir, tmp_path, now, run_id="old-run", days=40.0)
    queue = _dedupe_queue(home, 3)
    target = queue / "thread.key-00000.json"
    read_projection = hygiene.read_projection_object
    attempts = []

    def read(path, *args, **kwargs):
        if path == target:
            attempts.append(path)
            if len(attempts) == 1:
                raise OSError("simulated read error")
        return read_projection(path, *args, **kwargs)

    monkeypatch.setattr(hygiene, "read_projection_object", read)
    assert hygiene._collect_active_wake_ids(home.owner_home_dir, time_expired=lambda: False) is None
    assert run_dir.exists()
    assert _drain_until_removed(home, now, run_dir) is not None
    assert len(attempts) == 2


def test_confirmed_wake_cover_corruption_rebuilds_from_queue(tmp_path):
    home, now = _home_fixture(tmp_path)
    run_dir = _write_projection(home.owner_home_dir, tmp_path, now, run_id="old-run", days=40.0)
    queue = _dedupe_queue(home, 6)
    assert hygiene._collect_active_wake_ids(home.owner_home_dir, time_expired=lambda: False) == set()
    cover = home.owner_home_dir.joinpath(*hygiene._WAKE_COVER_PARTS)
    damaged = b"invalid SQLite header" * 64
    cover.write_bytes(damaged)
    hold_lock = """
import sys
from pathlib import Path
from agent_py_agent.agent.common.json_io import locked_json_path
with locked_json_path(Path(sys.argv[1])):
    print("ready", flush=True)
    sys.stdin.readline()
"""
    with subprocess.Popen([sys.executable, "-c", hold_lock, str(cover)], text=True,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE) as peer:
        assert peer.stdout.readline().strip() == "ready"
        assert hygiene._collect_active_wake_ids(home.owner_home_dir, time_expired=lambda: False) is None
        assert cover.read_bytes() == damaged and run_dir.exists()
        peer.communicate("\n", timeout=10)
        assert peer.returncode == 0
    assert _drain_until_removed(home, now, run_dir) is not None
    assert len(list(queue.iterdir())) == 6


@pytest.mark.parametrize(("entries", "count"), [(1, 8), (4, 72)])
def test_cold_wake_listing_and_reads_share_loaded_entry_allowance(tmp_path, monkeypatch, entries, count):
    from agent_py_agent.agent.user_space import directory_paging

    home, _ = _home_fixture(tmp_path)
    (home.owner_home_dir / "agents" / "run-one").mkdir(parents=True)
    queue = _dedupe_queue(home, count)
    reads = _count_queue_reads(monkeypatch, queue)
    clock = [0.0]
    _observe_projections(monkeypatch, home, clock=clock)
    directory_page = directory_paging.directory_page

    def late_page(root, parts, position, limit, **kwargs):
        page = directory_page(root, parts, position, limit, **kwargs)
        if root.joinpath(*parts) == queue:
            clock[0] += 0.026
        return page

    monkeypatch.setattr(directory_paging, "directory_page", late_page)

    def classify(_home, _run, *, wake_lookup, **_kwargs):
        wake_lookup()
        return {"classification": "active_or_protected"}

    monkeypatch.setattr(hygiene, "_classify", classify)
    cursor = {}
    for _ in range(80):
        before = len(reads)
        receipt = validate_lane_receipt(hygiene.maintain_subagents_lane(_request(home, cursor, entries=entries)))
        assert len(reads) - before <= receipt["items"] <= entries
        cursor = json.loads(json.dumps(receipt["cursor"]))
        if len(reads) == count:
            break
    assert len(reads) == len(set(reads)) == count
