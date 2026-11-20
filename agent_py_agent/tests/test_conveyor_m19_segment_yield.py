from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import FrozenInstanceError, asdict, dataclass, replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.fast_dataclass import fast_replace
from agent_py_agent.agent.gateway_parts import control_operation_service
from agent_py_agent.agent.gateway_parts.io import fcntl, gateway_lock_sidecar_dirs
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
    supervisor._shutdown_control = SimpleNamespace(stopping=False, commit_scope=nullcontext)
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


def _load_budget_config(tmp_path, *, days=3, entries=17, ms=9):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"gateway_control_operation_retention_days: {days}\n"
        f"retention_scan_slice_entries: {entries}\n"
        f"retention_scan_slice_ms: {ms}\n",
        encoding="utf-8",
    )
    return load_config(config_path)


@pytest.mark.parametrize("failure", [None, "prune", "retire"])
def test_retention_segment_uses_loaded_budgets_and_isolates_failures(monkeypatch, tmp_path, failure):
    dispatcher = object.__new__(loops._RequestDispatcher)
    dispatcher.bootstrap_agent = SimpleNamespace(config=_load_budget_config(tmp_path))
    dispatcher.paths = object()
    events = []

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

    for _ in range(2):
        if failure is None:
            dispatcher._retain_control_operations_once()
        else:
            with pytest.raises(OSError, match=f"simulated {failure} I/O error"):
                dispatcher._retain_control_operations_once()

    # retire 总在 prune 之后跑，即使 prune 抛错；异常交给段守卫而不是被吞掉。
    assert events == [("prune", 1000.0, 3, 17, 9), ("retire",)] * 2


def _maintenance_only_dispatcher(paths, config):
    dispatcher = object.__new__(loops._RequestDispatcher)
    dispatcher.paths = paths
    dispatcher.bootstrap_agent = SimpleNamespace(config=config)
    dispatcher._recover_throttle = SimpleNamespace(due=lambda: False)
    dispatcher._closing_deadline_once = lambda: None
    dispatcher._redispatch_if_inbox_changed = lambda: 0
    dispatcher._next_terminal_projection_at = dispatcher._next_input_reconcile_at = float("inf")
    dispatcher._next_lock_cleanup_at = 0.0
    dispatcher._lock_cleanup_backoff = loops.LoopErrorBackoff()
    return dispatcher


def _expired_receipt(paths, name):
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    owner = OwnerIdentity("local", "user", "main")
    scope = GatewayControlScope("main", "local", "chat", {"message_id": name}, resolved_owner=owner)
    operation, digest = control_operation_service.gateway_control_operation_identity(scope, "/stop")
    receipt = control_operation_service._new_control_operation_receipt(scope, "stop", "/stop", operation, digest)
    receipt = replace(receipt, state="completed", updated_at=86400.0,
                      result={"kind": "stop", "ok": True, "message": "done"})
    path = control_operation_service.gateway_control_operation_path(paths, operation)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt.to_dict()), encoding="utf-8")
    # 预置伴生锁，使目录条目数固定为 2×回执，不受 prune 期间新建锁文件影响。
    path.with_suffix(".json.lock").touch(mode=0o600)
    return path


@pytest.mark.skipif(fcntl is None, reason="需要原文件事务和目录句柄")
def test_control_receipt_retention_continues_every_maintenance_pass(monkeypatch, tmp_path):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root((tmp_path / "gateway").resolve())
    receipts = [_expired_receipt(paths, f"expired-{index}") for index in range(300)]
    # 非默认的条数预算（默认 128）必须原样到达 prune；时限放宽只为消除机器速度带来的不确定性。
    config = _load_budget_config(tmp_path, days=7, entries=200, ms=60000)
    dispatcher = _maintenance_only_dispatcher(paths, config)
    slices, retired, sidecar_sweeps = [], [], []
    real_prune = control_operation_service.prune_terminal_control_operations

    def prune(*args, **kwargs):
        result = real_prune(*args, **kwargs)
        slices.append((kwargs["retention_days"], kwargs["max_entries"], kwargs["max_ms"], result["removed"]))
        return result

    monkeypatch.setattr(control_operation_service, "prune_terminal_control_operations", prune)
    monkeypatch.setattr(sqlite_pool, "retire_idle_connections", lambda: retired.append(True) or 0)
    monkeypatch.setattr(loops, "cleanup_orphan_lock_files", lambda path: sidecar_sweeps.append(path))
    # 单调钟停在小时级 sidecar 间隔以内；墙钟远超 7 天保留期。
    monkeypatch.setattr(loops, "time", SimpleNamespace(monotonic=lambda: 10.0, time=lambda: 30 * 86400.0))

    for _ in range(3):
        dispatcher._run_maintenance_segments()

    assert [receipt for receipt in receipts if receipt.exists()] == []
    assert [row[:3] for row in slices] == [(7, 200, 60000)] * 3
    assert sum(row[3] for row in slices) == 300
    assert retired == [True] * 3
    # 孤儿锁清扫仍按 3,600 秒节拍：三轮里只跑首轮那一次。
    assert len(sidecar_sweeps) == len(gateway_lock_sidecar_dirs(paths))


def test_failed_retention_slice_defers_only_its_own_segment(monkeypatch, tmp_path):
    dispatcher = _maintenance_only_dispatcher(object(), _load_budget_config(tmp_path))
    dispatcher._next_lock_cleanup_at = float("inf")
    clock, calls = [100.0], []

    def prune(*_args, **_kwargs):
        calls.append("prune")
        raise OSError("simulated prune I/O error")

    monkeypatch.setattr(loops, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000.0))
    monkeypatch.setattr(control_operation_service, "prune_terminal_control_operations", prune)
    monkeypatch.setattr(sqlite_pool, "retire_idle_connections", lambda: calls.append("retire") or 0)
    monkeypatch.setattr(loops, "_print_gateway_loop_error", lambda *_args: None)

    dispatcher._run_maintenance_segments()
    assert calls == ["prune", "retire"]
    assert dispatcher._control_retention_backoff.consecutive_failures == 1
    assert dispatcher._next_control_retention_at > clock[0]
    dispatcher._run_maintenance_segments()
    assert calls == ["prune", "retire"], "退避期内本段不重跑"
    clock[0] = dispatcher._next_control_retention_at
    monkeypatch.setattr(control_operation_service, "prune_terminal_control_operations", lambda *_a, **_k: calls.append("ok"))
    dispatcher._run_maintenance_segments()
    assert calls[-2:] == ["ok", "retire"]
    assert dispatcher._control_retention_backoff.consecutive_failures == 0
    assert dispatcher._next_control_retention_at == 0.0


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
