"""W8 Track IV 回归测试：派发段间重派发、段耗时/扫描饥饿遥测、后台冷构建上限与阶段耗时日志。"""

from __future__ import annotations

import logging
import os
import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import hang_diagnostics, restart_service
from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
from agent_py_agent.agent.gateway_parts import loop_health as health_module
from agent_py_agent.agent.gateway_parts import request_worker as rw
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.loop_health import evaluate_dispatcher_stall, loop_health
from agent_py_agent.agent.gateway_parts.request_owner_binding import REQUEST_OWNER_KEY
from agent_py_agent.cli import gateway_loops
from agent_py_agent.tests.test_gateway_dispatcher_resilience import (
    _enqueue,
    _paths,
    _stub_dispatcher,
)


def _clear_receipt_caches() -> None:
    with delivery._ReceiptFailureCache.lock:
        delivery._ReceiptFailureCache.entries.clear()
    with delivery._CONSUMED_RECEIPT_STAMP_LOCK:
        delivery._CONSUMED_RECEIPT_STAMP_CACHE.clear()
    with delivery._RECEIPT_DIR_CACHE_LOCK:
        delivery._RECEIPT_DIR_CACHE.clear()


@pytest.fixture(autouse=True)
def _isolated_loop_and_restart_state():
    loop_health.reset()
    restart_service.reset_restart_drain_state()
    _clear_receipt_caches()
    yield
    hang_diagnostics.close()
    loop_health.reset()
    restart_service.reset_restart_drain_state()
    _clear_receipt_caches()


@pytest.fixture()
def isolated_admission(monkeypatch):
    admission = rw.GatewayAdmission()
    monkeypatch.setattr(rw, "admission", admission)
    monkeypatch.setattr(gateway_loops, "admission", admission)
    return admission


def test_pending_request_arriving_mid_reconcile_is_claimed_before_next_segment(
    tmp_path, monkeypatch, isolated_admission
) -> None:
    paths = _paths(tmp_path)
    claims: list[str] = []
    observed_at_next_segment: list[list[str]] = []
    reconcile_entered = threading.Event()
    release_reconcile = threading.Event()

    dispatcher = _stub_dispatcher(paths, claims, threading.Event())
    dispatcher._closing_deadline_once = lambda: None
    dispatcher._next_terminal_projection_at = float("inf")
    dispatcher._next_input_reconcile_at = 0.0
    dispatcher._next_lock_cleanup_at = 0.0

    def submit(claim, user_key, conversation_key):
        claims.append(claim.request_id)

    dispatcher._submit = submit

    def blocking_reconcile():
        reconcile_entered.set()
        assert release_reconcile.wait(5.0)

    def probe_next_segment():
        observed_at_next_segment.append(list(claims))

    monkeypatch.setattr(dispatcher, "_reconcile_inputs_once", blocking_reconcile)
    monkeypatch.setattr(dispatcher, "_cleanup_lock_sidecars_once", probe_next_segment)

    returned: list[int] = []
    worker = threading.Thread(target=lambda: returned.append(dispatcher.tick()), daemon=True)
    worker.start()
    try:
        assert reconcile_entered.wait(5.0)
        _enqueue(paths, "req-mid-reconcile", "user-a")
    finally:
        release_reconcile.set()
        worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert claims == ["req-mid-reconcile"]
    assert observed_at_next_segment == [["req-mid-reconcile"]]
    assert returned == [1]


def test_redispatch_if_inbox_changed_skips_dir_listing_when_inbox_unchanged(
    tmp_path, monkeypatch, isolated_admission
) -> None:
    paths = _paths(tmp_path)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    stale = time.time() - 10.0
    os.utime(paths.inbox, (stale, stale))
    monkeypatch.setattr(rw.GatewayInboxScanGate, "_COARSE_MTIME_GUARD_SECONDS", 0.0)

    dispatcher = _stub_dispatcher(paths, [], threading.Event())
    dispatcher._closing_deadline_once = lambda: None
    assert dispatcher.tick() == 0

    iter_calls: list[object] = []
    real_iter = rw._iter_pending_requests

    def spy_iter(paths_):
        iter_calls.append(paths_)
        return real_iter(paths_)

    monkeypatch.setattr(rw, "_iter_pending_requests", spy_iter)

    assert dispatcher._redispatch_if_inbox_changed() == 0
    assert iter_calls == []
    assert loop_health.snapshot()["last_dispatch_scan_monotonic"] > 0.0


