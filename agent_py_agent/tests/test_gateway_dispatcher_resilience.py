"""Gateway 派发线程的永不停机与可观测合同。

2026-09-28 生产事故：磁盘写满时 tick 里的错误打印本身抛 OSError，逃出 except 块，while 外没有兜底，
派发线程静默退出，前台请求堆在 inbox 一下午没人处理，而 /status 只能看到 pending 在涨。三条硬合同：
1. tick 里任何异常（包括错误打印本身失败）都不能杀掉派发线程，后续请求照常认领；
2. 派发线程真的退出时留下结构化退出事实，心跳与 /status 直接反映 dispatcher_alive=False；
3. 请求文件的 rename 落在 glob 之后、record_scan 之前时，下一轮扫描仍会处理它（扫描门在扫描前取样 mtime）。
撤修复即 FAIL，见各用例里的注释。构造全部是确定性的：注入 ENOSPC、卡住的 tick、glob 之后的 rename。
"""

from __future__ import annotations

import errno
import json
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import hang_diagnostics
from agent_py_agent.agent.gateway_parts import loop_health as health_module
from agent_py_agent.agent.gateway_parts import request_worker as rw
from agent_py_agent.agent.gateway_parts.http_handlers import handle_status
from agent_py_agent.agent.gateway_parts.loop_health import GatewayLoopHealth, loop_health
from agent_py_agent.agent.gateway_parts.paths import GatewayPaths
from agent_py_agent.cli import gateway_loops
from agent_py_agent.cli.gateway_loop_backoff import LoopErrorBackoff
from agent_py_agent.tests.test_gateway_admission_wait import _paths


# LLM: 直接调用派发循环的测试同样会装私有诊断，清理后不能把GC/fd留给其他用例。
# 函数用途: 隔离健康账本与生产诊断资源。
@pytest.fixture(autouse=True)
def _fresh_loop_health():
    loop_health.reset()
    yield
    hang_diagnostics.close()
    loop_health.reset()


@pytest.fixture()
def fresh_admission(monkeypatch):
    admission = rw.GatewayAdmission()
    monkeypatch.setattr(rw, "admission", admission)
    monkeypatch.setattr(gateway_loops, "admission", admission)
    return admission


# 函数用途: 往 inbox 写一份最小请求文件（与两层准入用例同形状）。
def _enqueue(paths: GatewayPaths, request_id: str, user: str) -> None:
    paths.inbox.mkdir(parents=True, exist_ok=True)
    payload = {"id": request_id, "kind": "ask", "goal": "g", "user_id": user, "created_at": time.time()}
    (paths.inbox / f"{request_id}.json").write_text(json.dumps(payload), encoding="utf-8")


