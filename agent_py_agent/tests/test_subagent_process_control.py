"""子代理停止的进程边界：新会话后代不能逃逸，独立宿主不能被误杀。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.subagents import process_control


# LLM: 只启动测试拥有的进程；等待结构化 PID 文件确认新会话命令已开始，超时即失败。
# 函数用途: 在受控进程测试中等到写入者就绪，不依赖固定睡眠猜启动时机。
def _wait_for_pid_file(path: Path, process: subprocess.Popen) -> int:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists() and path.read_text():
            return int(path.read_text())
        assert process.poll() is None, "测试宿主在写入就绪记录前退出"
        time.sleep(0.02)
    pytest.fail("测试命令未就绪")


@pytest.mark.skipif(os.name != "posix", reason="验证 POSIX 独立 session 的终止边界")
@pytest.mark.parametrize("ignore_term", [False, True])
def test_worker_stop_terminates_detached_writer_and_preserves_other_host(
    tmp_path: Path, ignore_term: bool,
) -> None:
    pid_file = tmp_path / "writer.pid"
    output = tmp_path / "samples.txt"
    child_script = "\n".join([
        "import os, pathlib, signal, time",
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)" if ignore_term else "",
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))",
        f"with pathlib.Path({str(output)!r}).open('a') as handle:",
        "    while True:",
        "        handle.write('sample\\n'); handle.flush(); time.sleep(0.02)",
    ])
    parent_script = "\n".join([
        "import subprocess, sys",
        f"child = subprocess.Popen([sys.executable, '-c', {child_script!r}], start_new_session=True)",
        "child.wait()",
    ])
    parent = subprocess.Popen([sys.executable, "-c", parent_script], start_new_session=True)
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
    )
    child_pid = 0
    try:
        child_pid = _wait_for_pid_file(pid_file, parent)
        report = process_control.terminate_pid_with_escalation(parent.pid, grace_seconds=0.2)
        assert not process_control.is_pid_alive(child_pid), "命令另开 session 后逃逸，取消未停止写入"
        parent.wait(timeout=3)
        before = output.read_bytes()
        time.sleep(0.12)
        assert output.read_bytes() == before
        assert unrelated.poll() is None
        assert report["status"] == ("killed" if ignore_term else "terminated"), report
        assert report["termination"]["confirmed"] is True
        assert report["termination"]["observed_processes"] >= 2
        assert report["escalated"] is ignore_term
    finally:
        for proc in (parent, unrelated):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=3)
        if child_pid and process_control.is_pid_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)


@pytest.mark.parametrize("method", ["SIGTERM", "SIGTERM->SIGKILL", "taskkill/T/F"])
def test_unconfirmed_tree_receipt_never_claims_terminated(monkeypatch, method):
    from agent_py_agent.agent.tooling import process_registry

    monkeypatch.setattr(process_control, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        process_registry, "terminate_process_tree",
        lambda *args, **kwargs: process_registry.ProcessTerminationReceipt(
            method, False, None, 2, (12346,),
        ),
    )
    report = process_control.terminate_pid_with_escalation(12345)
    assert report["status"] == "kill_sent"
    assert report["exited"] is False
    assert report["termination"]["unresolved_pids"] == (12346,)


@pytest.mark.parametrize("pid,alive,status", [(0, True, "no_pid"), (12345, False, "not_alive")])
def test_absent_worker_does_not_send_signals(monkeypatch, pid, alive, status):
    monkeypatch.setattr(process_control, "is_pid_alive", lambda pid: alive)
    assert process_control.terminate_pid_with_escalation(pid)["status"] == status