def test_run_segment_logs_slow_warning_and_records_segment_telemetry(monkeypatch, caplog) -> None:
    clock = [100.0]
    monkeypatch.setattr(gateway_loops.time, "monotonic", lambda: clock[0])
    dispatcher = object.__new__(gateway_loops._RequestDispatcher)
    backoff = gateway_loops.LoopErrorBackoff()
    site = gateway_loops._RECONCILE_SITE
    inside_segment: list[str] = []

    def slow_segment():
        inside_segment.append(loop_health.snapshot()["dispatch_segment"])
        clock[0] += 1.5

    def fast_segment():
        clock[0] += 0.1

    with caplog.at_level(logging.WARNING, logger="agent_py_agent.cli.gateway_loops"):
        assert dispatcher._run_segment(site, backoff, slow_segment) == 0.0
        after_slow = loop_health.snapshot()
        assert dispatcher._run_segment(gateway_loops._PROJECTION_SITE, backoff, fast_segment) == 0.0
        after_fast = loop_health.snapshot()

    assert inside_segment == [site.context]
    assert after_slow["dispatch_segment"] == ""
    assert after_slow["last_segment_ms"] == pytest.approx(1500.0)
    assert after_slow["max_segment_ms"] == pytest.approx(1500.0)
    assert after_fast["last_segment_ms"] == pytest.approx(100.0)
    assert after_fast["max_segment_ms"] == pytest.approx(1500.0)
    slow_logs = [rec for rec in caplog.records if "GATEWAY_DISPATCH_SEGMENT_SLOW" in rec.message]
    assert len(slow_logs) == 1
    assert site.context in slow_logs[0].message
    assert "elapsed_ms=1500" in slow_logs[0].message


def test_evaluate_dispatcher_stall_uses_last_dispatch_scan_despite_segment_progress(monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(health_module, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))

    loop_health.mark_started()
    loop_health.mark_tick_started()
    loop_health.note_dispatch_scan()

    clock[0] = 1125.0
    assert loop_health.note_dispatch_tick_progress() is True

    clock[0] = 1130.0
    snapshot = loop_health.snapshot()
    starved = evaluate_dispatcher_stall(snapshot, pending_count=1)
    assert starved["dispatcher_stalled"] is True
    assert starved["dispatcher_stall_reason"] == "pending_starved"
    assert starved["dispatcher_stall_age_s"] == pytest.approx(130.0)

    idle = evaluate_dispatcher_stall(snapshot, pending_count=0)
    assert idle["dispatcher_stalled"] is False
    assert idle["dispatcher_stall_reason"] == ""


