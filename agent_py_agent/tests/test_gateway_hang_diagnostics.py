"""卡顿诊断：单测模拟内核只读事实；真实信号和C看门狗仅作用于临时子进程。"""
from __future__ import annotations

import gc
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import hang_diagnostics as hd
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root


# LLM: pytest主进程不能安装真实信号或C定时器；真实导出在独立子进程中验证，替身仅记录生命周期调用。
# 函数用途: 隔离模块状态并捕获faulthandler参数，测试结束恢复原回调。
@pytest.fixture(autouse=True)
def isolated_diagnostics(monkeypatch):
    calls = []
    monkeypatch.setattr(hd.faulthandler, "register", lambda *a, **kw: calls.append(("register", a, kw)), raising=False)
    monkeypatch.setattr(hd.faulthandler, "unregister", lambda *a: calls.append(("unregister", a, {})), raising=False)
    monkeypatch.setattr(hd.faulthandler, "dump_traceback_later", lambda *a, **kw: calls.append(("arm", a, kw)))
    monkeypatch.setattr(hd.faulthandler, "cancel_dump_traceback_later", lambda: calls.append(("cancel", (), {})))
    hd.close()
    calls.clear()
    yield calls
    hd.close()


# LLM: 注册、定时器参数及退出顺序必须来自公开入口；不把调用成功等同真实栈导出。
# 函数用途: 钉住安装、重置及幂等关闭的文件和回调生命周期。
@pytest.mark.skipif(os.name != "posix" or not hasattr(signal, "SIGUSR1"), reason="需要POSIX SIGUSR1")
def test_install_arm_close_lifecycle(tmp_path, isolated_diagnostics):
    assert hd.install(tmp_path) is True
    handle = hd._handle
    assert handle is not None and not handle.closed
    assert gc.callbacks.count(hd._gc_probe) == 1
    register = next(call for call in isolated_diagnostics if call[0] == "register")
    assert register[1] == (signal.SIGUSR1,)
    assert register[2] == {"file": handle, "all_threads": True, "chain": False}
    hd.arm_stall_watchdog(2.5)
    assert isolated_diagnostics[-1] == ("arm", (2.5,), {"repeat": False, "file": handle, "exit": False})
    hd.close()
    assert handle.closed and hd._handle is None
    assert hd._gc_probe not in gc.callbacks
    assert [call[0] for call in isolated_diagnostics][-2:] == ["cancel", "unregister"]
    hd.close()


# LLM: 重装必须撤销旧C引用再关闭旧fd；同文件追加保留原记录，不重复挂GC回调。
# 函数用途: 防止重复初始化泄漏文件描述符和回调。
@pytest.mark.skipif(os.name != "posix" or not hasattr(signal, "SIGUSR1"), reason="需要POSIX SIGUSR1")
def test_reinstall_closes_old_handle_and_preserves_append(tmp_path):
    dump = tmp_path / hd.GATEWAY_HANG_DUMP_FILENAME
    dump.write_text("previous\n", encoding="utf-8")
    assert hd.install(tmp_path)
    old = hd._handle
    assert hd.install(tmp_path)
    assert old.closed and hd._handle is not old
    assert gc.callbacks.count(hd._gc_probe) == 1
    assert dump.read_text(encoding="utf-8") == "previous\n"
    assert dump.stat().st_mode & 0o777 == 0o600


# LLM: 打开失败不能安装半套回调，且未安装时arm必须无副作用。
# 函数用途: 验证诊断不可写时降级，不阻断业务或触发定时器。
def test_install_open_failure_leaves_no_state(tmp_path, isolated_diagnostics):
    bad_root = tmp_path / "file"
    bad_root.write_text("not a directory")
    assert hd.install(bad_root) is False
    assert hd._handle is None and hd._gc_probe not in gc.callbacks
    hd.arm_stall_watchdog(1)
    assert isolated_diagnostics == []