# 函数用途: 轮询结构化条件，超时给出明确断言信息；上限只是防挂起。
def _wait_until(predicate, what: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"等待{what}超时"
        time.sleep(0.01)


# 类用途: 前 N 次写入抛 ENOSPC 的 stderr 替身（磁盘写满时 print 的真实失败形态），之后正常记录写入。
class _FailingStderr:
    def __init__(self, failures: int) -> None:
        self.failures_left = failures
        self.writes: list[str] = []

    def write(self, text: str) -> int:
        if self.failures_left > 0:
            self.failures_left -= 1
            raise OSError(errno.ENOSPC, "No space left on device")
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        return None


# LLM: 绕过真实构造器只隔离执行池副作用，仍须与构造器同步扫描和启动状态；退避测试继续调用真实 tick。
# 函数用途: 用真实 tick/派发/扫描门、假执行池组一个派发者：submit 只记请求编号并置位 stop_event。
def _stub_dispatcher(paths: GatewayPaths, claims: list[str], stop_event: threading.Event):
    dispatcher = object.__new__(gateway_loops._RequestDispatcher)
    # 跳过构造器的fixture也保真实本实例Future持有字段，tick不绕过原完成核收。
    dispatcher._future_gate = threading.Lock()
    dispatcher._request_futures = {}
    dispatcher._admission_gate = dispatcher._drain_runtime = dispatcher._drain_group = None
    dispatcher.paths = paths
    dispatcher.limits = rw.AdmissionLimits(user_inflight=4, global_inflight=8)
    dispatcher._scan_gate = rw.GatewayInboxScanGate()
    dispatcher._recover_throttle = SimpleNamespace(due=lambda: False)
    dispatcher._next_terminal_projection_at = float("inf")
    dispatcher._terminal_projection_startup = True
    dispatcher._next_input_reconcile_at = float("inf")
    dispatcher.bootstrap_agent = SimpleNamespace(config=SimpleNamespace())
    dispatcher.backoff = LoopErrorBackoff()
    dispatcher.tick_failed = False
    dispatcher._recovery_backoff = LoopErrorBackoff()
    dispatcher._projection_backoff = LoopErrorBackoff()
    dispatcher._reconcile_backoff = LoopErrorBackoff()
    dispatcher._lock_cleanup_backoff = LoopErrorBackoff()
    dispatcher._next_lock_cleanup_at = float("inf")

    def submit(claim, user_key, conversation_key):
        claims.append(claim.request_id)
        stop_event.set()

    dispatcher._submit = submit
    dispatcher.shutdown = lambda: None
    return dispatcher


def test_dispatch_loop_survives_tick_errors_and_failed_error_printing(tmp_path, monkeypatch, fresh_admission) -> None:
    paths = _paths(tmp_path)
    stop_event = threading.Event()
    claims: list[str] = []
    dispatcher = _stub_dispatcher(paths, claims, stop_event)
    monkeypatch.setattr(gateway_loops, "_RequestDispatcher", lambda context, paths: dispatcher)
    monkeypatch.setattr(gateway_loops, "GATEWAY_REQUEST_POLL_INTERVAL_SECONDS", 0.001)
    stderr = _FailingStderr(failures=1)
    monkeypatch.setattr(sys, "stderr", stderr)
    real_dispatch = gateway_loops.dispatch_pending_requests
    calls = {"dispatch": 0, "due": 0}

    def dispatch(paths_, limits, submit, **kwargs):
        calls["dispatch"] += 1
        if calls["dispatch"] == 1:
            raise RuntimeError("段内错误：tick 自己的 except 接住并打印，打印撞上 ENOSPC")
        if calls["dispatch"] == 3:
            _enqueue(paths_, "req-after-enospc", "user-a")
        return real_dispatch(paths_, limits, submit, **kwargs)

    def due():
        calls["due"] += 1
        if calls["due"] == 2:
            raise RuntimeError("段外错误：直接从 tick 逃出，只有外层守卫能接")
        return False

    monkeypatch.setattr(gateway_loops, "dispatch_pending_requests", dispatch)
    dispatcher._recover_throttle = SimpleNamespace(due=due)

    # 撤修复：OSError(ENOSPC) 从这里炸出（错误打印失败杀死循环），或段外 RuntimeError 炸出（没有外层守卫）。
    gateway_loops._gateway_request_loop(SimpleNamespace(agent=None), paths, stop_event)

    assert claims == ["req-after-enospc"], "打印失败与段外异常之后，后续请求必须照常被认领"
    snapshot = loop_health.snapshot()
    assert snapshot["loop_error_print_failures"] == 1
    assert snapshot["last_loop_error_print_failure"]["type"] == "OSError"
    assert snapshot["last_loop_error_print_failure"]["context"] == "gateway_request_dispatch.iteration"
    assert snapshot["dispatch_tick_errors"] == 2, "段内错误与逃逸错误都记在 dispatcher 名下"
    assert snapshot["loop_error_counts"] == {"dispatcher": 2}
    assert snapshot["last_dispatch_tick_error"]["context"] == "gateway_request_dispatch.tick"
    assert snapshot["dispatch_tick_count"] == 3 and snapshot["last_dispatch_tick_dispatched"] == 1
    assert snapshot["dispatcher_state"] == "exited" and snapshot["dispatcher_exit_error"] == {}
    assert snapshot["dispatcher_alive"] is False and snapshot["last_dispatch_tick_at"] > 0
    assert any("gateway_request_dispatch.tick" in text for text in stderr.writes), "磁盘恢复后打印照常"


def test_print_gateway_loop_error_never_raises(monkeypatch) -> None:
    stderr = _FailingStderr(failures=1)
    monkeypatch.setattr(sys, "stderr", stderr)

    # 撤修复：OSError 从这里炸出。
    gateway_loops._print_gateway_loop_error("gateway_heartbeat.write", "heartbeat", OSError("disk full"))
    gateway_loops._print_gateway_loop_error("gateway_heartbeat.write", "heartbeat", OSError("disk full"))

    snapshot = loop_health.snapshot()
    assert snapshot["loop_error_print_failures"] == 1
    assert snapshot["last_loop_error_print_failure"]["context"] == "gateway_heartbeat.write"
    assert snapshot["last_loop_error_print_failure"]["type"] == "OSError"
    assert len([text for text in stderr.writes if "[gateway-loop-error]" in text]) == 1


# 类用途: 第一次 tick 卡在 release 上（模拟正在跑的 tick），放行后抛 SystemExit（线程被杀）。
class _BlockingThenDyingDispatcher:
    def __init__(self) -> None:
        self.release = threading.Event()

    def tick(self) -> int:
        self.release.wait(timeout=10.0)
        raise SystemExit(3)

    def shutdown(self) -> None:
        return None


# 线程里的 SystemExit 是本用例故意制造的"线程被杀"，pytest 的线程异常钩子会告警，这里明确忽略。
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_dispatcher_death_is_visible_in_heartbeat_and_status(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    dispatcher = _BlockingThenDyingDispatcher()
    monkeypatch.setattr(gateway_loops, "_RequestDispatcher", lambda context, paths: dispatcher)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        gateway_loops, "log_gateway_event", lambda agent, event_type, payload: events.append((event_type, payload))
    )
    agent = SimpleNamespace(subagents=SimpleNamespace(workspace=str(tmp_path / "sub")), config=SimpleNamespace())
    stop_event = threading.Event()
    thread = threading.Thread(
        target=gateway_loops._gateway_request_loop,
        args=(SimpleNamespace(agent=agent), paths, stop_event),
        daemon=True,
    )
    thread.start()
    _wait_until(lambda: loop_health.snapshot()["last_dispatch_tick_started_at"] > 0, "第一次 tick 开始")

    # 活着且正在 tick：心跳载荷带存活与 tick 起止事实
    gateway_loops._write_gateway_heartbeat(paths, agent, status="running", pid=os.getpid())
    heartbeat = json.loads(paths.heartbeat.read_text(encoding="utf-8"))
    assert heartbeat["dispatcher_alive"] is True and heartbeat["dispatcher_state"] == "running"
    assert heartbeat["last_dispatch_tick_started_at"] > 0 and heartbeat["last_dispatch_tick_at"] == 0.0

    dispatcher.release.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    snapshot = loop_health.snapshot()  # 撤修复：没有退出记录，状态仍是 running/alive
    assert snapshot["dispatcher_alive"] is False and snapshot["dispatcher_state"] == "exited"
    assert snapshot["dispatcher_exit_error"]["type"] == "SystemExit"
    assert snapshot["dispatcher_exit_error"]["context"] == "gateway_request_loop.run"
    assert [event_type for event_type, _ in events] == ["gateway_request_loop_exited"]
    assert events[0][1]["stage"] == "gateway_request_loop.run" and events[0][1]["dispatcher_alive"] is False

    # 进程还在（state=running），/status 直接读进程内账本，报出派发已死
    paths.state.write_text(
        json.dumps({"status": "running", "pid": os.getpid(), "started_at": time.time()}), encoding="utf-8"
    )
    sent: list[tuple[int, dict]] = []
    handle_status(SimpleNamespace(_send_json=lambda status, body: sent.append((status, body))), SimpleNamespace(paths=paths))
    assert sent[0][0] == 200 and sent[0][1]["status"] == "running"
    assert sent[0][1]["dispatcher_alive"] is False and sent[0][1]["dispatcher_state"] == "exited"
    assert sent[0][1]["dispatcher_exit_error"]["type"] == "SystemExit"
    assert sent[0][1]["adapter_alive"] is False and sent[0][1]["adapter_pid"] is None  # 没有 adapter.pid → 不算活


def test_dispatcher_shutdown_error_is_recorded_with_shutdown_stage(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    stop_event = threading.Event()
    stop_event.set()  # 不跑 tick，直接走关闭

    class _Dispatcher:
        def tick(self) -> int:
            return 0

        def shutdown(self) -> None:
            raise RuntimeError("executor shutdown failed")

    monkeypatch.setattr(gateway_loops, "_RequestDispatcher", lambda context, paths: _Dispatcher())
    monkeypatch.setattr(gateway_loops, "log_gateway_event", lambda agent, event_type, payload: None)
    monkeypatch.setattr(sys, "stderr", _FailingStderr(failures=0))

    gateway_loops._gateway_request_loop(SimpleNamespace(agent=None), paths, stop_event)  # Exception 按原语义吞掉

    snapshot = loop_health.snapshot()
    assert snapshot["dispatcher_state"] == "exited"
    assert snapshot["dispatcher_exit_error"]["context"] == "gateway_request_pool.shutdown"  # 撤修复：记成 run 阶段


def test_loop_health_reports_vanished_thread_without_exit_record() -> None:
    health = GatewayLoopHealth()
    assert health.snapshot()["dispatcher_state"] == "not_started"
    assert health.snapshot()["dispatcher_alive"] is False

    thread = threading.Thread(target=health.mark_started)
    thread.start()
    thread.join()
    snapshot = health.snapshot()  # 撤修复（alive 只看退出记录）：线程已经没了却报 running/alive
    assert snapshot["dispatcher_alive"] is False and snapshot["dispatcher_state"] == "vanished"

    health.mark_started()
    assert health.snapshot()["dispatcher_alive"] is True and health.snapshot()["dispatcher_state"] == "running"
    health.mark_exited(None)
    assert health.snapshot()["dispatcher_alive"] is False and health.snapshot()["dispatcher_state"] == "exited"


def test_scan_gate_rescans_request_renamed_in_after_glob(tmp_path, monkeypatch, fresh_admission) -> None:
    paths = _paths(tmp_path)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    stale = time.time() - 10
    os.utime(paths.inbox, (stale, stale))  # 扫描前样本一定早于 rename，与文件系统时间粒度无关
    monkeypatch.setattr(rw.GatewayInboxScanGate, "_COARSE_MTIME_GUARD_SECONDS", 0.0)  # 关掉粗粒度保护，只看取样时机
    gate = rw.GatewayInboxScanGate()
    limits = rw.AdmissionLimits(user_inflight=4, global_inflight=8)
    real_iter = rw._iter_pending_requests
    injected = {"done": False}

    def iter_then_rename(paths_):
        entries = real_iter(paths_)  # glob 已经发生
        if not injected["done"]:
            injected["done"] = True
            tmp = paths_.inbox / ".req-late.json.tmp"
            payload = {"id": "req-late", "kind": "ask", "goal": "g", "user_id": "u", "created_at": time.time()}
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, paths_.inbox / "req-late.json")  # rename 落在 glob 之后、record_scan 之前
        return entries

    monkeypatch.setattr(rw, "_iter_pending_requests", iter_then_rename)
    claimed: list[str] = []

    def submit(claim, user_key, conversation_key):
        claimed.append(claim.request_id)

    assert rw.dispatch_pending_requests(paths, limits, submit, scan_gate=gate) == 0  # 这轮 glob 没看到它
    # 撤修复（扫描后取样）：这次 rename 被记成"已见过"，这里是 False，文件永久跳过
    assert gate.should_scan(paths.inbox) is True
    assert rw.dispatch_pending_requests(paths, limits, submit, scan_gate=gate) == 1
    assert claimed == ["req-late"]


# 类用途: 记录每次等待时长的 stop_event 替身：等够 limit 次就置位，不真的睡，让退避序列可精确断言。
class _RecordingStop:
    def __init__(self, limit: int) -> None:
        self.waits: list[float] = []
        self.limit = limit
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def wait(self, seconds: float) -> bool:
        self.waits.append(round(float(seconds), 6))
        if len(self.waits) >= self.limit:
            self._set = True
        return self._set


# 类用途: 按脚本决定每次 tick 是抛错还是空闲返回 0 的派发者/监督者替身。
class _ScriptedTick:
    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.backoff = LoopErrorBackoff()
        self.tick_failed = False

    def tick(self) -> int:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if outcome == "error":
            raise RuntimeError("tick keeps failing")
        return 0


def test_dispatch_loop_backs_off_and_throttles_prints_on_continuous_errors(monkeypatch) -> None:
    stderr = _FailingStderr(failures=0)
    monkeypatch.setattr(sys, "stderr", stderr)
    outcomes = ["error"] * 12 + ["idle", "error", "error"]
    stop = _RecordingStop(limit=len(outcomes))

    gateway_loops._run_request_dispatch(_ScriptedTick(outcomes), stop)

    # 撤修复（无退避）：全是 0.2；成功一次清零，新一段故障从头退避
    assert stop.waits[:12] == [0.2, 0.4, 0.8, 1.6, 3.2, 6.4, 12.8, 25.6, 30.0, 30.0, 30.0, 30.0]
    assert stop.waits[12] == gateway_loops.GATEWAY_REQUEST_POLL_INTERVAL_SECONDS
    assert stop.waits[13:] == [0.2, 0.4]
    printed = [text for text in stderr.writes if "[gateway-loop-error]" in text]
    assert len(printed) == 3, "同种错误只打第 1 次和第 10 次，成功后新一段的第 1 次再打；撤修复：15 次"
    snapshot = loop_health.snapshot()
    assert snapshot["dispatch_tick_errors"] == 14 and snapshot["dispatch_tick_count"] == 15
    assert snapshot["last_dispatch_tick_error"]["type"] == "RuntimeError"


def test_background_main_loop_backs_off_and_throttles_prints(monkeypatch) -> None:
    stderr = _FailingStderr(failures=0)
    monkeypatch.setattr(sys, "stderr", stderr)
    outcomes = ["error"] * 11 + ["idle"]
    stop = _RecordingStop(limit=len(outcomes))

    gateway_loops._run_background_main_ticks(_ScriptedTick(outcomes), stop, 1.0)

    # 出错等 max(轮询间隔 1.0, 退避)：退避永远不比循环自己的节奏更快；撤修复：全是 1.0
    assert stop.waits == [1.0, 1.0, 1.0, 1.6, 3.2, 6.4, 12.8, 25.6, 30.0, 30.0, 30.0, 1.0]
    assert len([text for text in stderr.writes if "[gateway-loop-error]" in text]) == 2  # 第 1 次与第 10 次


def test_loop_error_backoff_delay_and_print_policy() -> None:
    backoff = LoopErrorBackoff(base_seconds=0.2, cap_seconds=30.0, print_every=10)
    assert backoff.delay() == 0.0
    decisions = [backoff.record_failure("k") for _ in range(21)]
    assert [index + 1 for index, decision in enumerate(decisions) if decision] == [1, 10, 20]
    assert backoff.delay() == 30.0 and backoff.consecutive_failures == 21
    assert backoff.record_failure("other") is True  # 另一种错误的第一次也要打
    backoff.record_success()
    assert backoff.delay() == 0.0 and backoff.consecutive_failures == 0
    assert backoff.record_failure("k") is True and backoff.delay() == 0.2


@pytest.mark.parametrize("failures", [1100, 4811, 4913, 50000])
def test_long_error_streak_keeps_backoff_and_can_recover(failures) -> None:
    backoff = LoopErrorBackoff()
    for _ in range(failures):
        backoff.record_failure("persistent_io_error")
    assert backoff.delay() == 30.0
    assert backoff.consecutive_failures == failures
    backoff.record_success()
    assert backoff.delay() == 0.0
    assert backoff.record_failure("persistent_io_error") is True
    assert backoff.delay() == 0.2


def test_disabled_error_delay_stays_zero_after_long_error_streak() -> None:
    backoff = LoopErrorBackoff(base_seconds=0, cap_seconds=30)
    for _ in range(4913):
        backoff.record_failure("persistent_io_error")
    assert backoff.delay() == 0.0


# LLM: 只替换时钟和内核来源，仍执行真实账本、计数、心跳提交与日志落盘。
# 函数用途: 建立可推进时间的派发现场，不等待真实120秒。
def _stall_case(tmp_path, monkeypatch):
    clock = [1000.0]
    clock_module = SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0])
    monkeypatch.setattr(health_module, "time", clock_module)
    monkeypatch.setattr(gateway_loops, "time", clock_module)
    proc = tmp_path / "proc" / str(os.getpid())
    (proc / "task" / "1").mkdir(parents=True)
    (proc / "status").write_text("State:\tS\nThreads:\t1\n", encoding="utf-8")
    (proc / "task" / "1" / "wchan").write_text("futex_wait\n", encoding="utf-8")
    monkeypatch.setattr(hang_diagnostics, "_PROC_ROOT", proc.parent)
    monkeypatch.setattr(hang_diagnostics.faulthandler, "dump_traceback", lambda **kw: kw["file"].write("synthetic stack\n"))
    events = []
    monkeypatch.setattr(gateway_loops, "log_gateway_event", lambda agent, event, data: events.append((event, data)))
    agent = SimpleNamespace(config=SimpleNamespace(), subagents=SimpleNamespace(workspace=str(tmp_path / "sub")))
    loop_health.mark_started()
    return _paths(tmp_path), agent, clock, events