def test_sync_owner_schedulers_caps_cold_builds_rotates_and_never_skips_loaded(monkeypatch) -> None:
    cold_owners = [SimpleNamespace(provider="feishu", owner_kind="user", owner_id=f"cold-{i}") for i in range(5)]
    loaded_owner = SimpleNamespace(provider="feishu", owner_kind="user", owner_id="loaded-0")
    managed_owner = SimpleNamespace(provider="local", owner_kind="user", owner_id="managed-0")
    all_owners = [*cold_owners, loaded_owner, managed_owner]

    class _FakePool:
        def __init__(self) -> None:
            self.agents: dict[str, object] = {loaded_owner.owner_id: SimpleNamespace(owner_id=loaded_owner.owner_id)}
            self.built_per_tick: list[list[str]] = []
            self.get_calls: list[str] = []
            self._tick_built: list[str] = []

        def peek(self, owner):
            return self.agents.get(owner.owner_id)

        def get(self, owner, *, hard=True, touch=False):
            self.get_calls.append(owner.owner_id)
            if owner.owner_id not in self.agents:
                self.agents[owner.owner_id] = SimpleNamespace(owner_id=owner.owner_id)
                self._tick_built.append(owner.owner_id)
            return self.agents[owner.owner_id]

        def hard_agents(self):
            return list(self.agents.values())

        def finish_tick(self):
            self.built_per_tick.append(list(self._tick_built))
            self._tick_built.clear()

    managed_reconciles: list[str] = []
    managed_gets: list[str] = []
    managed_agent = SimpleNamespace(owner_id=managed_owner.owner_id)
    fake_managed = SimpleNamespace(
        handles=lambda owner: owner.owner_id == managed_owner.owner_id,
        reconcile=lambda owner, commit_transition: managed_reconciles.append(owner.owner_id),
        get=lambda owner: managed_gets.append(owner.owner_id) or managed_agent,
        hard_agents=lambda: [managed_agent],
    )

    pool = _FakePool()
    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._base_agent = SimpleNamespace(config=SimpleNamespace())
    supervisor._registry = SimpleNamespace(
        snapshot=lambda: all_owners,
        hard_snapshot=lambda: all_owners,
    )
    supervisor._owner_pool = pool
    supervisor._managed_owner_pool = fake_managed
    supervisor._owner_schedulers = {}
    supervisor._channels = SimpleNamespace()
    supervisor._owner_build_cursor = 0
    supervisor._inflight = {}
    supervisor._ensure_managed_owner_pool = lambda: fake_managed
    supervisor._drop_owner_planner_scope = lambda _key: None
    monkeypatch.setattr(
        gateway_loops,
        "_build_background_scheduler",
        lambda scoped_agent, _channels: SimpleNamespace(agent=scoped_agent),
    )

    for _ in range(3):
        pool.get_calls.clear()
        supervisor._sync_owner_schedulers()
        pool.finish_tick()
        assert loaded_owner.owner_id in pool.get_calls

    assert [len(batch) for batch in pool.built_per_tick] == [2, 2, 1]
    assert {oid for batch in pool.built_per_tick for oid in batch} == {o.owner_id for o in cold_owners}
    assert managed_reconciles == [managed_owner.owner_id] * 3
    assert managed_gets == [managed_owner.owner_id] * 3
    assert len(supervisor._owner_schedulers) == len(all_owners)


def test_redispatch_if_inbox_changed_claims_nothing_while_restart_draining(
    tmp_path, isolated_admission
) -> None:
    paths = _paths(tmp_path)
    _enqueue(paths, "req-during-drain", "user-a")
    claims: list[str] = []
    dispatcher = _stub_dispatcher(paths, claims, threading.Event())

    restart_service._set_phase("turn_wait", "gwrestart-w8")
    assert restart_service.restart_draining() is True
    assert dispatcher._redispatch_if_inbox_changed() == 0
    assert claims == []
    assert (paths.inbox / "req-during-drain.json").exists()

    restart_service.reset_restart_drain_state()
    assert dispatcher._redispatch_if_inbox_changed() == 1
    assert claims == ["req-during-drain"]


