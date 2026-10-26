# LLM: 隔离OS信号与C计时器，仍执行生产入口的安装、日志和清理；不修改Supervisor用例。
# 模块用途: 验证Gateway生产诊断接线。
from __future__ import annotations

import gc
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts import hang_diagnostics as hd
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.cli import gateway_process as gp


# LLM: 不安装真实信号/C定时器；缺少POSIX注册接口的平台也能建立替身，退出总会清理本模块资源。
# 函数用途: 隔离诊断进程级副作用。
@pytest.fixture
def diagnostic_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(signal, "getsignal", lambda signum: signal.SIG_DFL)
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    monkeypatch.setattr(hd.faulthandler, "enable", lambda **kw: None)
    monkeypatch.setattr(hd.faulthandler, "register", lambda *a, **kw: calls.append(("register", kw)), raising=False)
    monkeypatch.setattr(hd.faulthandler, "unregister", lambda *a: calls.append(("unregister",)), raising=False)
    monkeypatch.setattr(hd.faulthandler, "dump_traceback_later", lambda *a, **kw: calls.append(("arm", kw)))
    monkeypatch.setattr(hd.faulthandler, "cancel_dump_traceback_later", lambda: calls.append(("cancel",)))
    hd.close()
    calls.clear()
    yield calls
    hd.close()


# LLM: 复用原信号入口，Windows只在本用例合成SIGUSR1编号且注册/恢复均为替身，不能替换生产stderr合同。
# 函数用途: 检查生产安装持久诊断，恢复后释放句柄和GC探针。
def test_gateway_signal_lifecycle_installs_persistent_diagnostics(tmp_path, monkeypatch, diagnostic_calls):
    monkeypatch.setattr(signal, "SIGUSR1", getattr(signal, "SIGUSR1", 30), raising=False)
    paths = gateway_paths_from_root(tmp_path)
    previous = gp._install_gateway_signal_handlers(paths, {"pid": os.getpid()})
    try:
        assert hd._handle is not None
        assert hd._gc_probe in gc.callbacks
        assert len([call for call in diagnostic_calls if call[0] == "register"]) == int(hasattr(signal, "SIGUSR1"))
        hd.arm_stall_watchdog(120)
        assert diagnostic_calls[-1][0] == "arm"
    finally:
        gp._restore_gateway_signal_handlers(previous)
    assert hd._handle is None and hd._gc_probe not in gc.callbacks
    assert (tmp_path / hd.GATEWAY_HANG_DUMP_FILENAME).exists()


# LLM: 安装故障只降级诊断，不能使原TERM/INT处理失效。
# 函数用途: 验证生产确实尝试安装且异常不逃逸。
def test_gateway_diagnostic_install_failure_preserves_stop_handlers(tmp_path, monkeypatch, diagnostic_calls):
    attempted = []

    # LLM: 只注入初始化故障，不改变原信号处理函数。
    # 函数用途: 模拟诊断初始化异常。
    def unavailable(*args, **kwargs):
        attempted.append(True)
        raise OSError("diagnostic unavailable")

    monkeypatch.setattr(hd, "install", unavailable)
    previous = gp._install_gateway_signal_handlers(gateway_paths_from_root(tmp_path), {"pid": os.getpid()})
    try:
        assert attempted == [True]
        assert signal.SIGTERM in previous and signal.SIGINT in previous
    finally:
        gp._restore_gateway_signal_handlers(previous)


# LLM: 同根生产重复安装不能取消首tick已挂的C定时器，默认手动重装语义不变。
# 函数用途: 复现dispatcher先安装、原信号入口后安装时看门狗被清掉。
def test_production_reinstall_preserves_live_watchdog(tmp_path, diagnostic_calls):
    assert hd.install(tmp_path, register_signal=False)
    handle = hd._handle
    hd.arm_stall_watchdog(120)
    before = list(diagnostic_calls)
    assert hd.install(tmp_path, register_signal=False)
    assert hd._handle is handle
    assert diagnostic_calls == before