# LLM: 公开起止接口造样；不能只按alive或旧完成时间判死。
# 函数用途: 验证卡住、饥饿、空闲、阈值和未来时间。
@pytest.mark.parametrize("started,finished,pending,reason", [
    (800, 790, 0, "tick_wedged"), (880, 870, 0, "tick_wedged"),
    (881, 870, 2, ""), (100, 110, 1, "pending_starved"),
    (100, 110, 0, ""), (0, 0, 2, ""), (0, 0, 0, ""),
    (990, 995, 3, ""), (1100, 1090, 0, ""),
])
def test_dispatcher_stall_heartbeat_boundaries(tmp_path, monkeypatch, started, finished, pending, reason):
    paths, agent, clock, events = _stall_case(tmp_path, monkeypatch)
    actions = [(started, loop_health.mark_tick_started), (finished, lambda: loop_health.mark_tick_finished(0))]
    for stamp, action in sorted(actions, key=lambda entry: entry[0]):
        if stamp > 0:
            clock[0] = stamp
            action()
    clock[0] = 10000 if (started, finished, pending) == (100, 110, 0) else 1000
    for index in range(pending):
        _enqueue(paths, f"pending-{index}", "u")
    gateway_loops._write_gateway_heartbeat(paths, agent, status="running", pid=os.getpid())
    heartbeat = json.loads(paths.heartbeat.read_text(encoding="utf-8"))
    assert heartbeat.get("dispatcher_stalled") is bool(reason)
    assert heartbeat["dispatcher_state"] == ("stalled" if reason else "running")
    assert heartbeat["dispatcher_alive"] is True
    assert heartbeat.get("dispatcher_stall_reason", "") == reason
    assert len(events) == bool(reason)
    if reason:
        assert events[0][1]["wchan_top"] == [{"wchan": "futex_wait", "count": 1}]
        assert reason in paths.log.read_text(encoding="utf-8")