def test_background_supervisor_tick_logs_slow_phases(monkeypatch, caplog) -> None:
    clock = [200.0]
    monkeypatch.setattr(gateway_loops.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(gateway_loops, "restart_draining", lambda: False)

    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._base_agent = SimpleNamespace(config=SimpleNamespace())
    supervisor._next_owner_retire_at = float("inf")
    supervisor._maybe_write_heartbeat = lambda: None
    supervisor._maybe_seed_wake_pending_owners = lambda: clock.__setitem__(0, clock[0] + 0.05)
    supervisor._collect_finished_ticks = lambda: []
    supervisor._sync_owner_schedulers = lambda: clock.__setitem__(0, clock[0] + 1.2)
    supervisor._recover_active_watch_harvesters = lambda: 0
    supervisor._run_due_curators = lambda: None
    supervisor._submit_ready_thread_ticks = lambda: None

    with caplog.at_level(logging.WARNING, logger="agent_py_agent.cli.gateway_loops"):
        assert supervisor.tick() is False

    slow_logs = [rec for rec in caplog.records if "GATEWAY_BACKGROUND_TICK_SLOW" in rec.message]
    assert len(slow_logs) == 1
    msg = slow_logs[0].message
    assert "total_ms=1250" in msg
    for phase in ("seed:", "collect:", "retire:", "sync:1200", "watch:", "curators:", "submit:"):
        assert phase in msg


def test_reconcile_inputs_once_passes_should_yield_callback(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    dispatcher = _stub_dispatcher(paths, [], threading.Event())
    dispatcher._tick_inbox_stamp = dispatcher._current_inbox_stamp()
    assert dispatcher._inbox_changed_since_tick_start() is False

    captured: list[object] = []

    def fake_reconcile(paths_, agent_, *, limit=64, should_yield=None):
        captured.append((paths_, agent_, limit, should_yield))
        assert should_yield() is False
        _enqueue(paths_, "req-yield-trigger", "user-a")
        assert should_yield() is True
        return {"checked": 1, "errors": 0, "terminal_unknown": 0}

    monkeypatch.setattr(gateway_loops, "reconcile_gateway_input_receipts", fake_reconcile)
    monkeypatch.setattr(gateway_loops, "raise_if_input_reconcile_unsettled", lambda _s: None)

    dispatcher._reconcile_inputs_once()
    assert len(captured) == 1
    assert captured[0][2] == 64
    assert captured[0][3] == dispatcher._inbox_changed_since_tick_start


def _write_consumed_receipt(paths, request_id: str) -> None:
    owner = {"provider": "local", "owner_kind": "main", "owner_id": "main"}
    prepared = {
        "id": request_id,
        "request_id": request_id,
        "kind": "ask",
        "goal": "reconcile test",
        "user_id": "local-agent",
        REQUEST_OWNER_KEY: dict(owner),
        "owner_id": "main",
        "metadata": {
            "client_input_digest": f"digest-{request_id}",
            REQUEST_OWNER_KEY: dict(owner),
        },
    }
    receipt = delivery.GatewayInputReceipt(
        request_id=request_id,
        client_input_digest=f"digest-{request_id}",
        client_message_id=f"msg-{request_id}",
        state="consumed",
        prepared_request=prepared,
        prepared_payload_digest=delivery._prepared_payload_digest(prepared),
        guidance_dedupe_key=f"dedupe-{request_id}",
        updated_at=100.0,
    )
    receipts_dir = paths.root / "input_receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    write_json_file_atomic(receipts_dir / f"{request_id}.json", receipt.to_dict())


def test_claim_at_tick_start_does_not_cause_spurious_reconcile_yield(
    tmp_path, monkeypatch, isolated_admission
) -> None:
    paths = _paths(tmp_path)
    _enqueue(paths, "req-claimed-at-start", "user-a")
    _write_consumed_receipt(paths, "gwreq-msg-w8-1")
    _write_consumed_receipt(paths, "gwreq-msg-w8-2")

    claims: list[str] = []
    dispatcher = _stub_dispatcher(paths, claims, threading.Event())
    dispatcher._closing_deadline_once = lambda: None
    dispatcher._next_terminal_projection_at = float("inf")
    dispatcher._next_input_reconcile_at = 0.0
    dispatcher._next_lock_cleanup_at = float("inf")
    dispatcher._submit = lambda claim, _u, _c: claims.append(claim.request_id)

    summaries: list[dict[str, object]] = []
    real_reconcile = gateway_loops.reconcile_gateway_input_receipts

    def spy_reconcile(*args, **kwargs):
        summary = real_reconcile(*args, **kwargs)
        summaries.append(summary)
        return summary

    monkeypatch.setattr(gateway_loops, "reconcile_gateway_input_receipts", spy_reconcile)

    assert dispatcher.tick() == 1
    assert claims == ["req-claimed-at-start"]
    assert len(summaries) == 1
    assert summaries[0]["checked"] == 2
    assert summaries[0]["consumed"] == 2
    assert summaries[0]["yielded"] is False