# LLM: 不支持信号时仍保留GC探针；返回False只表示没有启用信号dump，不是整体安装失败。
# 函数用途: 守住非信号平台的降级与关闭清理。
def test_missing_sigusr1_keeps_gc_probe(tmp_path, monkeypatch):
    monkeypatch.delattr(hd.signal, "SIGUSR1", raising=False)
    assert hd.install(tmp_path) is False
    assert hd._handle is not None and hd._gc_probe in gc.callbacks
    handle = hd._handle
    hd.close()
    assert handle.closed and hd._gc_probe not in gc.callbacks


# LLM: 注册故障不撤销已可用GC记录；注销只针对确实注册成功的信号。
# 函数用途: 验证信号注册异常后的部分功能和资源收尾。
def test_registration_failure_keeps_gc_and_does_not_unregister(tmp_path, monkeypatch, isolated_diagnostics):
    # LLM: 仅故障注入本模块信号注册，不改变GC或真实进程信号。
    # 函数用途: 模拟faulthandler不支持注册的环境。
    def failed_register(*args, **kwargs):
        raise RuntimeError("registration unavailable")

    monkeypatch.setattr(hd.faulthandler, "register", failed_register)
    assert hd.install(tmp_path) is False
    assert hd._handle is not None and hd._gc_probe in gc.callbacks
    hd.close()
    assert not any(call[0] == "unregister" for call in isolated_diagnostics)


# LLM: 起始单调时钟可以为0，stop必须有相应gen2 start；写后不关闭即可读到flush结果。
# 函数用途: 验证慢GC写结构化记录且不会把未配对stop误当长暂停。
def test_slow_gen2_gc_flushes_json(tmp_path, monkeypatch):
    hd.install(tmp_path)
    assert hd._handle is not None
    ticks = iter([0.0, 1.25])
    monkeypatch.setattr(hd, "time", SimpleNamespace(monotonic=lambda: next(ticks), time=time.time))
    hd._gc_probe("stop", {"generation": 2})
    hd._gc_probe("start", {"generation": 2})
    hd._gc_probe("stop", {"generation": 2, "collected": 17})
    rows = [json.loads(line) for line in (tmp_path / hd.GATEWAY_HANG_DUMP_FILENAME).read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0] == {"event": "gc_gen2_pause", "at": rows[0]["at"], "seconds": 1.25, "collected": 17}
    assert rows[0]["at"] > 0


# LLM: 只记录达到阈值的gen2周期，其他generation与短暂停不能混入卡顿证据。
# 函数用途: 覆盖阈值包含边界及非目标回收。
@pytest.mark.parametrize("generation,elapsed,expected", [(2, 1.0, 1), (2, 0.99, 0), (1, 5.0, 0)])
def test_gc_threshold_and_generation(tmp_path, monkeypatch, generation, elapsed, expected):
    hd.install(tmp_path)
    assert hd._handle is not None
    ticks = iter([10.0, 10.0 + elapsed])
    monkeypatch.setattr(hd, "time", SimpleNamespace(monotonic=lambda: next(ticks), time=time.time))
    hd._gc_probe("start", {"generation": generation})
    hd._gc_probe("stop", {"generation": generation, "collected": 0})
    assert len((tmp_path / hd.GATEWAY_HANG_DUMP_FILENAME).read_text().splitlines()) == expected


# LLM: 已启动的进程级watchdog在配置0时必须取消，非有限值不能传给C层定时器。
# 函数用途: 防止关闭/非法阈值仍留下旧定时导出。
@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf")])
def test_disabled_watchdog_cancels_previous_arm(tmp_path, isolated_diagnostics, seconds):
    hd.install(tmp_path)
    assert hd._handle is not None
    hd.arm_stall_watchdog(5)
    hd.arm_stall_watchdog(seconds)
    assert [call[0] for call in isolated_diagnostics].count("arm") == 1
    assert isolated_diagnostics[-1][0] == "cancel"


