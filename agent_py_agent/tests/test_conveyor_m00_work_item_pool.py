# LLM: Conveyor M00 / R3-G0：WorkItemPool 复用模型调用/工具等待线程但绝不排队——有空闲线程就用、否则扩容到
#   max_threads、再超出就起一次性守护线程；每项前后都清中断旗与登记的线程局部、在调用方给的 context 里跑；
#   句柄状态 PREPARED → RELEASED → RUNNING → DONE / CANCELLED 只在句柄锁内比较交换：interrupt 只落在“本项运行中”，
#   开始前变取消、结束后无操作，绝不漏到同线程下一项；“未开始”只由 CANCELLED 证明。LIFO 队列保持 http_lanes 原操作。
# 模块用途: 固定 agent/concurrency/work_item_pool.py 的池合同（C1.7）、观察协议（C5.2）与 lifo_work_queue（C1.6），
#   不起 Gateway、不触网。
from __future__ import annotations

import contextvars
import inspect
import random
import sys
import threading
import time
import types

import pytest

from agent_py_agent.agent.concurrency import work_item_pool
from agent_py_agent.agent.concurrency.interrupt import is_interrupted, set_interrupt
from agent_py_agent.agent.concurrency.lifo_work_queue import CancellableWorkQueue
from agent_py_agent.agent.concurrency.work_item_pool import (
    MODEL_CALL_POOL,
    TOOL_WAIT_POOL,
    WorkItemHandle,
    WorkItemObservation,
    WorkItemPool,
    register_item_local_reset,
)

# 同时在飞的工作项数（R3 §8 G0：600 个并发提交）。
BURST_ITEM_COUNT = 600
# 每项从提交到开始执行的上限秒数（50 ms）。
START_LATENCY_LIMIT_SECONDS = 0.05
# 突发用例私有池的线程上限：小于突发数，迫使“扩容”与“一次性溢出线程”两条路径都被走到。
BURST_POOL_MAX_THREADS = 64
# 中断/完成竞争轮数（10^4）。
INTERRUPT_RACE_ROUNDS = 10_000
# 竞争用例里中断方与工作项各自随机空转的最大迭代数，用来把中断打在开始前、运行中、结束后三个时段。
RACE_SPIN_MAX_ITERATIONS = 3000
# 竞争用例期间的解释器线程切换间隔（1 µs）：有 GIL 的构建默认 5 ms 才切换，两侧空转无法交错；无 GIL 构建不受影响。
RACE_SWITCH_INTERVAL_SECONDS = 1e-6
# 阻塞等待的上限秒数。
WAIT_SECONDS = 10
# 观察预备态工作项“确实没人运行”的等待秒数。
PREPARED_IDLE_SECONDS = 0.05
# 短超时探测（join/wait_until 在工作项仍运行时必须按期返回 False）的秒数。
SHORT_WAIT_SECONDS = 0.01

_LOCAL = threading.local()
_VAR = contextvars.ContextVar("m00_item_var", default="unset")


# 函数用途: 登记给池的线程局部复位：清掉上一项留在本模块线程局部上的值。
def _reset_local() -> None:
    _LOCAL.__dict__.clear()


# 函数用途: 等到至少 count 个消费者挂在队列条件变量上，避免依赖固定睡眠。
def _wait_waiters(queue: CancellableWorkQueue, count: int) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        with queue.mutex:
            if len(queue.not_empty._waiters) >= count:
                return
        assert time.monotonic() < deadline, "queue consumer never parked"
        time.sleep(0)


# 函数用途: 等到池里至少 count 个 worker 空闲挂起（可接手）。
def _wait_parked(pool: WorkItemPool, count: int = 1) -> None:
    _wait_waiters(pool._queue, count)


# 函数用途: 忙等指定迭代数，制造亚毫秒级的时序差。
def _spin(iterations: int) -> None:
    for _ in range(iterations):
        pass


