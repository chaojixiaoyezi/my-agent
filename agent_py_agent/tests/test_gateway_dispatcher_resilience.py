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

from agent_py_agent.agent.gateway_parts import request_worker as rw
from agent_py_agent.agent.gateway_parts.http_handlers import handle_status
from agent_py_agent.agent.gateway_parts.loop_health import GatewayLoopHealth, loop_health
from agent_py_agent.agent.gateway_parts.paths import GatewayPaths
from agent_py_agent.cli import gateway_loops
from agent_py_agent.tests.test_gateway_admission_wait import _paths


@pytest.fixture(autouse=True)
def _fresh_loop_health():
    loop_health.reset()
    yield
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


# 函数用途: 用真实 tick/派发/扫描门、假执行池组一个派发者：submit 只记请求编号并置位 stop_event。
def _stub_dispatcher(paths: GatewayPaths, claims: list[str], stop_event: threading.Event):
    dispatcher = object.__new__(gateway_loops._RequestDispatcher)
    dispatcher.paths = paths
    dispatcher.limits = rw.AdmissionLimits(user_inflight=4, global_inflight=8)
    dispatcher._scan_gate = rw.GatewayInboxScanGate()
    dispatcher._recover_throttle = SimpleNamespace(due=lambda: False)
    dispatcher._next_terminal_projection_at = float("inf")
    dispatcher._next_input_reconcile_at = float("inf")
    dispatcher.bootstrap_agent = SimpleNamespace(config=SimpleNamespace())

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
    assert snapshot["dispatch_tick_errors"] == 1
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