# LLM: 仅建立合成/proc，不读取真实进程正文；状态白名单与等待点频次使用明确可重算样本。
# 函数用途: 构造可跨平台复现的Linux状态、线程wchan及pressure输入。
def _fake_proc(tmp_path, monkeypatch, mask=None):
    proc = tmp_path / "proc"
    target = proc / "123456"
    target.mkdir(parents=True)
    fields = {"State": "S (sleeping)", "VmRSS": "100 kB", "RssAnon": "40 kB", "RssFile": "60 kB",
              "VmSwap": "0 kB", "Threads": "17"}
    if mask is not None:
        fields["SigCgt"] = mask
    (target / "status").write_text("Name:\tSECRET_PROCESS_NAME\n" + "\n".join(f"{key}:\t{value}" for key, value in fields.items()))
    for number, wchan in enumerate(["futex_wait"] * 5 + [f"wait_{i}" for i in range(12)], start=1):
        folder = target / "task" / str(number)
        folder.mkdir(parents=True)
        (folder / "wchan").write_text(wchan + "\n")
    (proc / "pressure").mkdir()
    for kind in ("memory", "io"):
        (proc / "pressure" / kind).write_text("some avg10=0.01 total=1\nfull ignored\n")
    monkeypatch.setattr(hd, "_PROC_ROOT", proc)
    return fields


# LLM: 信号只能在JSON落盘后请求；模拟内核捕获位，不把此单测冒充真实Linux采集。
# 函数用途: 验证两种路径载体、字段白名单、前10等待点及发送顺序。
@pytest.mark.skipif(os.name != "posix" or not hasattr(signal, "SIGUSR1"), reason="需要POSIX信号编号")
@pytest.mark.parametrize("as_paths", [False, True])
def test_capture_proc_facts_before_signal(tmp_path, monkeypatch, as_paths):
    fields = _fake_proc(tmp_path, monkeypatch, format(1 << (int(signal.SIGUSR1) - 1), "x"))
    root = tmp_path / "gateway"
    observed = []

    # LLM: 模拟信号出口前读取真实输出文件，避免仅用调用次数证明顺序。
    # 函数用途: 捕获信号参数及已落盘的事件类型。
    def signal_after_snapshot(pid, sig):
        rows = (root / hd.GATEWAY_HANG_DUMP_FILENAME).read_text().splitlines()
        observed.append((pid, sig, json.loads(rows[-1])["event"]))

    monkeypatch.setattr(hd.os, "kill", signal_after_snapshot)
    facts = hd.capture_gateway_hang_facts(gateway_paths_from_root(root) if as_paths else root, 123456)
    assert facts
    assert facts["status"] == fields
    assert len(facts["wchan_top"]) == 10
    assert facts["wchan_top"][0] == {"wchan": "futex_wait", "count": 5}
    assert facts["pressure"] == {"memory": "some avg10=0.01 total=1", "io": "some avg10=0.01 total=1"}
    assert facts["sigusr1_sent"] is True
    assert observed == [(123456, signal.SIGUSR1, "supervisor_hang_capture")]
    row = json.loads((root / hd.GATEWAY_HANG_DUMP_FILENAME).read_text())
    assert (row["event"], row["pid"], row["stage"]) == ("supervisor_hang_capture", 123456, "before_term")
    assert "SECRET_PROCESS_NAME" not in json.dumps(row)


# LLM: 缺失、非法或未捕获位都不能向旧版进程发送默认会终止进程的SIGUSR1。
# 函数用途: 覆盖安全发送门及显式关闭信号请求。
@pytest.mark.parametrize("mask,send", [(None, True), ("0", True), ("bad-mask", True), ("-1", True), ("ffffffff", False)])
def test_capture_never_signals_without_caught_bit(tmp_path, monkeypatch, mask, send):
    _fake_proc(tmp_path, monkeypatch, mask)
    monkeypatch.setattr(hd.os, "kill", lambda *args: pytest.fail("unsafe signal"))
    facts = hd.capture_gateway_hang_facts(tmp_path / "gateway", 123456, stage="after_term", send_sigusr1=send)
    assert facts
    assert facts["sigusr1_sent"] is False
    row = json.loads((tmp_path / "gateway" / hd.GATEWAY_HANG_DUMP_FILENAME).read_text())
    assert row["stage"] == "after_term"


