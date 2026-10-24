# LLM: 隔离OS信号与C计时器，仍执行生产入口的安装、日志和清理；不修改Supervisor用例。
# 模块用途: 验证Gateway生产诊断接线。
from __future__ import annotations

import gc
import json
import os
import signal
import subprocess
import sys
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
