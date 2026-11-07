"""提交后提示、原权威扫描与精确行CAS的端到端组件测试。"""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.runtime_db import input_lifecycle
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.scheduler.due_index import SchedulerDueIndex

OWNER = {"provider": "feishu", "kind": "user", "id": "alice"}
KEY = ("feishu", "user", "alice")


def store(due):
    return {"jobs": {"job": {"status": "active", "next_run_at": due}}, "runs": {}}


def test_due_listener_observes_committed_effective_deadline_across_instances(tmp_path):
    index = SchedulerDueIndex(tmp_path / "due.sqlite3")
    assert callable(getattr(index, "add_due_listener", None))
    events = []

    def listener(owner, due, revision):
        assert SchedulerDueIndex(index.path).next_due_at(owner) == due
        events.append((owner, due, revision))

    remove = index.add_due_listener(listener)
    try:
        other = SchedulerDueIndex(index.path)
        other.sync_owner(OWNER, store(200), now=100)
        other.claim_due_owners(now=200, lease_seconds=30)
        other.sync_owner(OWNER, {"jobs": {}, "runs": {}}, now=201)
        assert [item[1] for item in events] == [200, 230, None]
        assert all(left[2] < right[2] for left, right in zip(events, events[1:]))
    finally:
        remove()


def test_runtime_notifications_are_after_commit_and_keep_exact_cas(tmp_path):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    assert callable(getattr(input_lifecycle, "add_wake_listener", None))
    notifications = []

    def listener(path, row, revision):
        current = RuntimeRepository(path, read_only=True).next_runtime_wake()
        assert (dict(current) if current else None) == row
        notifications.append(revision)

    remove = input_lifecycle.add_wake_listener(listener)
    try:
        row = dict(repo.upsert_wake(root_task_id="one", next_due_at=100))
        claim = repo.claim_runtime_wake(row, now=101)
        assert claim and repo.next_runtime_wake()["lease_until"] == 401
        assert repo.perform_runtime_wake(claim, lambda: None)
        replacement = dict(repo.upsert_wake(root_task_id="one", next_due_at=500))
        assert replacement["wake_id"] == claim["wake_id"]
        assert not repo.settle_runtime_wake(claim)
        assert repo.read_runtime_wake(claim["wake_id"]) == replacement
        assert notifications == sorted(set(notifications)) and len(notifications) == 4
    finally:
        remove()


def test_release_retry_and_claimed_expiry_use_consumable_time(tmp_path):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    assert callable(getattr(repo, "next_runtime_wake", None))
    row = repo.upsert_wake(root_task_id="one", next_due_at=100)
    repo.pop_due_wakes(now=101)
    assert repo.next_runtime_wake()["lease_until"] == 401
    repo.release_wake(row["wake_id"], retry_after=700)
    assert repo.next_runtime_wake()["retry_after"] == 700
    assert not repo.due_runtime_wakes(now=699)
    assert repo.due_runtime_wakes(now=700)


# LLM: 默认配置只供临时组件；显式覆盖允许将真实线程backstop与计时合同分开。
# 函数用途: 在隔离目录组合真实索引和登记表，不启动完整Agent。
def context_for(tmp_path, **config):
    from agent_py_agent.agent.owner_scoped_pool import ActiveOwnerRegistry

    home = SimpleNamespace(root=tmp_path, owners_dir=tmp_path / "owners", global_index_dir=tmp_path / "global_index",
                           owner_home_dir=tmp_path / "owners/local/main")
    registry = ActiveOwnerRegistry()
    index = SchedulerDueIndex(home.global_index_dir / "scheduler_due.sqlite3")
    settings = {"owner_agent_pool_max_agents": 128, "background_owner_workers": 8,
                "owner_maintenance_scan_interval_seconds": 0.06, **config}
    base = SimpleNamespace(config=SimpleNamespace(**settings), home_paths=home,
        scheduler_repository=SimpleNamespace(due_index=index), _active_owner_registry=registry)
    return SimpleNamespace(agent=base), registry, index


