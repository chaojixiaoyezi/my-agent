"""以确定性时钟跑真实维护控制器；领域动作替身不授予真实清理权限。"""
from __future__ import annotations

import threading
from types import SimpleNamespace

from agent_py_agent.agent.owner_activity_index import next_hint_revision
from agent_py_agent.agent.scheduler import timer_service
from agent_py_agent.agent.user_space import owner_maintenance as maintenance
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.cli import gateway_loops as loops
from agent_py_agent.tests.test_s2w4_due_wakeup import context_for


def test_three_thousand_owners_and_lost_hint_stay_within_backstop(tmp_path, monkeypatch):
    context, _registry, _due = context_for(tmp_path)
    context.agent.home_paths = ensure_my_agent_home(tmp_path / "cell")
    context.agent.config.owner_maintenance_scan_interval_seconds = 60
    clock = SimpleNamespace(now=10_000.0)
    fake_time = SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.now)
    monkeypatch.setattr(loops, "time", fake_time)
    monkeypatch.setattr(timer_service, "time", fake_time)
    monkeypatch.setattr(loops, "_startup_grace_active", lambda controller: False)
    home = context.agent.home_paths
    deadlines, served = {}, {}
    for value in range(3000):
        owner = f"u{value:04}"
        (home.owners_dir / "providers/feishu/users" / owner).mkdir(parents=True)
        deadlines[owner] = clock.now + value % 121
    original_deadlines = dict(deadlines)
    dropped = "u2999"
    hints = loops._OwnerWakeHints(context)
    controller = loops._GatewayOwnerMaintenanceController(context)
    hints.bind_controller("maintenance", controller)

    def note(owner, owner_home, **kwargs):
        if owner[0] != "feishu":
            return
        due = deadlines[owner[2]]
        if owner[2] == dropped:
            hints.index.note_deadline(owner, "maintenance", due, revision=next_hint_revision())
        else:
            hints._publish(owner, "maintenance", due)

    monkeypatch.setattr(hints, "note_maintenance", note)
    for owner in deadlines:
        key = ("feishu", "user", owner)
        hints.index.note_facts(key, False, False, revision=next_hint_revision())
        note(key, None)

    def invoke(operation, scoped_home, *, now, **kwargs):
        owner = scoped_home.owner_home_dir.name
        if owner in deadlines and now >= deadlines[owner]:
            served.setdefault(owner, now)
            deadlines[owner] = now + 3600
            return maintenance.OwnerMaintenanceResult(True, "success")
        return maintenance.OwnerMaintenanceResult(False, "not_due")

    monkeypatch.setattr(maintenance, "invoke_owner_maintenance", invoke)
    monkeypatch.setattr(loops, "owner_maintenance_due", lambda home, *, now: now >= deadlines.get(home.name, float("inf")))
    monkeypatch.setattr(loops, "_cleanup_loaded_runtime_safety", lambda agent: None)
    from agent_py_agent.agent.gateway_parts import compact_operations
    monkeypatch.setattr(compact_operations, "idle_compact_maintenance_tick", lambda *args, **kwargs: None)

    def run_virtual(stop, tick, site):
        assert site.interval == hints.backstop
        last_scan = clock.now - hints.backstop
        for elapsed in range(0, 182):
            clock.now = 10_000.0 + elapsed
            with hints.timer._condition:
                ready = hints.timer._take_due_locked(clock.now)
            hints.timer._dispatch(ready)
            if controller._wake_event.is_set() or clock.now - last_scan >= hints.backstop:
                tick()
                last_scan = clock.now

    monkeypatch.setattr(loops, "_run_loop_with_backoff", run_virtual)
    try:
        controller.run(threading.Event())
        assert set(served) == set(original_deadlines)
        assert all(original_deadlines[owner] <= at <= original_deadlines[owner] + hints.backstop for owner, at in served.items())
        assert served[dropped] <= original_deadlines[dropped] + hints.backstop
    finally:
        hints.close()
