"""用真实目录和可控时钟验证投影枚举的条目预算与续点。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.memory_store import retention_scan
from agent_py_agent.agent.subagents.services.persistence import locator_hygiene as hygiene
from agent_py_agent.agent.user_space import storage_lanes
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.storage_lanes import (
    StorageLaneBudget,
    StorageLaneRequest,
    validate_lane_receipt,
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
    receipt = validate_lane_receipt(hygiene.maintain_subagents_lane(_request(home, {}, entries=4)))

    assert len(seen) == 3
    assert receipt["items"] == receipt["protected"]["projection"] == 1
    assert not receipt["complete"]