# LLM: 无/proc时只写降级结构化快照，不能根据PID存在或平台名猜已注册信号。
# 函数用途: 验证非Linux/进程退出竞态下仍可安全采集。
def test_missing_proc_writes_minimal_capture_without_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(hd, "_PROC_ROOT", tmp_path / "missing-proc")
    monkeypatch.setattr(hd.os, "kill", lambda *args: pytest.fail("unknown signal handler"))
    facts = hd.capture_gateway_hang_facts(tmp_path, os.getpid())
    assert facts
    assert facts["status"] == {} and facts["wchan_top"] == [] and facts["pressure"] == {}
    assert facts["sigusr1_sent"] is False
    assert json.loads((tmp_path / hd.GATEWAY_HANG_DUMP_FILENAME).read_text())["event"] == "supervisor_hang_capture"


# LLM: 禁止0、负数与bool PID进入os.kill，避免把单进程诊断扩大成进程组信号。
# 函数用途: 固定危险PID输入的拒绝边界。
@pytest.mark.parametrize("pid", [0, -1, True, "123"])
def test_capture_rejects_invalid_pid(tmp_path, pid):
    with pytest.raises(ValueError):
        hd.capture_gateway_hang_facts(tmp_path, pid)


# LLM: 采集文件不可写时不得先发信号；诊断故障只返回错误类别，不改变被监护进程。
# 函数用途: 验证写入失败软降级且不留下未落盘信号请求。
def test_capture_write_failure_does_not_signal(tmp_path, monkeypatch):
    _fake_proc(tmp_path, monkeypatch, "ffffffff")
    bad_root = tmp_path / "not-a-directory"
    bad_root.write_text("file")
    monkeypatch.setattr(hd.os, "kill", lambda *args: pytest.fail("signal before evidence"))
    facts = hd.capture_gateway_hang_facts(bad_root, 123456)
    assert facts
    assert facts["write_error"] and facts["sigusr1_sent"] is False


# LLM: 读取内核捕获位后进程仍可能退出；发送失败不能丢失已经落盘的现场。
# 函数用途: 覆盖采集与发信号之间的进程退出竞态。
@pytest.mark.skipif(os.name != "posix" or not hasattr(signal, "SIGUSR1"), reason="非POSIX不进入发信号分支")
def test_capture_signal_failure_retains_snapshot(tmp_path, monkeypatch):
    _fake_proc(tmp_path, monkeypatch, "ffffffff")

    # LLM: 内核信息读取成功后模拟目标退出，不向真实PID发信号。
    # 函数用途: 为信号竞态提供明确ProcessLookupError。
    def disappeared(*args):
        raise ProcessLookupError("gone")

    monkeypatch.setattr(hd.os, "kill", disappeared)
    facts = hd.capture_gateway_hang_facts(tmp_path, 123456)
    assert facts
    assert facts["sigusr1_sent"] is False and facts["sigusr1_error"] == "ProcessLookupError"
    assert json.loads((tmp_path / hd.GATEWAY_HANG_DUMP_FILENAME).read_text())["pid"] == 123456


# LLM: GC回调可能与退出关闭交错；不能把关闭文件写异常传播给GC或业务线程。
# 函数用途: 验证探针在文件已经关闭时安全降级。
def test_gc_probe_tolerates_closed_handle(tmp_path, monkeypatch):
    hd.install(tmp_path)
    assert hd._handle is not None
    hd._handle.close()
    ticks = iter([1.0, 3.0])
    monkeypatch.setattr(hd, "time", SimpleNamespace(monotonic=lambda: next(ticks), time=time.time))
    hd._gc_probe("start", {"generation": 2})
    hd._gc_probe("stop", {"generation": 2, "collected": 1})