def test_public_surface_and_module_pools():
    assert work_item_pool.WORK_ITEM_POOL_MAX_THREADS_COUNT == 512
    assert (MODEL_CALL_POOL.name, MODEL_CALL_POOL.max_threads) == ("my-agent-model-pool", 512)
    assert (TOOL_WAIT_POOL.name, TOOL_WAIT_POOL.max_threads) == ("my-agent-tool-wait-pool", 512)
    for method in (WorkItemPool.prepare, WorkItemPool.submit):
        params = inspect.signature(method).parameters
        assert list(params) == ["self", "fn", "context", "on_start"]
        assert params["fn"].kind is inspect.Parameter.POSITIONAL_ONLY
        assert params["context"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["context"].default is inspect.Parameter.empty
        assert params["on_start"].kind is inspect.Parameter.KEYWORD_ONLY and params["on_start"].default is None
    assert inspect.signature(WorkItemPool).parameters["max_threads"].kind is inspect.Parameter.KEYWORD_ONLY
    for name in ("start", "interrupt", "cancel_if_not_started", "proven_not_started", "done", "is_alive"):
        assert list(inspect.signature(getattr(WorkItemHandle, name)).parameters) == ["self"], name
    assert list(inspect.signature(WorkItemHandle.join).parameters) == ["self", "timeout"]
    assert isinstance(WorkItemObservation.attempted, property)
    for name in ("start", "proven_not_started", "exited"):
        assert list(inspect.signature(getattr(WorkItemObservation, name)).parameters) == ["self"], name
    assert list(inspect.signature(WorkItemObservation.wait_until).parameters) == ["self", "deadline"]
    with pytest.raises(ValueError):
        WorkItemPool("m00-empty", max_threads=0)
    names = []
    handle = MODEL_CALL_POOL.submit(lambda: names.append(threading.current_thread().name),
                                    context=contextvars.copy_context())
    assert handle.join(WAIT_SECONDS)
    assert names and names[0].startswith("my-agent-model-pool")


def test_lifo_work_queue_wakes_the_newest_waiter_and_discards_by_owner():
    queue = CancellableWorkQueue()
    got: list = []

    def consume(label: str) -> None:
        got.append((label, queue.get(timeout=WAIT_SECONDS)))

    older = threading.Thread(target=consume, args=("older",), daemon=True)
    older.start()
    _wait_waiters(queue, 1)
    newer = threading.Thread(target=consume, args=("newer",), daemon=True)
    newer.start()
    _wait_waiters(queue, 2)
    queue.put("a")
    newer.join(WAIT_SECONDS)
    assert got == [("newer", "a")]
    queue.put("b")
    older.join(WAIT_SECONDS)
    assert got == [("newer", "a"), ("older", "b")]
    assert not queue.needs_worker() and queue.not_empty.pending_wakeups == 0
    owner, other = object(), object()
    unrelated = types.SimpleNamespace(future=other, args=(other,))
    by_future = types.SimpleNamespace(future=owner, args=())
    by_args = types.SimpleNamespace(future=None, args=(owner, 1))
    by_task = types.SimpleNamespace(future=None, args=(), task=(len, (owner,), {}))
    for item in (unrelated, by_future, by_args, by_task):
        queue.put(item)
    assert queue.needs_worker()
    unfinished = queue.unfinished_tasks
    for remaining in ([unrelated, by_args, by_task], [unrelated, by_task], [unrelated], [unrelated]):
        assert queue.discard(owner) is None
        assert list(queue.queue) == remaining
    assert queue.unfinished_tasks == unfinished - 3


def test_burst_items_all_start_promptly_without_queueing():
    pool = WorkItemPool("m00-burst", max_threads=BURST_POOL_MAX_THREADS)
    release = threading.Event()
    started: list[float | None] = [None] * BURST_ITEM_COUNT
    submitted = [0.0] * BURST_ITEM_COUNT
    handles = []
    try:
        for index in range(BURST_ITEM_COUNT):
            def blocked(index: int = index) -> None:
                started[index] = time.perf_counter()
                release.wait(WAIT_SECONDS)

            submitted[index] = time.perf_counter()
            handles.append(pool.submit(blocked, context=contextvars.copy_context()))
        deadline = time.monotonic() + WAIT_SECONDS
        while any(value is None for value in started) and time.monotonic() < deadline:
            time.sleep(0.001)
        assert all(value is not None for value in started)
        latencies = sorted(begin - queued for begin, queued in zip(started, submitted))
        assert latencies[-1] < START_LATENCY_LIMIT_SECONDS, latencies[-5:]
        names = [thread.name for thread in threading.enumerate()]
        assert sum(name.startswith("m00-burst_") for name in names) == BURST_POOL_MAX_THREADS
        assert sum(name == "m00-burst-overflow" for name in names) == BURST_ITEM_COUNT - BURST_POOL_MAX_THREADS
    finally:
        release.set()
        assert all(handle.join(WAIT_SECONDS) for handle in handles)
    assert all(handle.done() for handle in handles)


# 函数用途: 竞争用例的主体：逐轮预备并交付、随机空转后中断或取消，记录各时段结果、漏旗轮次与同线程复用次数。
def _race_rounds(pool: WorkItemPool, rng: random.Random, outcomes: dict, leaked: list) -> int:
    same_thread = 0
    for round_index in range(INTERRUPT_RACE_ROUNDS):
        if round_index:
            _wait_parked(pool)
        work = rng.randrange(RACE_SPIN_MAX_ITERATIONS)
        delay = rng.randrange(RACE_SPIN_MAX_ITERATIONS)
        use_cancel = round_index % 2 == 1
        seen: list = []
        box: list = []

        def first(work: int = work, seen: list = seen, box: list = box) -> None:
            seen.append((threading.get_ident(), box[0].ident))
            _spin(work)
            seen.append(is_interrupted())

        handle = pool.prepare(first, context=contextvars.Context())
        box.append(handle)
        handle.start()
        _spin(delay)
        if use_cancel:
            cancelled = handle.cancel_if_not_started()
            if not cancelled:
                handle.interrupt()
        else:
            handle.interrupt()
            cancelled = None
        assert handle.join(WAIT_SECONDS)
        assert not handle.is_alive() and handle.ident is None
        if handle.proven_not_started():
            assert not seen and not handle.done() and cancelled in (True, None)
            outcomes["not_started"] += 1
        else:
            ident, own_ident = seen[0]
            assert len(seen) == 2 and own_ident == ident and handle.done() and cancelled in (False, None)
            outcomes["interrupted" if seen[1] else "after_finish"] += 1
        _wait_parked(pool)
        probe: list = []
        assert pool.submit(lambda probe=probe: probe.append((threading.get_ident(), is_interrupted())),
                           context=contextvars.Context()).join(WAIT_SECONDS)
        ident, flagged = probe[0]
        if flagged:
            leaked.append(round_index)
        if seen and ident == seen[0][0]:
            same_thread += 1
    return same_thread


def test_interrupt_never_leaks_into_the_next_item_on_the_same_thread():
    pool = WorkItemPool("m00-race", max_threads=1)
    rng = random.Random(10_000)
    outcomes = {"not_started": 0, "interrupted": 0, "after_finish": 0}
    leaked: list = []
    previous_interval = sys.getswitchinterval()
    sys.setswitchinterval(RACE_SWITCH_INTERVAL_SECONDS)
    try:
        same_thread = _race_rounds(pool, rng, outcomes, leaked)
    finally:
        sys.setswitchinterval(previous_interval)
    assert not leaked
    assert all(outcomes.values()), outcomes
    # 复用同一线程的轮次才真正检验“不漏旗”；单线程池在 worker 挂起后提交，几乎每轮都应复用。
    assert same_thread >= (outcomes["interrupted"] + outcomes["after_finish"]) * 0.9


def test_item_hygiene_resets_locals_interrupt_flag_and_context():
    register_item_local_reset(_reset_local)
    register_item_local_reset(_reset_local)
    assert work_item_pool._ITEM_LOCAL_RESETS.count(_reset_local) == 1
    pool = WorkItemPool("m00-hygiene", max_threads=1)
    results: list = []

    def dirty() -> None:
        _LOCAL.value = "dirty"
        _VAR.set("dirty")
        set_interrupt(True)
        results.append((threading.get_ident(), is_interrupted(), _VAR.get()))

    def clean() -> None:
        results.append((threading.get_ident(), is_interrupted(), _VAR.get(), getattr(_LOCAL, "value", None)))

    caller_context = contextvars.copy_context()
    assert pool.submit(dirty, context=caller_context).join(WAIT_SECONDS)
    _wait_parked(pool)
    assert pool.submit(clean, context=contextvars.copy_context()).join(WAIT_SECONDS)
    (ident_a, flagged_a, var_a), (ident_b, flagged_b, var_b, local_b) = results
    assert ident_a == ident_b
    assert (flagged_a, var_a) == (True, "dirty")
    assert (flagged_b, var_b, local_b) == (False, "unset", None)
    assert caller_context[_VAR] == "dirty"
    # 空闲期间被打上的陈旧旗（例如迟到的按 ident 中断）也必须在下一项开始前清掉。
    _wait_parked(pool)
    set_interrupt(True, ident_a)
    after: list = []
    assert pool.submit(lambda: after.append((threading.get_ident(), is_interrupted())),
                       context=contextvars.copy_context()).join(WAIT_SECONDS)
    assert after == [(ident_a, False)]


def test_on_start_runs_first_inside_the_item_context_and_cannot_block_fn(monkeypatch):
    pool = WorkItemPool("m00-start", max_threads=2)
    caller_context = contextvars.copy_context()
    caller_context.run(_VAR.set, "caller")
    order: list = []
    handle = pool.submit(lambda: order.append(("fn", _VAR.get())), context=caller_context,
                         on_start=lambda: order.append(("start", _VAR.get())))
    assert handle.join(WAIT_SECONDS)
    assert order == [("start", "caller"), ("fn", "caller")]
    captured: list = []
    monkeypatch.setattr(threading, "excepthook", captured.append)
    ran: list = []

    def broken_start() -> None:
        raise RuntimeError("on_start failed")

    assert pool.submit(lambda: ran.append(True), context=contextvars.copy_context(),
                       on_start=broken_start).join(WAIT_SECONDS)
    assert ran == [True]
    assert [type(item.exc_value) for item in captured] == [RuntimeError]


def test_item_exception_reaches_threading_excepthook_and_the_thread_survives(monkeypatch):
    captured: list = []
    monkeypatch.setattr(threading, "excepthook", captured.append)
    pool = WorkItemPool("m00-error", max_threads=1)
    idents: list = []

    def boom() -> None:
        idents.append(threading.get_ident())
        raise RuntimeError("boom")

    handle = pool.submit(boom, context=contextvars.copy_context())
    assert handle.join(WAIT_SECONDS) and handle.done()
    assert len(captured) == 1
    assert captured[0].exc_type is RuntimeError and str(captured[0].exc_value) == "boom"
    assert captured[0].thread is not None and captured[0].thread.ident == idents[0]
    _wait_parked(pool)
    assert pool.submit(lambda: idents.append(threading.get_ident()),
                       context=contextvars.copy_context()).join(WAIT_SECONDS)
    assert idents[1] == idents[0]


def test_prepare_start_join_done_and_observation_semantics():
    pool = WorkItemPool("m00-join", max_threads=2)
    gate, entered = threading.Event(), threading.Event()
    idents: list = []
    box: list = []

    def blocked() -> None:
        idents.append((threading.get_ident(), box[0].ident))
        entered.set()
        gate.wait(WAIT_SECONDS)

    handle = pool.prepare(blocked, context=contextvars.copy_context())
    box.append(handle)
    observation = WorkItemObservation(handle)
    # 预备态：没有任何线程能运行它；从未 start 的观察已结清。
    assert not observation.attempted and observation.exited() and observation.wait_until(time.monotonic())
    assert not handle.is_alive() and not handle.done() and not handle.proven_not_started() and handle.ident is None
    assert handle.join(PREPARED_IDLE_SECONDS) is False and not entered.is_set()
    observation.start()
    assert observation.attempted and entered.wait(WAIT_SECONDS)
    assert idents[0][0] == idents[0][1] == handle.ident
    assert handle.is_alive() and not handle.done() and handle.join(SHORT_WAIT_SECONDS) is False
    assert not observation.exited() and observation.wait_until(time.monotonic() + SHORT_WAIT_SECONDS) is False
    assert handle.cancel_if_not_started() is False and not handle.proven_not_started()
    with pytest.raises(RuntimeError):
        handle.start()
    gate.set()
    assert observation.wait_until(time.monotonic() + WAIT_SECONDS) is True
    assert handle.join(WAIT_SECONDS) is True and handle.join() is True
    assert handle.done() and not handle.is_alive() and handle.ident is None and observation.exited()
    # 结束后中断/取消都不生效；池线程复用到下一项也不改变本项的 done()。
    handle.interrupt()
    assert handle.cancel_if_not_started() is False
    _wait_parked(pool)
    probe: list = []
    assert pool.submit(lambda: probe.append((threading.get_ident(), is_interrupted())),
                       context=contextvars.copy_context()).join(WAIT_SECONDS)
    assert probe == [(idents[0][0], False)]
    assert handle.done() and observation.exited() and not handle.proven_not_started()
    # 开始前取消：只有真正转入 CANCELLED 的那次返回 True；之后 start 不交付、join 立即返回。
    ran: list = []
    cancelled = pool.prepare(lambda: ran.append("cancelled"), context=contextvars.copy_context())
    cancelled_observation = WorkItemObservation(cancelled)
    assert cancelled.cancel_if_not_started() is True and cancelled.cancel_if_not_started() is False
    assert cancelled.proven_not_started() and not cancelled.done() and not cancelled.is_alive()
    assert cancelled.join(0) is True
    cancelled_observation.start()
    assert cancelled_observation.attempted and cancelled_observation.proven_not_started()
    assert cancelled_observation.exited() and cancelled_observation.wait_until(time.monotonic())
    interrupted = pool.prepare(lambda: ran.append("interrupted"), context=contextvars.copy_context())
    interrupted.interrupt()
    assert interrupted.proven_not_started()
    interrupted.start()
    _wait_parked(pool)
    assert pool.submit(lambda: ran.append("probe"), context=contextvars.copy_context()).join(WAIT_SECONDS)
    assert ran == ["probe"]


def test_start_that_reaches_no_thread_is_proven_not_started(monkeypatch):
    pool = WorkItemPool("m00-spawn-fail", max_threads=1)
    gate, entered = threading.Event(), threading.Event()

    def occupy() -> None:
        entered.set()
        gate.wait(WAIT_SECONDS)

    busy = pool.submit(occupy, context=contextvars.copy_context())
    assert entered.wait(WAIT_SECONDS)
    ran: list = []
    handle = pool.prepare(lambda: ran.append("refused"), context=contextvars.copy_context())
    observation = WorkItemObservation(handle)

    def refuse(self) -> None:
        raise RuntimeError("can't start new thread")

    # 唯一的池线程忙、池已满：只能走一次性线程，而它起不来。
    monkeypatch.setattr(threading.Thread, "start", refuse)
    try:
        with pytest.raises(RuntimeError, match="can't start new thread"):
            observation.start()
    finally:
        monkeypatch.undo()
    assert handle.proven_not_started() and observation.proven_not_started() and observation.exited()
    assert handle.join(0) is True and not handle.done() and not handle.is_alive()
    gate.set()
    assert busy.join(WAIT_SECONDS)
    _wait_parked(pool)
    assert pool.submit(lambda: ran.append("after"), context=contextvars.copy_context()).join(WAIT_SECONDS)
    assert ran == ["after"]


def test_join_from_inside_the_running_item_raises_instead_of_deadlocking():
    pool = WorkItemPool("m00-self-join", max_threads=1)
    box: dict = {}

    def selfjoin() -> None:
        try:
            box["handle"].join()
        except RuntimeError as exc:
            box["error"] = str(exc)
        box["observed"] = box["observation"].wait_until(time.monotonic() + WAIT_SECONDS)

    box["handle"] = pool.prepare(selfjoin, context=contextvars.copy_context())
    box["observation"] = WorkItemObservation(box["handle"])
    box["observation"].start()
    assert box["handle"].join(WAIT_SECONDS)
    assert "cannot join" in box["error"]
    # 与 _ThreadExitObservation 等待自身线程时一致：不死锁，按“未结清”返回。
    assert box["observed"] is False
    assert box["observation"].exited()