# LLM: 真信号和C导出仅作用于测试新建子进程，不作用于pytest或生产Gateway。
# 函数用途: 验证生产安装后无需信号/内核捕获位也能留下真实线程栈。
def test_real_child_production_capture_without_signal(tmp_path):
    source = '''
import gc, os, sys, threading
from pathlib import Path
from agent_py_agent.agent.gateway_parts import hang_diagnostics as hd
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.cli import gateway_process as gp
paths = gateway_paths_from_root(Path(sys.argv[1]))
previous = gp._install_gateway_signal_handlers(paths, {"pid": os.getpid()})
stop = threading.Event()
worker = threading.Thread(target=stop.wait)
worker.start()
try:
    facts = hd.capture_gateway_hang_facts(paths, os.getpid(), stage="tick_wedged", send_sigusr1=False)
    assert facts["sigusr1_sent"] is False
    text = (paths.root / hd.GATEWAY_HANG_DUMP_FILENAME).read_text(encoding="utf-8")
    assert "tick_wedged" in text and text.count("most recent call first") >= 2, text
finally:
    stop.set()
    worker.join(2)
    gp._restore_gateway_signal_handlers(previous)
assert hd._handle is None and hd._gc_probe not in gc.callbacks
print("production capture and cleanup verified")
'''
    result = subprocess.run([sys.executable, "-c", source, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "production capture and cleanup verified" in result.stdout


# LLM: 直接推进真实fixture生成器，在缺失API时把setup异常转成明确断言；不把macOS自带属性冒充Windows能力。
# 函数用途: 复现faulthandler没有register/unregister时fixture自身无法建立。
def test_diagnostic_fixture_accepts_absent_signal_apis(monkeypatch):
    monkeypatch.delattr(hd.faulthandler, "register", raising=False)
    monkeypatch.delattr(hd.faulthandler, "unregister", raising=False)
    probe = diagnostic_calls.__wrapped__(monkeypatch)
    error = None
    try:
        try:
            next(probe)
        except AttributeError as exc:
            error = exc
        assert error is None, f"fixture must accept absent Windows APIs: {error}"
    finally:
        probe.close()


# LLM: 只删除Windows缺失的接口，不伪造整个os平台；C计时器用fixture隔离，信号默认动作绝不能触发。
# 函数用途: 验证缺失SIGUSR1和注册API时诊断安装、武装、撤销、采集及关闭均可安全降级。
def test_diagnostics_without_posix_signal_apis(tmp_path, monkeypatch, diagnostic_calls):
    monkeypatch.delattr(hd.faulthandler, "register", raising=False)
    monkeypatch.delattr(hd.faulthandler, "unregister", raising=False)
    monkeypatch.delattr(hd.signal, "SIGUSR1", raising=False)
    monkeypatch.setattr(hd, "_PROC_ROOT", tmp_path / "missing-proc")
    assert hd.install(tmp_path) is False
    assert hd._handle is not None and hd._gc_probe in gc.callbacks
    hd.arm_stall_watchdog(120)
    assert diagnostic_calls[-1][0] == "arm"
    hd.disarm_stall_watchdog()
    assert diagnostic_calls[-1][0] == "cancel"
    facts = hd.capture_gateway_hang_facts(tmp_path, os.getpid())
    assert facts["sigusr1_sent"] is False and "write_error" not in facts
    dump = tmp_path / hd.GATEWAY_HANG_DUMP_FILENAME
    assert json.loads(dump.read_text(encoding="utf-8"))["pid"] == os.getpid()
    hd.close()
    assert hd._handle is None and hd._installed_root is None
    assert hd._gc_probe not in gc.callbacks


# LLM: 只替代C计时器出口并明确强制原生分支，晋升线程、Condition和真实单调时钟照常运行。
# 函数用途: 在不向pytest进程导出栈的前提下观察延迟晋升。
@pytest.fixture
def deferred_watchdog(tmp_path, monkeypatch, diagnostic_calls):
    hd.install(tmp_path, register_signal=False)
    calls, armed = diagnostic_calls, threading.Event()
    calls.clear()

    # LLM: 只观察替身C出口的参数和代次，不创建真实C定时器。
    # 函数用途: 记录一次晋升并唤醒测试等待者。
    def record_arm(seconds, **kwargs):
        calls.append(("arm", seconds, kwargs))
        armed.set()

    monkeypatch.setattr(hd.faulthandler, "dump_traceback_later", record_arm)
    monkeypatch.setattr(hd, "_is_native_faulthandler", lambda: True, raising=False)
    monkeypatch.setattr(hd, "_DEFERRED_C_WATCHDOG_PROMOTE_SECONDS", 0.03, raising=False)
    yield calls, armed
    hd.close()


# LLM: 紧密快速tick仅观察C出口零调用，真实线程路径另由独立子进程覆盖。
# 函数用途: 防止快速派发重新创建或取消C定时器。
def test_deferred_fast_ticks_never_create_or_cancel_c_watchdog(deferred_watchdog):
    calls, _armed = deferred_watchdog
    for _ in range(500):
        hd.arm_stall_watchdog(120)
        hd.disarm_stall_watchdog()
    hd.close()
    assert calls == []
    promoter = hd._stall_promoter
    assert promoter is not None and promoter.is_alive() and promoter.daemon


# LLM: 观察实际promoter异步晋升，C替身必须收到剩余期限而非原全长超时。
# 函数用途: 验证慢tick仅晋升一次并正确取消。
def test_deferred_tick_promotes_remaining_deadline_once(deferred_watchdog):
    calls, armed = deferred_watchdog
    hd.arm_stall_watchdog(2)
    assert not armed.wait(0.005)
    assert armed.wait(1)
    assert len(calls) == 1 and calls[0][0] == "arm"
    assert 0 < calls[0][1] <= 1.98
    assert calls[0][2] == {"repeat": False, "file": hd._handle, "exit": False}
    hd.disarm_stall_watchdog()
    hd.disarm_stall_watchdog()
    hd.close()
    assert [call[0] for call in calls] == ["arm", "cancel"]


# LLM: 以事件确认晋升，旧代期限不能覆盖重新arm的当前代。
# 函数用途: 验证重设看门狗丢弃旧任务。
def test_deferred_rearm_cannot_promote_stale_generation(deferred_watchdog):
    calls, armed = deferred_watchdog
    with hd._lifecycle_lock:
        hd.arm_stall_watchdog(2)
        hd.disarm_stall_watchdog()
        hd.arm_stall_watchdog(5)
        assert calls == []
    assert armed.wait(1)
    assert len(calls) == 1 and 4 < calls[0][1] < 5


# LLM: 同锁确认idle并安装探针后撤销，防止把安装前的空闲wait误认成被测唤醒；仍断言下次有限合并窗口。
# 函数用途: 验证通知已消耗也保留合并窗口。
def test_deferred_cancel_before_wakeup_retains_coalescing_window(monkeypatch, deferred_watchdog):
    _calls, _armed = deferred_watchdog
    hd.arm_stall_watchdog(120)
    hd.disarm_stall_watchdog()
    promoter = hd._stall_promoter
    deadline, ready = time.monotonic() + 1, threading.Event()
    waits, reentered = [], threading.Event()
    original = promoter.condition.wait

    # LLM: 记录同一Condition的下一次等待，不替换实际释放锁和阻塞行为。
    # 函数用途: 提供可确定的等待窗口观察点。
    def observe_wait(timeout=None):
        waits.append(timeout)
        reentered.set()
        return original(timeout)

    while True:
        with hd._lifecycle_lock:
            if promoter.idle:
                monkeypatch.setattr(promoter.condition, "wait", observe_wait)
                hd.arm_stall_watchdog(120)
                hd.disarm_stall_watchdog()
                break
        assert time.monotonic() < deadline
        ready.wait(0.001)
    assert reentered.wait(1)
    assert waits[0] == hd._DEFERRED_C_WATCHDOG_PROMOTE_SECONDS


# LLM: 重新安装使用新fd但复用唯一daemon，旧句柄不能被晚到晋升引用。
# 函数用途: 验证关闭和重装的句柄生命周期。
def test_deferred_close_reinstall_reuses_thread_and_new_handle(tmp_path, deferred_watchdog):
    calls, armed = deferred_watchdog
    hd.arm_stall_watchdog(120)
    old, promoter = hd._handle, hd._stall_promoter
    hd.close()
    assert calls == [] and old.closed
    assert hd.install(tmp_path / "new", register_signal=False)
    hd.arm_stall_watchdog(2)
    assert armed.wait(1)
    assert hd._stall_promoter is promoter
    assert calls[0][2]["file"] is hd._handle and hd._handle is not old


# LLM: 取消失败时C仍可能借用fd，必须保留句柄而不是为了关闭成功强行释放。
# 函数用途: 验证取消失败保持借用句柄安全。
def test_deferred_failed_cancel_keeps_c_borrowed_handle(monkeypatch, deferred_watchdog):
    calls, armed = deferred_watchdog
    hd.arm_stall_watchdog(2)
    assert armed.wait(1)
    handle = hd._handle

    # LLM: 精确模拟C取消抛错，不改变其仍持有句柄的前提。
    # 函数用途: 注入取消失败。
    def failed_cancel():
        raise RuntimeError("cannot release C timer")

    monkeypatch.setattr(hd.faulthandler, "cancel_dump_traceback_later", failed_cancel)
    hd.close()
    assert hd._handle is handle and not handle.closed
    monkeypatch.setattr(hd.faulthandler, "cancel_dump_traceback_later", lambda: calls.append(("cancel",)))
    hd.close()
    assert handle.closed and hd._handle is None


# LLM: 子进程保留真实faulthandler身份，用profile观察C调用而非替换出口；只在临时日志导出本子进程线程。
# 函数用途: 真正验证快速tick零C线程调用，以及默认一秒窗口后的C栈导出。
def test_real_child_deferred_native_watchdog(tmp_path):
    source = '''
import faulthandler, json, sys, threading, time
from pathlib import Path
from agent_py_agent.agent.gateway_parts import hang_diagnostics as hd
root = Path(sys.argv[1])
assert hd.install(root, register_signal=False)
native_arm, native_cancel = faulthandler.dump_traceback_later, faulthandler.cancel_dump_traceback_later
calls = []
def profile(frame, event, function):
    if event == "c_call":
        if function is native_arm:
            calls.append("arm")
        elif function is native_cancel:
            calls.append("cancel")
sys.setprofile(profile)
threading.setprofile(profile)
try:
    for _ in range(500):
        hd.arm_stall_watchdog(120)
        hd.disarm_stall_watchdog()
    hd.close()
    assert calls == [], calls
    promoter = hd._stall_promoter
    assert promoter.is_alive() and promoter.daemon
    assert hd.install(root, register_signal=False)
    hd.arm_stall_watchdog(1.1)
    assert hd._stall_promoter is promoter
    dump, deadline = root / hd.GATEWAY_HANG_DUMP_FILENAME, time.monotonic() + 4
    while True:
        text = dump.read_text()
        if "Timeout (" in text and "most recent call first" in text and "in <module>" in text:
            break
        assert time.monotonic() < deadline, text
        time.sleep(0.01)
    hd.close()
    assert calls == ["arm", "cancel"], calls
    assert len([t for t in threading.enumerate() if t.name == "gateway-stall-promoter"]) == 1
    print(json.dumps({"fast_ticks": 500, "native_calls": calls, "promoters": 1}))
finally:
    hd.close()
    sys.setprofile(None)
    threading.setprofile(None)
'''
    result = subprocess.run([sys.executable, "-c", source, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"fast_ticks": 500, "native_calls": ["arm", "cancel"], "promoters": 1}
