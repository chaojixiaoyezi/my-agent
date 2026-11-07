"""提示索引失败不能阻断原后台循环；失败构造和退出均不残留提示资源。"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import owner_wake_discovery
from agent_py_agent.agent.runtime_db import input_lifecycle
from agent_py_agent.agent.scheduler import due_index
from agent_py_agent.cli import gateway_loops as loops


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """只替换领域控制器，保留真实组合根、线程启动、tick/backoff、索引及订阅。"""
    home = SimpleNamespace(root=tmp_path, global_index_dir=tmp_path / "global_index",
        owners_dir=tmp_path / "owners", owner_home_dir=tmp_path / "owners/local/main")
    context = SimpleNamespace(agent=SimpleNamespace(home_paths=home,
        config=SimpleNamespace(owner_maintenance_scan_interval_seconds=0.01),
        scheduler_repository=SimpleNamespace(due_index=due_index.SchedulerDueIndex(
            home.global_index_dir / "scheduler_due.sqlite3")), _owner_wake_hints=None))
    state = SimpleNamespace(context=context, stop=threading.Event(), controllers={}, supervisors=[],
        hints=[], timers=[], errors=[], escaped=[])
    original_init = loops._OwnerWakeHints.__init__
    original_timer = loops.TimerService
    original_report = loops._print_gateway_loop_error

    def hint_init(self, current):
        state.hints.append(self)
        original_init(self, current)

    def timer(**kwargs):
        instance = original_timer(**kwargs)
        state.timers.append(instance)
        return instance

    def report(site, worker, exc):
        state.errors.append((site, worker, exc))
        original_report(site, worker, exc)

    class Controller:
        interval = 0.01

        def __init__(self, kind):
            assert kind not in state.controllers
            self.kind, self.ticks = kind, 0
            self.started, self.finished = threading.Event(), threading.Event()
            self._priority, self._base_home = {}, home
            state.controllers[kind] = self

        def tick(self):
            self.ticks += 1
            self.started.set()

        def run(self, stop):
            self.thread = threading.current_thread()
            try:
                loops._run_hinted_controller(self, stop,
                    loops._LoopSite(self.kind, self.kind, self.kind, self.interval))
            finally:
                self.finished.set()

    class Supervisor:
        poll_interval = 0.001

        def __init__(self, current):
            assert current is context
            self.ticks, self.closed = 0, 0
            state.supervisors.append(self)

        def tick(self):
            assert set(state.controllers) == {"reconcile", "maintenance", "scheduler_due"}
            assert all(controller.started.wait(1) for controller in state.controllers.values())
            self.ticks += 1
            if self.ticks == 3:
                state.stop.set()
            return False

        def shutdown(self):
            self.closed += 1

    monkeypatch.setattr(loops._OwnerWakeHints, "__init__", hint_init)
    monkeypatch.setattr(loops, "TimerService", timer)
    monkeypatch.setattr(loops, "_print_gateway_loop_error", report)
    monkeypatch.setattr(loops, "_BackgroundMainSupervisor", Supervisor)
    monkeypatch.setattr(loops, "_GatewayOrphanReconciler", lambda current: Controller("reconcile"))
    monkeypatch.setattr(loops, "_GatewayOwnerMaintenanceController", lambda current: Controller("maintenance"))
    monkeypatch.setattr(loops, "_GatewaySchedulerDueController", lambda current: Controller("scheduler_due"))
    before_wake = dict(input_lifecycle._WAKE_LISTENERS)
    due_key = str(context.agent.scheduler_repository.due_index.path)
    before_due = dict(due_index._DUE_LISTENERS.get(due_key, {}))
    binding_key = str(home.owners_dir.resolve())
    before_binding = owner_wake_discovery._ACTIVITY_BINDINGS.get(binding_key)
    yield state
    state.stop.set()
    for hint in state.hints:
        for remove in getattr(hint, "_removers", ()):
            remove()
        discovery = getattr(hint, "discovery", None)
        if discovery is not None:
            discovery.close()
        for event in getattr(hint, "events", {}).values():
            event.set()
    for timer_instance in state.timers:
        timer_instance.stop(1)
    for controller in state.controllers.values():
        if hasattr(controller, "thread"):
            controller.thread.join(1)
    assert dict(input_lifecycle._WAKE_LISTENERS) == before_wake
    assert dict(due_index._DUE_LISTENERS.get(due_key, {})) == before_due
    assert owner_wake_discovery._ACTIVITY_BINDINGS.get(binding_key) is before_binding


def run_background(state):
    def run():
        try:
            loops._gateway_background_main_loop(state.context, state.stop)
        except Exception as exc:
            state.escaped.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(3)
    try:
        assert not thread.is_alive(), "初始化故障不能让循环卡在重试中"
        assert not state.escaped, state.escaped
        assert state.stop.is_set()
        assert len(state.supervisors) == 1
        assert state.supervisors[0].ticks == 3
        assert state.supervisors[0].closed == 1
        assert set(state.controllers) == {"reconcile", "maintenance", "scheduler_due"}
        for controller in state.controllers.values():
            assert controller.ticks >= 1
            assert controller.finished.is_set()
            assert not controller.thread.is_alive()
        assert all(not timer.snapshot()["running"] for timer in state.timers)
    finally:
        state.stop.set()
        for hint in state.hints:
            for event in getattr(hint, "events", {}).values():
                event.set()
        thread.join(1)


@pytest.mark.parametrize("failure", ["once", "persistent", "corrupt"])
def test_activity_index_failure_starts_all_loops_hint_free(runtime, monkeypatch, failure):
    attempts = []
    original_connect = loops.OwnerActivityIndex._connect
    error = OSError("simulated owner activity index I/O error")
    if failure == "corrupt":
        path = runtime.context.agent.home_paths.global_index_dir / "owner_activity.sqlite3"
        path.write_bytes(b"not a sqlite database")

    def connect(index):
        attempts.append(index.path)
        if failure == "persistent" or failure == "once" and len(attempts) == 1:
            raise error
        return original_connect(index)

    monkeypatch.setattr(loops.OwnerActivityIndex, "_connect", connect)
    run_background(runtime)
    assert len(attempts) == 1, "降级不应重试构造或阻塞核心循环"
    assert len(runtime.hints) == 1 and len(runtime.timers) == 1
    assert len(runtime.errors) == 1
    assert runtime.errors[0][:2] == ("gateway_owner_wake_hints.initialize", "background-main")
    if failure != "corrupt":
        assert runtime.errors[0][2] is error
    assert runtime.context.agent._owner_wake_hints is None
    assert getattr(runtime.supervisors[0], "_wake_hints", None) is None
    assert all(getattr(controller, "_wake_hints", None) is None for controller in runtime.controllers.values())
    assert runtime.timers[0]._thread is None


@pytest.mark.parametrize("failure", ["wake_listener", "discovery"])
def test_partial_construction_removes_registered_listeners(runtime, monkeypatch, failure):
    handles, removed = [], []
    original_due = due_index.SchedulerDueIndex.add_due_listener
    original_wake = input_lifecycle.add_wake_listener
    error = OSError("simulated late hint initialization error")

    def track(remove):
        def cleanup():
            removed.append(remove)
            remove()
        handles.append(remove)
        return cleanup

    def add_due(index, listener):
        return track(original_due(index, listener))

    def add_wake(listener):
        if failure == "wake_listener":
            raise error
        return track(original_wake(listener))

    def fail_discovery(*args, **kwargs):
        raise error

    monkeypatch.setattr(due_index.SchedulerDueIndex, "add_due_listener", add_due)
    monkeypatch.setattr(input_lifecycle, "add_wake_listener", add_wake)
    monkeypatch.setattr(loops, "bind_owner_activity_index", fail_discovery)
    hint = object.__new__(loops._OwnerWakeHints)
    try:
        with pytest.raises(OSError) as caught:
            hint.__init__(runtime.context)
        assert caught.value is error
        assert removed == handles and handles
        assert hint._removers == []
        assert runtime.context.agent._owner_wake_hints is None
        assert runtime.timers[0]._thread is None
    finally:
        for remove in handles:
            remove()


@pytest.mark.parametrize("failure", ["bind_controller", "start"])
def test_activation_failure_closes_built_hints_before_fallback(runtime, monkeypatch, failure):
    original = getattr(loops._OwnerWakeHints, failure)
    original_close = loops._OwnerWakeHints.close
    closed = []
    error = OSError("simulated hint activation error")

    def fail(self, *args):
        original(self, *args)
        raise error

    def close(self):
        closed.append(self)
        original_close(self)

    monkeypatch.setattr(loops._OwnerWakeHints, failure, fail)
    monkeypatch.setattr(loops._OwnerWakeHints, "close", close)
    run_background(runtime)
    assert closed == runtime.hints and len(closed) == 1
    assert runtime.errors == [("gateway_owner_wake_hints.initialize", "background-main", error)]
    assert runtime.context.agent._owner_wake_hints is None
    assert getattr(runtime.supervisors[0], "_wake_hints", None) is None
    assert all(getattr(controller, "_wake_hints", None) is None for controller in runtime.controllers.values())


def test_healthy_hints_start_once_and_shutdown_removes_resources(runtime, monkeypatch):
    starts, closes = [], []
    original_start, original_close = loops._OwnerWakeHints.start, loops._OwnerWakeHints.close

    def start(self):
        starts.append(self)
        original_start(self)

    def close(self):
        closes.append(self)
        original_close(self)

    monkeypatch.setattr(loops._OwnerWakeHints, "start", start)
    monkeypatch.setattr(loops._OwnerWakeHints, "close", close)
    run_background(runtime)
    assert starts == closes == runtime.hints and len(starts) == 1
    assert runtime.errors == []
    assert runtime.hints[0]._closed_event.is_set()
    assert getattr(runtime.context.agent, "_owner_wake_hints", None) is None
