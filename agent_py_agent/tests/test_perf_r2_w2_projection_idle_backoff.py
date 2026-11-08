# LLM: PERF-R2-W2 回归：终态投影兜底段在终态目录无增删且页未满（只剩上一拍已试过、仍无法结算的同一批）时，
#   按原 LoopErrorBackoff 倍增空转间隔；目录一变或满页（积压追平中）立即回到 0.75 秒。驱动真实 tick，只替换修复函数与时钟。
# 模块用途: 固定终态投影空转退避与复位合同。
from __future__ import annotations

import threading
from types import SimpleNamespace

from agent_py_agent.cli import gateway_loops
from agent_py_agent.cli.gateway_loop_backoff import LoopErrorBackoff


# LLM: 绕过构造器只为隔离执行池；除终态投影外的维护段到期时间设为无穷，派发段由调用方替换为空。
# 函数用途: 组一个只跑终态投影段的真实 tick 派发者。
def _dispatcher(tmp_path):
    terminal = tmp_path / "requests" / "terminal"
    terminal.mkdir(parents=True)
    dispatcher = object.__new__(gateway_loops._RequestDispatcher)
    dispatcher._future_gate, dispatcher._request_futures = threading.Lock(), {}
    dispatcher._admission_gate = dispatcher._drain_runtime = dispatcher._drain_group = None
    dispatcher.paths = SimpleNamespace(terminal=terminal, root=tmp_path)
    dispatcher.limits = dispatcher._scan_gate = None
    dispatcher._recover_throttle = SimpleNamespace(due=lambda: False)
    dispatcher._closing_deadline_once = lambda: None
    dispatcher._terminal_projection_startup = False
    dispatcher._next_terminal_projection_at = 0.0
    dispatcher._next_input_reconcile_at = dispatcher._next_lock_cleanup_at = float("inf")
    dispatcher.bootstrap_agent = SimpleNamespace(config=SimpleNamespace())
    dispatcher.backoff, dispatcher.tick_failed = LoopErrorBackoff(), False
    for name in ("_recovery_backoff", "_closing_backoff", "_projection_backoff", "_reconcile_backoff", "_lock_cleanup_backoff"):
        setattr(dispatcher, name, LoopErrorBackoff())
    return dispatcher, terminal


# LLM: 假单调钟只替换本模块的 time 引用；修复函数返回调用方给定的(本拍处理数, 错误列表)。
# 函数用途: 准备派发者、终态目录、时钟与修复调用记录。
def _setup(tmp_path, monkeypatch, report):
    dispatcher, terminal = _dispatcher(tmp_path)
    clock, calls = [1000.0], []
    monkeypatch.setattr(gateway_loops, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0]))
    monkeypatch.setattr(gateway_loops, "dispatch_pending_requests", lambda *args, **kwargs: 0)
    monkeypatch.setattr(gateway_loops, "repair_gateway_terminal_projections",
                        lambda *args, **kwargs: calls.append(kwargs) or report[0])
    return dispatcher, terminal, clock, calls


# 函数用途: 以 0.2 秒一拍推进假钟跑真实 tick，返回期间修复函数被调用的次数。
def _run(dispatcher, clock, seconds, calls):
    before = len(calls)
    for _ in range(int(seconds / 0.2)):
        clock[0] += 0.2
        dispatcher.tick()
    return len(calls) - before


def test_unsettleable_page_backs_off_while_terminal_directory_is_unchanged(tmp_path, monkeypatch):
    dispatcher, _terminal, clock, calls = _setup(tmp_path, monkeypatch, [(2, [])])
    _run(dispatcher, clock, 60.0, calls)
    steady = _run(dispatcher, clock, 120.0, calls)
    assert steady <= 6  # 原实现约每 0.75 秒一次（120 秒约 160 次）；退避封顶后约每 30 秒一次


def test_new_terminal_file_or_full_page_restores_fast_cadence(tmp_path, monkeypatch):
    report = [(2, [])]
    dispatcher, terminal, clock, calls = _setup(tmp_path, monkeypatch, report)
    _run(dispatcher, clock, 120.0, calls)
    (terminal / "gwreq-msg-new.json").write_text("{}", encoding="utf-8")
    clock[0] += 31.0  # 越过当前退避窗口，下一拍看到目录变化
    dispatcher.tick()
    assert _run(dispatcher, clock, 3.0, calls) >= 3
    report[0] = (16, [])  # 满页 = 积压追平中，不得退避
    assert _run(dispatcher, clock, 15.0, calls) >= 15