# LLM: 仅对测试自身持有的新子进程注册/发送信号，Windows不执行POSIX用例；无/proc平台明确使用合成捕获位。
# 函数用途: 真正验证SIGUSR1导出和C层一次性watchdog，不拿mock回执冒充线程栈。
@pytest.mark.skipif(os.name != "posix" or not hasattr(signal, "SIGUSR1"), reason="需要POSIX SIGUSR1")
@pytest.mark.parametrize("mode", ["signal", "watchdog"])
def test_real_child_dumps_all_threads(tmp_path, mode):
    script = textwrap.dedent('''
        import json, os, signal, sys, threading, time
        from pathlib import Path
        from agent_py_agent.agent.gateway_parts import hang_diagnostics as hd
        root, mode = Path(sys.argv[1]), sys.argv[2]
        assert hd.install(root)
        stop = threading.Event()
        worker = threading.Thread(target=lambda: stop.wait(), daemon=True)
        worker.start()
        source = "kernel"
        try:
            if mode == "signal":
                if not Path(f"/proc/{os.getpid()}/status").exists():
                    source = "fixture"
                    hd._PROC_ROOT = root / "proc-fixture"
                    proc = hd._PROC_ROOT / str(os.getpid())
                    proc.mkdir(parents=True)
                    mask = 1 << (int(signal.SIGUSR1) - 1)
                    (proc / "status").write_text(f"SigCgt:\\t{mask:x}\\n")
                facts = hd.capture_gateway_hang_facts(root, os.getpid())
                assert facts["sigusr1_sent"]
            else:
                hd.arm_stall_watchdog(0.05)
            deadline = time.monotonic() + 3
            dump = root / hd.GATEWAY_HANG_DUMP_FILENAME
            text = dump.read_text()
            # 全线程栈由C层分段追加，等两个线程头及主线程末帧都出现再做原断言。
            while text.count("most recent call first") < 2 or "in <module>" not in text:
                assert time.monotonic() < deadline, "no C-level stack dump"
                time.sleep(0.01)
                text = dump.read_text()
            assert text.count("most recent call first") >= 2, text
            if mode == "signal":
                assert json.loads(text.splitlines()[0])["event"] == "supervisor_hang_capture"
            else:
                assert "Timeout (" in text
            print(json.dumps({"mode": mode, "proc_source": source}))
        finally:
            hd.close()
            stop.set()
            worker.join(1)
    ''')
    completed = subprocess.run([sys.executable, "-c", script, str(tmp_path), mode],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["mode"] == mode


# LLM: 超大但有限的正数也可能被C计时器拒绝；失败后必须取消先前已武装的deadline。
# 函数用途: 复现OverflowError传播并守住旧watchdog的收尾策略。
def test_watchdog_overflow_cancels_old_deadline(tmp_path, monkeypatch, isolated_diagnostics):
    hd.install(tmp_path)
    hd.arm_stall_watchdog(60)

    # LLM: 只替换计时器入口，不触发真实进程级倒计时。
    # 函数用途: 模拟底层时间范围溢出。
    def overflow(*args, **kwargs):
        raise OverflowError("timeout out of range")

    monkeypatch.setattr(hd.faulthandler, "dump_traceback_later", overflow)
    hd.arm_stall_watchdog(1e20)
    assert isolated_diagnostics[-1][0] == "cancel"


# LLM: FIFO只建在测试临时目录，子进程由subprocess.run超时回收；不允许诊断入口自身无限阻塞。
# 函数用途: 在真实OS下复现FIFO打开阻塞和C层有限大数溢出，不用mock冒充修复。
@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "mkfifo") or not hasattr(signal, "SIGUSR1"), reason="需要POSIX FIFO及SIGUSR1")
@pytest.mark.parametrize("case", ["fifo_install", "fifo_capture", "watchdog_overflow"])
def test_real_child_failure_paths_return_without_hang(tmp_path, case):
    if case.startswith("fifo"):
        os.mkfifo(tmp_path / hd.GATEWAY_HANG_DUMP_FILENAME)
    script = textwrap.dedent('''
        import json, os, sys
        from pathlib import Path
        from agent_py_agent.agent.gateway_parts import hang_diagnostics as hd
        root, case = Path(sys.argv[1]), sys.argv[2]
        try:
            if case == "fifo_install":
                assert hd.install(root) is False
            elif case == "fifo_capture":
                result = hd.capture_gateway_hang_facts(root, os.getpid())
                assert result["write_error"] and result["sigusr1_sent"] is False
            else:
                assert hd.install(root)
                hd.arm_stall_watchdog(60)
                hd.arm_stall_watchdog(1e20)
            print(json.dumps({"finished": case}))
        finally:
            hd.close()
    ''')
    try:
        completed = subprocess.run([sys.executable, "-c", script, str(tmp_path), case],
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=4)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{case}: diagnostic entry blocked for four seconds")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout) == {"finished": case}