# LLM: Event保持真实在途，推进时钟而非sleep；恢复后下一次卡死须再次采集。
# 函数用途: 验证阻塞tick每个episode只记录一次。
def test_wedged_dispatcher_captures_once_per_episode(tmp_path, monkeypatch):
    paths, agent, clock, events = _stall_case(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    dispatcher = _ScriptedTick(["idle"])

    # LLM: 仅阻塞测试持有的Event，finally保证释放。
    # 函数用途: 模拟不返回的派发调用。
    def wedged():
        entered.set()
        assert release.wait(5)
        return 0

    dispatcher.tick = wedged
    worker = threading.Thread(target=gateway_loops._guarded_dispatch_tick, args=(dispatcher,))
    worker.start()
    try:
        assert entered.wait(2)
        clock[0] += 120
        for _ in range(2):
            gateway_loops._write_gateway_heartbeat(paths, agent, status="running", pid=os.getpid())
        assert len(events) == 1
        assert json.loads(paths.heartbeat.read_text(encoding="utf-8")).get("dispatcher_stalled") is True
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    gateway_loops._write_gateway_heartbeat(paths, agent, status="running", pid=os.getpid())
    assert json.loads(paths.heartbeat.read_text(encoding="utf-8"))["dispatcher_stalled"] is False
    clock[0] += 1
    loop_health.mark_tick_started()
    clock[0] += 120
    gateway_loops._write_gateway_heartbeat(paths, agent, status="running", pid=os.getpid())
    assert len(events) == 2


# LLM: 只包派发tick，不包worker、idle等待或退避；BaseException也须撤销。
# 函数用途: 守住正常、异常和退出的arm/finally-disarm顺序。
@pytest.mark.parametrize("error", [None, RuntimeError, SystemExit])
def test_dispatch_tick_watchdog_lifetime(monkeypatch, error):
    calls = []
    monkeypatch.setattr(hang_diagnostics, "arm_stall_watchdog", lambda seconds: calls.append(("arm", seconds)))
    monkeypatch.setattr(hang_diagnostics, "disarm_stall_watchdog", lambda: calls.append(("disarm",)), raising=False)
    dispatcher = _ScriptedTick(["idle"])

    # LLM: tick内部观察已武装状态，再按参数模拟结果。
    # 函数用途: 提供受控派发结果。
    def tick():
        calls.append(("tick",))
        if error:
            raise error("test")
        return 0

    dispatcher.tick = tick
    if error is SystemExit:
        with pytest.raises(SystemExit):
            gateway_loops._guarded_dispatch_tick(dispatcher)
    else:
        gateway_loops._guarded_dispatch_tick(dispatcher)
    assert calls == [("arm", 120.0), ("tick",), ("disarm",)]


# LLM: 退出态优先于旧tick；诊断失败不能杀心跳。
# 函数用途: 检查采集失败仍发布stalled，以及退出后不误报。
def test_dispatcher_stall_capture_failure_keeps_heartbeat(tmp_path, monkeypatch):
    paths, agent, clock, _events = _stall_case(tmp_path, monkeypatch)
    loop_health.mark_tick_started()
    clock[0] += 120

    # LLM: 只让诊断依赖失效，不影响原心跳writer。
    # 函数用途: 模拟诊断读取故障。
    def failed_capture(*args, **kwargs):
        raise OSError("diagnostics unavailable")

    monkeypatch.setattr(hang_diagnostics, "capture_gateway_hang_facts", failed_capture)
    gateway_loops._write_gateway_heartbeat(paths, agent, status="running", pid=os.getpid())
    assert json.loads(paths.heartbeat.read_text(encoding="utf-8")).get("dispatcher_stalled") is True
    loop_health.mark_exited(None)
    gateway_loops._write_gateway_heartbeat(paths, agent, status="running", pid=os.getpid())
    heartbeat = json.loads(paths.heartbeat.read_text(encoding="utf-8"))
    assert heartbeat["dispatcher_stalled"] is False and heartbeat["dispatcher_state"] == "exited"


# LLM: 不先安装主线程信号入口，必须由真实派发入口在首tick前准备私有fd。
# 函数用途: 复现启动首拍未布防窗口。
def test_first_dispatch_tick_has_watchdog_before_signal_install(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    stop, armed = threading.Event(), []
    monkeypatch.setattr(hang_diagnostics.faulthandler, "dump_traceback_later", lambda *a, **kw: armed.append(kw["file"]))
    monkeypatch.setattr(hang_diagnostics.faulthandler, "cancel_dump_traceback_later", lambda: None)
    dispatcher = _ScriptedTick(["idle"])

    # LLM: 第一次tick即记录是否在途，随后退出，失败不产生无限循环。
    # 函数用途: 在真正派发循环内观察首拍资源。
    def tick():
        stop.set()
        return 0

    dispatcher.tick = tick
    dispatcher.shutdown = lambda: None
    monkeypatch.setattr(gateway_loops, "_RequestDispatcher", lambda *a: dispatcher)
    gateway_loops._gateway_request_loop(SimpleNamespace(agent=None), paths, stop)
    assert len(armed) == 1 and not armed[0].closed


# LLM: 分离墙钟与单调钟，只让校时发生，不能据展示时间误报或延迟真实deadline。
# 函数用途: 覆盖向前跳、完成后倒拨、在途倒拨三种校时。
@pytest.mark.parametrize("case", ["forward", "completed_rollback", "wedged_rollback"])
def test_dispatcher_stall_uses_monotonic_progress(tmp_path, monkeypatch, case):
    paths, agent, wall, _events = _stall_case(tmp_path, monkeypatch)
    monotonic = [500.0]
    health_module.time.monotonic = lambda: monotonic[0]
    loop_health.mark_tick_started()
    if case == "forward":
        wall[0] += 3600
        monotonic[0] += 1
    elif case == "completed_rollback":
        wall[0] = 500
        monotonic[0] += 1
        loop_health.mark_tick_finished(0)
        wall[0] = 1500
        monotonic[0] += 1
    else:
        wall[0] = 500
        monotonic[0] += 120
    gateway_loops._write_gateway_heartbeat(paths, agent, status="running", pid=os.getpid())
    assert json.loads(paths.heartbeat.read_text(encoding="utf-8"))["dispatcher_stalled"] is (case == "wedged_rollback")


# LLM: 两入口读取同一冻结时钟和真实pending目录；/status只能投影，不可领取诊断episode或写文件。
# 函数用途: 验证卡死、饥饿、空闲、未启动和退出的HTTP/心跳五字段完全同口径。
@pytest.mark.parametrize("case", ["tick_wedged", "pending_starved", "idle", "not_started", "exited"])
def test_status_and_heartbeat_share_dispatcher_stall_projection(tmp_path, monkeypatch, case):
    paths, agent, clock, events = _stall_case(tmp_path, monkeypatch)
    loop_health.mark_tick_started()
    if case in {"pending_starved", "idle"}:
        loop_health.mark_tick_finished(0)
    if case == "pending_starved":
        _enqueue(paths, "starved", "u")
    clock[0] += 121 if case != "idle" else 10800
    if case == "not_started":
        loop_health.reset()
    elif case == "exited":
        loop_health.mark_exited(None)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.state.write_text(json.dumps({"status": "running", "pid": os.getpid(), "started_at": 1000}), encoding="utf-8")
    replies = []
    handler = SimpleNamespace(_send_json=lambda code, body: replies.append((code, body)))
    server = SimpleNamespace(paths=paths)
    handle_status(handler, server)
    handle_status(handler, server)
    assert events == []
    gateway_loops._write_gateway_heartbeat(paths, agent, status="running", pid=os.getpid())
    heartbeat = json.loads(paths.heartbeat.read_text(encoding="utf-8"))
    stalled = case in {"tick_wedged", "pending_starved"}
    expected_state = "stalled" if stalled else case if case in {"not_started", "exited"} else "running"
    keys = ("dispatcher_state", "dispatcher_stalled", "dispatcher_stall_reason",
            "dispatcher_stall_age_s", "dispatcher_stall_timeout_s")
    for code, body in replies:
        assert code == 200
        assert body.get("dispatcher_state") == heartbeat["dispatcher_state"] == expected_state
        assert body.get("dispatcher_stalled") is heartbeat["dispatcher_stalled"] is stalled
        assert {key: body.get(key) for key in keys} == {key: heartbeat[key] for key in keys}
    assert len(events) == int(stalled)


def test_loop_health_error_records_are_redacted() -> None:
    loop_health.note_loop_error("dispatcher", "gateway_request_dispatch.tick", RuntimeError("upstream api_key=sk-live-123456 rejected"))
    record = loop_health.snapshot()["last_dispatch_tick_error"]
    assert "sk-live-123456" not in record["message"] and "<redacted>" in record["message"]
    loop_health.mark_exited(RuntimeError("token=ghp_secret_value"), "gateway_request_pool.shutdown")
    exit_error = loop_health.snapshot()["dispatcher_exit_error"]
    assert exit_error["context"] == "gateway_request_pool.shutdown" and "ghp_secret_value" not in exit_error["message"]