# LLM: 50ms是提示与消费的逻辑边界，不以CI线程调度延迟判定；真实线程另测。
# 函数用途: 用可控时钟推进真实timer和due controller，验证不提前唤醒且50ms内登记。
def test_timer_wakes_real_due_controller_within_fifty_ms(tmp_path, monkeypatch):
    from agent_py_agent.agent.scheduler import timer_service
    from agent_py_agent.cli import gateway_loops as loops

    assert callable(getattr(loops, "_OwnerWakeHints", None))
    wall, mono = [time.time()], [100.0]
    clock = SimpleNamespace(time=lambda: wall[0], monotonic=lambda: mono[0])
    monkeypatch.setattr(loops, "time", clock)
    monkeypatch.setattr(timer_service, "time", clock)
    context, registry, index = context_for(tmp_path)
    hints = loops._OwnerWakeHints(context)
    controller = loops._GatewaySchedulerDueController(context)
    hints.bind_controller("scheduler_due", controller)
    due = wall[0] + 0.3
    due_mono = mono[0] + 0.3
    try:
        index.sync_owner(OWNER, store(due), now=wall[0])
        with hints.timer._condition:
            early = hints.timer._take_due_locked(due_mono - 0.001)
        hints.timer._dispatch(early)
        assert not hints.events["scheduler_due"].is_set()
        assert controller.tick(now=due - 0.001) == 0
        assert registry.snapshot() == []

        wall[0], mono[0] = due + 0.049, due_mono + 0.049
        with hints.timer._condition:
            ready = hints.timer._take_due_locked(mono[0])
        hints.timer._dispatch(ready)
        assert hints.events["scheduler_due"].is_set()
        assert controller.tick(now=wall[0]) == 1
        assert [(owner.provider, owner.owner_kind, owner.owner_id) for owner in registry.snapshot()] == [KEY]
        assert wall[0] <= due + 0.05
    finally:
        hints.close()


# LLM: backstop大于等待上界，成功必须经真实timer事件而非定时轮询；失败也要关闭两条线程。
# 函数用途: 验证真实线程唤醒链和异常路径的finally清理，不把主机负载当50ms性能门。
def test_timer_thread_wakes_due_controller_before_backstop(tmp_path, monkeypatch):
    from agent_py_agent.cli import gateway_loops as loops

    context, registry, index = context_for(tmp_path, owner_maintenance_scan_interval_seconds=60)
    hints = loops._OwnerWakeHints(context)
    controller = loops._GatewaySchedulerDueController(context)
    hints.bind_controller("scheduler_due", controller)
    initial_tick = threading.Event()
    awakened = threading.Event()
    tick = controller.tick

    def observe_tick():
        result = tick()
        initial_tick.set()
        if registry.snapshot():
            awakened.set()
        return result

    monkeypatch.setattr(controller, "tick", observe_tick)
    stop = threading.Event()
    thread = threading.Thread(target=controller.run, args=(stop,))
    try:
        hints.start()
        thread.start()
        assert initial_tick.wait(5), "due controller did not start"
        index.sync_owner(OWNER, store(time.time() + 0.1))
        assert awakened.wait(5), "timer did not wake due controller before the 60s backstop"
        assert [(owner.provider, owner.owner_kind, owner.owner_id) for owner in registry.snapshot()] == [KEY]
    finally:
        stop.set()
        try:
            hints.close()
        finally:
            if thread.ident is not None:
                thread.join(5)
            assert not thread.is_alive(), "due controller leaked after cleanup"
            assert not hints.timer.snapshot()["running"], "timer leaked after cleanup"


@pytest.mark.parametrize("context", [SimpleNamespace(), SimpleNamespace(agent=None)])
def test_missing_base_agent_is_explicitly_rejected(context):
    from agent_py_agent.cli import gateway_loops as loops

    with pytest.raises(RuntimeError, match="缺少已初始化的 agent"):
        loops._gateway_agent_from_context(context)


def test_listener_exception_does_not_undo_commit_and_restart_rebuilds(tmp_path):
    from agent_py_agent.cli import gateway_loops as loops

    assert callable(getattr(loops, "_OwnerWakeHints", None))
    context, registry, index = context_for(tmp_path)

    def failed(*args):
        raise RuntimeError("lost hint")

    remove = index.add_due_listener(failed)
    index.sync_owner(OWNER, store(time.time() - 1))
    remove()
    assert index.listener_failures == 1
    hints = loops._OwnerWakeHints(context)
    controller = loops._GatewaySchedulerDueController(context)
    hints.bind_controller("scheduler_due", controller)
    assert hints.timer.snapshot()["live_keys"] == 0
    controller.tick()
    hints.refresh_scheduler()
    assert registry.snapshot()
    assert hints.timer.snapshot()["live_keys"] > 0
    hints.close()


