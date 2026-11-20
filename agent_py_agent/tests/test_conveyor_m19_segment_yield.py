from __future__ import annotations

from contextlib import nullcontext
from dataclasses import FrozenInstanceError, asdict, dataclass, replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.fast_dataclass import fast_replace
from agent_py_agent.agent.gateway_parts import control_operation_service
from agent_py_agent.agent.io import sqlite_pool
from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.cli import gateway_loops as loops
from agent_py_agent.cli.gateway_lane_retry import BackgroundLaneRetry


def _supervisor_with_ready_owners(monkeypatch, *, slots, owners, threads=1):
    clock = [0.0]
    events = []
    monkeypatch.setattr(loops, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000.0 + clock[0]))

    def make_scheduler(label):
        def prepare_tick(**_kwargs):
            clock[0] += 0.05
            events.append(("plan", label))

        return SimpleNamespace(
            prepare_tick=prepare_tick,
            ready_thread_ids=lambda **_kwargs: [f"thread-{label}-{i}" for i in range(threads)],
            runtime=SimpleNamespace(agent=None),
        )

    supervisor = object.__new__(loops._BackgroundMainSupervisor)
    supervisor._base_agent = SimpleNamespace(config=SimpleNamespace(
        background_owner_workers=slots, background_threads_per_owner=threads, background_admin_threads_per_owner=threads,
    ))
    supervisor._base_scheduler = make_scheduler("base")
    supervisor._owner_schedulers = {f"owner-{i}": make_scheduler(f"owner-{i}") for i in range(1, owners)}
    supervisor._inflight = {}
    supervisor._owner_idle_streaks = {}
    supervisor._lane_retry = BackgroundLaneRetry()
    supervisor._shutdown_control = SimpleNamespace(stopping=False)
    supervisor._retain_owner_planner_scopes = lambda _keys: None
    supervisor._owner_plan_backoff_state = lambda *_args, **_kwargs: (False, 0.0, None, True)
    supervisor._planner_sqlite_scope = lambda _key: nullcontext()
    supervisor._note_owner_plan = lambda *_args: None
    supervisor._thread_lane_can_retry = lambda *_args: True

    def submit(_worker, _scheduler, label, _thread_id):
        events.append(("submit", label))
        return object()

    supervisor._executor = SimpleNamespace(submit=submit)
    return supervisor, clock, events


