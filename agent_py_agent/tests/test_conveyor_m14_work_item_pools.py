"""模型和工具等待复用线程时，按原工作项验证取消、收尾和计时。"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_model_generation as generation
from agent_py_agent.agent.agent_core.tool_loop import tool_wait_input
from agent_py_agent.agent.concurrency.interrupt import is_interrupted, set_interrupt
from agent_py_agent.agent.concurrency.work_item_pool import (
    WorkItemHandle,
    WorkItemObservation,
    WorkItemPool,
)
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFailureParams,
    ModelCallLedger,
    ModelCallStartedParams,
)
from agent_py_agent.agent.conversation import background_shutdown as shutdown
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.model_request_selection import model_request_selection_scope
from agent_py_agent.agent.runtime_context import ThreadLocalAgentAttribute
from agent_py_agent.tests.test_gateway_model_adoption import actual_request, fake_http


def _state(call_id):
    ledger = ModelCallLedger()
    ledger.started(ModelCallStartedParams(call_id=call_id, backend="fixture", model="fixture",
                                        input_tokens=0, output_tokens_estimate=0))
    return SimpleNamespace(liveness=generation._CallLiveness(), ledger=ledger, call_id=call_id)


@pytest.fixture
def mock_guard(monkeypatch):
    monkeypatch.setattr(generation, "_effective_model_request_timeout_seconds", lambda *_: 15.0)

    def call(call_id="mock-call", *, agent=None, request_id="mock-request", state=None):
        request = SimpleNamespace(agent=agent or SimpleNamespace(backend=None), prompt=call_id,
                                  params=SimpleNamespace(request_id=request_id))
        return generation._generate_through_wall_guard(request, state if state is not None else _state(call_id))

    return call


@pytest.fixture
def install_pool(monkeypatch, request):
    def install(module, name, *, capacity=1):
        pool = WorkItemPool(f"m14-{name.lower()}", max_threads=capacity)
        monkeypatch.setattr(module, name, pool, raising=False)
        request.addfinalizer(pool._executor.shutdown)
        return pool

    return install


def _capture_handles(monkeypatch):
    handles = []
    original = generation._wait_for_generation_result

    def wait(request, state, results, worker, timeout, **kwargs):
        handles.append(worker)
        return original(request, state, results, worker, timeout, **kwargs)

    monkeypatch.setattr(generation, "_wait_for_generation_result", wait)
    return handles


def _wait_idle(pool, count=1):
    # join 证明工作项退出。复用断言还需等线程重新领取，避免测进调度空档。
    deadline = time.monotonic() + 5
    queue = pool._queue
    while time.monotonic() < deadline:
        with queue.mutex:
            if len(queue.not_empty._waiters) >= count:
                return
        time.sleep(0)
    pytest.fail(f"expected {count} idle workers")


def test_200_model_calls_create_no_per_call_guard_threads(monkeypatch, mock_guard):
    constructions = []
    original = threading.Thread.__init__

    def init(thread, *args, **kwargs):
        original(thread, *args, **kwargs)
        if thread.name == "my-agent-model-generate-timeout-guard":
            constructions.append(thread.name)

    monkeypatch.setattr(threading.Thread, "__init__", init)
    monkeypatch.setattr(generation, "_generate_backend_response", lambda *_: "ok")
    for index in range(200):
        assert mock_guard(f"mock-{index}") == "ok"
    assert len(constructions) == 0


def test_gateway_stages_count_only_executed_model_segments(tmp_path, monkeypatch):
    fixture = actual_request(tmp_path, mode="disabled")
    business, _ = fake_http(monkeypatch, fixture)
    stages = {"lane_queue_ms": 17}
    context = replace(fixture.context, stages=stages)

    request_execution._run_gateway_ask(context)

    assert len(business) == 1
    assert type(stages["first_model_request_ms"]) is int
    assert type(stages["model_wait_ms"]) is int
    assert stages["lane_queue_ms"] == 17
    segment, = stages["model_wait_segments_ms"]
    assert segment["started_ms"] == stages["first_model_request_ms"] <= segment["ended_ms"]


def test_late_interrupt_of_generation_does_not_touch_reused_thread(monkeypatch, mock_guard, install_pool):
    pool = install_pool(generation, "MODEL_CALL_POOL")
    handles = _capture_handles(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    workers = []

    def generate(_request, state, _timeout):
        workers.append(threading.current_thread())
        if state.call_id == "next":
            entered.set()
            assert release.wait(4)
        return is_interrupted()

    monkeypatch.setattr(generation, "_generate_backend_response", generate)
    assert mock_guard("first") is False
    handles[0].join(4)
    if isinstance(handles[0], threading.Thread):
        pytest.fail("model generation returned a thread instead of a work-item handle")
    _wait_idle(pool)
    with ThreadPoolExecutor(max_workers=1) as callers:
        next_call = callers.submit(mock_guard, "next")
        try:
            assert entered.wait(4)
            generation._interrupt_generation_worker(handles[0])
        finally:
            release.set()
        assert next_call.result(4) is False
    assert handles[1].join(4)
    assert workers[0] is workers[1]


@pytest.mark.parametrize("cancel", [False, True])
def test_shutdown_observes_item_before_release_through_completion(monkeypatch, request, cancel):
    start = getattr(shutdown, "start_background_model_work_item", None)
    assert callable(start)
    pool = WorkItemPool("m14-drain", max_threads=1)
    request.addfinalizer(pool._executor.shutdown)
    owner = shutdown.BackgroundShutdown()
    registered, release_start, entered, release_work = (threading.Event() for _ in range(4))
    workers = []

    def work():
        workers.append(threading.current_thread())
        entered.set()
        release_work.wait(4)

    original = WorkItemObservation.start

    def pause_before_start(observation):
        registered.set()
        assert release_start.wait(4)
        original(observation)

    monkeypatch.setattr(WorkItemObservation, "start", pause_before_start)
    handle = pool.prepare(work, context=copy_context())
    with ThreadPoolExecutor(max_workers=1) as callers:
        try:
            with owner.observe(None):
                caller = callers.submit(copy_context().run, start, handle)
                assert registered.wait(4)
                item, = owner.slices
                assert len(item.workers) == 1
                assert owner.blockers() == [{"reason": "background_slice_running", "ref": "submitted_slice"}]
            blocker = {"reason": "background_model_worker_unsettled", "ref": "original_worker"}
            assert owner.blockers() == [blocker]
            if cancel:
                handle.interrupt()
                assert handle.proven_not_started() and not owner.blockers()
            release_start.set()
            assert caller.result(4) is None
            if cancel:
                assert not entered.is_set()
                return
            assert entered.wait(4) and owner.blockers() == [blocker]
            release_work.set()
            assert handle.join(4) and not owner.blockers()
            _wait_idle(pool)
            next_entered = threading.Event()

            def next_work():
                workers.append(threading.current_thread())
                next_entered.set()

            next_handle = pool.submit(next_work, context=copy_context())
            assert next_entered.wait(4) and next_handle.join(4)
            assert workers[0] is workers[1] and not owner.blockers()
            assert item.exited_worker_count == 1
        finally:
            release_start.set()
            release_work.set()


@pytest.mark.parametrize("proven_not_started", [False, True])
def test_model_item_start_error_releases_only_proven_nonstart(monkeypatch, request, proven_not_started):
    pool = WorkItemPool("m14-start-error", max_threads=1)
    request.addfinalizer(pool._executor.shutdown)
    owner = shutdown.BackgroundShutdown()
    original_release = pool._release
    failure = RuntimeError("simulated work item start error")
    entered, release = threading.Event(), threading.Event()
    notifications = []

    def work():
        entered.set()
        release.wait(4)

    def start_error(handle):
        if proven_not_started:
            assert handle.cancel_if_not_started()
        else:
            original_release(handle)
            assert entered.wait(4)
        raise failure

    handle = pool.prepare(work, context=copy_context())
    monkeypatch.setattr(pool, "_release", start_error)
    try:
        with owner.observe(None), pytest.raises(RuntimeError) as caught:
            shutdown.start_background_model_work_item(handle, on_proven_not_started=lambda: notifications.append(True))
        assert caught.value is failure
        assert handle.proven_not_started() is proven_not_started
        assert notifications == ([True] if proven_not_started else [])
        assert bool(owner.blockers()) is not proven_not_started
    finally:
        release.set()
    assert handle.join(4) and not owner.blockers()


class _ThreadScopedAgent:
    _current_run_params = ThreadLocalAgentAttribute("_current_run_params")
    backend = None


def test_reused_model_thread_clears_run_params_context_and_interrupt(monkeypatch, mock_guard, install_pool):
    pool = install_pool(generation, "MODEL_CALL_POOL")
    handles = _capture_handles(monkeypatch)
    idle = threading.Event()
    queue_wait = pool._queue.not_empty.wait
    local_context = ContextVar("m14_model_context", default=None)
    agent = _ThreadScopedAgent()
    caller_params = object()
    dirty_params = object()
    first_state, next_state = _state("first"), _state("next")

    def wait_for_item(timeout=None):
        # 调用方的下一次提交会等队列锁，直到此线程已经登记为空闲。
        idle.set()
        return queue_wait(timeout)

    def settle_item():
        assert handles.pop().join(4) and idle.wait(4)
        idle.clear()

    def generate(_request, state, _timeout):
        assert not is_interrupted()
        if state.call_id == "first":
            assert agent._current_run_params is caller_params
            assert local_context.get() == "caller"
            agent._current_run_params = dirty_params
            local_context.set("worker")
            set_interrupt(True)
            assert is_interrupted()
        else:
            assert not hasattr(agent, "_current_run_params")
            assert local_context.get() is None
        return threading.current_thread()

    monkeypatch.setattr(generation, "_generate_backend_response", generate)
    monkeypatch.setattr(pool._queue.not_empty, "wait", wait_for_item)
    for _ in range(10_000):
        agent._current_run_params = caller_params
        token = local_context.set("caller")
        try:
            first = mock_guard("first", agent=agent, state=first_state)
            assert agent._current_run_params is caller_params and local_context.get() == "caller"
        finally:
            del agent._current_run_params
            local_context.reset(token)
        settle_item()
        assert mock_guard("next", agent=agent, state=next_state) is first
        settle_item()


def test_model_thread_creations_stay_at_128_over_twenty_concurrent_waves(monkeypatch, mock_guard, install_pool):
    pool = install_pool(generation, "MODEL_CALL_POOL", capacity=512)
    handles = _capture_handles(monkeypatch)
    constructions = []
    original = threading.Thread.__init__

    def init(thread, *args, **kwargs):
        original(thread, *args, **kwargs)
        if thread.name.startswith(pool.name):
            constructions.append(thread)

    def generate(*_):
        wave.wait(12)
        return threading.current_thread()

    monkeypatch.setattr(threading.Thread, "__init__", init)
    monkeypatch.setattr(generation, "_generate_backend_response", generate)
    with ThreadPoolExecutor(max_workers=128) as callers:
        for index in range(20):
            wave = threading.Barrier(129)
            calls = [callers.submit(mock_guard, f"wave-{index}-{slot}") for slot in range(128)]
            wave.wait(12)
            assert {call.result(12) for call in calls} == set(constructions)
            assert len(handles) == 128 and all(handle.join(4) for handle in handles)
            handles.clear()
            _wait_idle(pool, 128)
    assert len(constructions) == 128


def test_tool_wait_join_tracks_work_item_and_preserves_each_context(monkeypatch, install_pool):
    pool = install_pool(tool_wait_input, "TOOL_WAIT_POOL")
    request = SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(tool_wait_input_reply_enabled=False)),
                              params=SimpleNamespace())
    context_value = ContextVar("m14_tool_wait_context", default=None)
    release_tool, callback_entered, release_callback, joining = (threading.Event() for _ in range(4))

    def run():
        assert release_tool.wait(4)
        return threading.current_thread(), context_value.get()

    def item(value):
        context = copy_context()
        context.run(context_value.set, value)
        return SimpleNamespace(captured_context=context, run=run)

    def done(_):
        callback_entered.set()
        release_callback.wait(4)

    future = tool_wait_input.submit_wait_item(item("first"))
    future.add_done_callback(done)
    handle = future._tool_wait_worker
    assert isinstance(handle, WorkItemHandle)
    join = WorkItemHandle.join

    def observed_join(self, *args, **kwargs):
        if self is handle:
            joining.set()
        return join(self, *args, **kwargs)

    monkeypatch.setattr(WorkItemHandle, "join", observed_join)
    with ThreadPoolExecutor(max_workers=1) as callers:
        try:
            release_tool.set()
            assert callback_entered.wait(4) and future.done() and not handle.done()
            caller = callers.submit(tool_wait_input.await_tool_futures, request, [future])
            assert joining.wait(4) and not caller.done()
        finally:
            release_tool.set()
            release_callback.set()
        (worker, value), = caller.result(4)
    _wait_idle(pool)
    assert value == "first"
    next_future = tool_wait_input.submit_wait_item(item("second"))
    assert tool_wait_input.await_tool_futures(request, [next_future]) == [(worker, "second")]
    assert handle.done() and next_future._tool_wait_worker.done() and context_value.get() is None


@pytest.mark.parametrize("cancel", [False, True])
def test_global_model_blocker_is_tied_to_exact_item(monkeypatch, mock_guard, install_pool, cancel):
    install_pool(generation, "MODEL_CALL_POOL")
    entered, release = threading.Event(), threading.Event()
    executed = threading.Event()
    handles = _capture_handles(monkeypatch)
    original_start = WorkItemHandle.start
    call_id = "m14-exact-model-retention"
    state = _state(call_id)
    caller_threads = []

    def start(handle):
        if cancel:
            entered.set()
            assert release.wait(4)
        original_start(handle)

    def generate(*_):
        executed.set()
        entered.set()
        assert release.wait(4)
        return "ok"

    def call():
        caller_threads.append(threading.get_ident())
        try:
            return mock_guard(call_id, state=state)
        finally:
            set_interrupt(False)

    monkeypatch.setattr(WorkItemHandle, "start", start)
    monkeypatch.setattr(generation, "_generate_backend_response", generate)
    exact_blocker = {"reason": "model_call_retained", "ref": call_id}
    with ThreadPoolExecutor(max_workers=1) as callers:
        pending = callers.submit(call)
        try:
            assert entered.wait(4)
            state.ledger.failed(ModelCallFailureParams(call_id=call_id, error_type="logical_terminal"))
            assert exact_blocker in shutdown.model_shutdown_blockers()
            if cancel:
                set_interrupt(True, caller_threads[0])
        finally:
            release.set()
        if cancel:
            with pytest.raises(InterruptedError):
                pending.result(4)
            assert handles[0].proven_not_started() and not executed.is_set()
        else:
            assert pending.result(4) == "ok" and executed.is_set()
    assert handles[0].join(4) and exact_blocker not in shutdown.model_shutdown_blockers()


def test_model_blocker_remains_until_worker_local_cleanup_finishes(monkeypatch, mock_guard, install_pool):
    install_pool(generation, "MODEL_CALL_POOL")
    entered, release = threading.Event(), threading.Event()
    handles = _capture_handles(monkeypatch)
    call_id = "m14-local-cleanup"
    state = _state(call_id)

    class BlockingRunParams(ThreadLocalAgentAttribute):
        def __delete__(self, instance):
            entered.set()
            assert release.wait(4)
            super().__delete__(instance)

    class Agent:
        backend = None
        _current_run_params = BlockingRunParams("_current_run_params")

    agent = Agent()
    agent._current_run_params = object()
    monkeypatch.setattr(generation, "_generate_backend_response", lambda *_: "ok")
    exact_blocker = {"reason": "model_call_retained", "ref": call_id}
    try:
        assert mock_guard(call_id, agent=agent, state=state) == "ok"
        assert entered.wait(4)
        state.ledger.failed(ModelCallFailureParams(call_id=call_id, error_type="logical_terminal"))
        assert exact_blocker in shutdown.model_shutdown_blockers() and not handles[0].done()
    finally:
        release.set()
    assert handles[0].join(4) and exact_blocker not in shutdown.model_shutdown_blockers()


def test_model_stages_preserve_overlaps_gaps_and_incomplete_segments(monkeypatch, mock_guard, install_pool):
    install_pool(generation, "MODEL_CALL_POOL", capacity=3)
    stages = {}
    agent = _ThreadScopedAgent()
    clock_ms = [0]
    entered = {name: threading.Event() for name in ("first", "parallel", "late")}
    release = {name: threading.Event() for name in entered}
    request_id = "mock-request"

    def generate(_request, state, _timeout):
        entered[state.call_id].set()
        assert release[state.call_id].wait(4)
        return "ok"

    def direct(_backend, prompt, _state):
        if prompt == "direct":
            clock_ms[0] = 80
        return "ok"

    monkeypatch.setattr(request_execution, "monotonic_ns", lambda: clock_ms[0] * 1_000_000)
    monkeypatch.setattr(generation, "_generate_backend_response", generate)
    monkeypatch.setattr(generation, "_invoke_backend_generate", direct)
    with ThreadPoolExecutor(max_workers=3) as callers:
        try:
            with request_execution._ModelRequestStageTiming() as timing, model_request_selection_scope(
                SimpleNamespace(model_request_stage_timing=timing),
            ):
                timing.bind(agent, stages, request_id=request_id)
                def submit(name, start_ms):
                    clock_ms[0] = start_ms
                    pending = callers.submit(copy_context().run, mock_guard, name, agent=agent)
                    assert entered[name].wait(4)
                    return pending

                first = submit("first", 10)
                parallel = submit("parallel", 20)
                clock_ms[0] = 35
                release["first"].set()
                assert first.result(4) == "ok"
                clock_ms[0] = 45
                release["parallel"].set()
                assert parallel.result(4) == "ok"
                with monkeypatch.context() as patch:
                    patch.setattr(generation, "_effective_model_request_timeout_seconds", lambda *_: 0)
                    assert mock_guard("other-request", agent=agent, request_id="other-request") == "ok"
                    clock_ms[0] = 60
                    assert mock_guard("direct", agent=agent) == "ok"
                late = submit("late", 85)
            clock_ms[0] = 100
            release["late"].set()
            assert late.result(4) == "ok"
        finally:
            for event in release.values():
                event.set()
    assert stages == {
        "first_model_request_ms": 10,
        "model_wait_ms": 55,
        "model_wait_segments_ms": [
            {"started_ms": 10, "ended_ms": 35},
            {"started_ms": 20, "ended_ms": 45},
            {"started_ms": 60, "ended_ms": 80},
            {"started_ms": 85, "ended_ms": None},
        ],
    }


def g1_case_registered_model_work_item():
    pool = WorkItemPool("m14-g1", max_threads=1)
    handle = pool.prepare(lambda: None, context=copy_context())
    return shutdown._RegisteredModelWorkObservation(handle).exited