def test_due_legacy_repair_notifies_after_commit(tmp_path):
    index = SchedulerDueIndex(tmp_path / "due.sqlite3")
    assert callable(getattr(index, "add_due_listener", None))
    home = tmp_path / "owners/providers/feishu/users/alice/data/scheduler"
    home.mkdir(parents=True)
    (home / "store.json").write_text(json.dumps({**store(200), "updated_at": 100}))
    seen = []
    remove = index.add_due_listener(lambda owner, due, revision: seen.append((owner, due)))
    try:
        index.repair_legacy_owner_ledgers(tmp_path / "owners")
        assert seen == [(KEY, 200)]
    finally:
        remove()


def test_idle_controller_queries_once_per_backstop(tmp_path, monkeypatch):
    from agent_py_agent.cli import gateway_loops as loops

    assert callable(getattr(loops, "_OwnerWakeHints", None))
    context, _registry, index = context_for(tmp_path)
    hints = loops._OwnerWakeHints(context)
    controller = loops._GatewaySchedulerDueController(context)
    hints.bind_controller("scheduler_due", controller)
    queried = []
    original = index.claim_due_owners

    def claim(**kwargs):
        queried.append(time.monotonic())
        return original(**kwargs)

    monkeypatch.setattr(index, "claim_due_owners", claim)
    stop = threading.Event()
    thread = threading.Thread(target=controller.run, args=(stop,))
    hints.start()
    thread.start()
    try:
        stop.wait(0.16)
    finally:
        stop.set()
        hints.close()
        thread.join(0.5)
    assert len(queried) == 3
    assert all(b - a >= 0.05 for a, b in zip(queried, queried[1:]))


def test_closed_hint_wait_cannot_lose_shutdown_to_clear(tmp_path):
    from agent_py_agent.cli import gateway_loops as loops
    context, _registry, _index = context_for(tmp_path)
    hints = loops._OwnerWakeHints(context)
    hints.close()
    event = hints.events["scheduler_due"]
    event.clear()  # close发生于消费者clear之前的确定性交错
    proxy = loops._HintedStopWait(threading.Event(), event, 0.5, hints._closed_event)
    started = time.monotonic()
    assert proxy.wait(0.5)
    assert time.monotonic() - started < 0.05


def test_backpressure_does_not_rearm_overdue_deadlines_in_a_hot_loop(tmp_path):
    from agent_py_agent.cli import gateway_loops as loops
    context, _registry, index = context_for(tmp_path)
    hints = loops._OwnerWakeHints(context)
    try:
        index.sync_owner(OWNER, store(time.time() - 10))
        hints.timer.cancel((KEY, "scheduler_due"))
        hints.refresh_scheduler()
        due, _revision = hints.timer._live[("scheduler-backstop", "scheduler_due")]
        assert due > time.time()
        hints._publish(KEY, "maintenance", time.time() - 10)
        before = hints.timer.snapshot()["generation"]
        hints._publish(KEY, "maintenance", time.time() - 9)
        assert hints.timer.snapshot()["generation"] == before
    finally:
        hints.close()


def test_unavailable_priority_index_cannot_prevent_authoritative_tick(tmp_path, monkeypatch):
    from agent_py_agent.cli import gateway_loops as loops
    context, _registry, _index = context_for(tmp_path)
    hints = loops._OwnerWakeHints(context)
    controller = loops._GatewayOwnerMaintenanceController(context)
    hints.bind_controller("maintenance", controller)
    called = []

    def unavailable(**kwargs):
        raise OSError("index unavailable")

    monkeypatch.setattr(hints.index, "priority_owners", unavailable)
    monkeypatch.setattr(controller, "tick", lambda: called.append(True))
    monkeypatch.setattr(loops, "_run_loop_with_backoff", lambda stop, tick, site: tick())
    try:
        controller.run(threading.Event())
        assert called == [True]
    finally:
        hints.close()