@pytest.mark.parametrize("slots", [8, 128])
def test_slow_owner_planning_interleaves_submission(monkeypatch, slots):
    supervisor, clock, events = _supervisor_with_ready_owners(monkeypatch, slots=slots, owners=64)
    durations = []
    counts = []
    for _ in range((64 + slots - 1) // slots):
        started = clock[0]
        supervisor._submit_ready_thread_ticks()
        durations.append(clock[0] - started)
        counts.append(getattr(supervisor, "_thread_submit_counts", None))
        assert len(supervisor._inflight) <= slots
        supervisor._inflight.clear()

    assert events.index(("submit", "base")) < events.index(("plan", "owner-63"))
    assert [owner for operation, owner in events if operation == "submit"] == [
        "base", *supervisor._owner_schedulers,
    ]
    assert counts == [(min(slots, 64), min(slots, 64))] * len(durations)
    assert max(durations) == pytest.approx(min(slots, 64) * 0.05)
    assert max(durations) < 5.0


def test_each_owner_gets_a_slot_before_any_second_thread(monkeypatch):
    supervisor, _, events = _supervisor_with_ready_owners(monkeypatch, slots=5, owners=3, threads=3)
    supervisor._submit_ready_thread_ticks()
    assert [owner for operation, owner in events if operation == "submit"] == [
        "base", "owner-1", "owner-2", "base", "owner-1",
    ]


def test_partial_pass_fills_free_slots_then_rotates_to_unvisited_owners(monkeypatch):
    supervisor, _, events = _supervisor_with_ready_owners(monkeypatch, slots=4, owners=6, threads=4)
    for i in range(1, 4):
        supervisor._owner_schedulers[f"owner-{i}"].ready_thread_ids = lambda **_kwargs: []

    supervisor._submit_ready_thread_ticks()
    assert supervisor._thread_submit_counts == (4, 4)
    assert [owner for operation, owner in events if operation == "submit"] == ["base"] * 4

    supervisor._inflight.pop(next(iter(supervisor._inflight)))
    events.clear()
    supervisor._submit_ready_thread_ticks()
    assert events == [("plan", "owner-4"), ("submit", "owner-4")]
    assert len(supervisor._inflight) == 4


def test_slow_gateway_tick_reports_segment_counters(monkeypatch, caplog):
    supervisor, _, _ = _supervisor_with_ready_owners(monkeypatch, slots=128, owners=64)
    supervisor._maybe_write_heartbeat = lambda: None
    supervisor._maybe_seed_wake_pending_owners = lambda: None
    supervisor._collect_finished_ticks = list
    supervisor._next_owner_retire_at = float("inf")
    supervisor._sync_owner_schedulers = lambda: None
    supervisor._recover_active_watch_harvesters = lambda: None
    supervisor._run_due_curators = lambda: None
    monkeypatch.setattr(loops, "restart_draining", lambda: False)

    assert supervisor.tick() is False
    assert "segments=64 candidates_planned=64 submit_ms=3200" in caplog.text


@pytest.mark.parametrize("failure", [None, "prune", "retire"])
def test_maintenance_pass_uses_loaded_budgets_and_isolates_failures(monkeypatch, tmp_path, failure):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "gateway_control_operation_retention_days: 3\n"
        "retention_scan_slice_entries: 17\n"
        "retention_scan_slice_ms: 9\n",
        encoding="utf-8",
    )
    dispatcher = object.__new__(loops._RequestDispatcher)
    dispatcher.bootstrap_agent = SimpleNamespace(config=load_config(config_path))
    dispatcher.paths = object()
    events = []
    errors = []

    def prune(paths, *, now, retention_days, max_entries, max_ms):
        assert paths is dispatcher.paths
        events.append(("prune", now, retention_days, max_entries, max_ms))
        if failure == "prune":
            raise OSError("simulated prune I/O error")

    def retire():
        events.append(("retire",))
        if failure == "retire":
            raise OSError("simulated retire I/O error")

    monkeypatch.setattr(loops, "time", SimpleNamespace(time=lambda: 1000.0))
    monkeypatch.setattr(control_operation_service, "prune_terminal_control_operations", prune)
    monkeypatch.setattr(sqlite_pool, "retire_idle_connections", retire)
    monkeypatch.setattr(loops, "gateway_lock_sidecar_dirs", lambda _paths: [tmp_path])
    monkeypatch.setattr(loops, "cleanup_orphan_lock_files", lambda path: events.append(("sidecars", path)))
    monkeypatch.setattr(loops, "_print_gateway_loop_error", lambda context, _, exc: errors.append((context, str(exc))))

    dispatcher._cleanup_lock_sidecars_once()
    dispatcher._cleanup_lock_sidecars_once()

    assert events == [("prune", 1000.0, 3, 17, 9), ("retire",), ("sidecars", tmp_path)] * 2
    context = {"prune": "gateway.control_operation_retention", "retire": "gateway.sqlite_idle_retirement"}
    assert errors == ([(context[failure], f"simulated {failure} I/O error")] * 2 if failure else [])


def test_loop_site_matches_standard_frozen_dataclass():
    @dataclass(frozen=True)
    class Reference:
        loop: str
        context: str
        worker_id: str
        interval: float = 0.0

    reference = Reference("dispatcher", context="tick", worker_id="worker")
    site = loops._LoopSite("dispatcher", context="tick", worker_id="worker")
    assert asdict(site) == asdict(reference)
    assert asdict(fast_replace(site, interval=2.5)) == asdict(replace(reference, interval=2.5))
    with pytest.raises(FrozenInstanceError):
        site.interval = 1.0


def g1_case_loop_site():
    return lambda: loops._LoopSite("dispatcher", "gateway_request_dispatch.iteration", "dispatcher")
