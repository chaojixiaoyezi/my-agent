"""前台父进程结束且输出管道关闭后，仍须回收原独立进程组。"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.tooling import process_registry as registry
from agent_py_agent.agent.tooling import shell


# LLM: 只清理由本测试刚启动并记录出生标识的进程，不能按测试目录或命令扫描宿主。
# 函数用途: 即使修复前断言失败也收回测试子进程，避免失败验证污染真实 Gateway。
def _cleanup_test_child(pid: int, birth: str) -> None:
    if birth and registry.capture_process_birth_token(pid) == birth:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX 独立进程组与未回收组长合同")
@pytest.mark.parametrize("exit_code", [0, 7])
@pytest.mark.parametrize("ignore_term", [False, True])
def test_natural_exit_cleans_child_after_outer_pipes_close(tmp_path, exit_code, ignore_term):
    pidfile = tmp_path / "child.pid"
    ready = tmp_path / "child.ready"
    child_program = (
        "import pathlib, signal, time; "
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_term else "")
        + f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)"
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import pathlib, subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_program!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(child.pid))\n"
        f"while not pathlib.Path({str(ready)!r}).exists(): time.sleep(0.01)\n"
        "print('parent-result', flush=True)\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True,
    )
    proc = subprocess.Popen(
        [sys.executable, str(parent)], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    child_pid, child_birth = 0, ""
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        child_pid = int(pidfile.read_text())
        child_birth = registry.capture_process_birth_token(child_pid)
        assert child_birth

        result = shell._communicate_process(proc, command="parent-with-private-child-pipes", timeout=10)

        assert registry._process_instance_terminated(child_pid, child_birth), "父退出和管道 EOF 不证明原组已清理"
        assert result.returncode == exit_code
        assert result.termination.confirmed is True
        assert result.termination.observed_processes >= 2
        assert "parent-result" in result.stdout
        assert unrelated.poll() is None
    finally:
        _cleanup_test_child(child_pid, child_birth)
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=3)
        unrelated.terminate()
        unrelated.wait(timeout=3)


@pytest.mark.parametrize("exit_code", [0, 7])
def test_cleanup_unknown_keeps_original_command_result(tmp_path, monkeypatch, exit_code):
    completed = subprocess.CompletedProcess("generic-command", exit_code, "original-output", "original-error")
    completed.termination = registry.ProcessTerminationReceipt("identity_unavailable", False, exit_code, 0, (123,))
    tool = shell.ShellTool(tmp_path)
    monkeypatch.setattr(tool, "_run_command", lambda *args: completed)

    result = tool.execute({"command": "echo generic-command"})

    assert result.ok is False
    assert result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert result.effect_outcome == "unknown"
    facts = result.result_envelope["process"]
    assert facts["return_code"] == exit_code
    assert facts["command_succeeded"] is (exit_code == 0)
    assert facts["termination"]["confirmed"] is False
    assert "original-output" in result.output and "original-error" in result.output


def test_foreground_exit_freezes_cleanup_before_reaping(monkeypatch):
    proc = Mock(pid=12345, returncode=None)
    proc.poll.return_value = 7
    monkeypatch.setattr(registry, "_IS_WINDOWS", False)
    monkeypatch.setattr(registry, "_process_instance_terminated", lambda *args: True)

    def terminate(pid, handle, *, expected_birth_token):
        proc.poll.assert_not_called()
        assert (pid, handle, expected_birth_token) == (12345, proc, "launch-birth")
        return registry.ProcessTerminationReceipt("SIGTERM", True, 7, 2)

    monkeypatch.setattr(registry, "terminate_process_tree", terminate)
    code, receipt = registry.finish_foreground_process(proc, expected_birth_token="launch-birth")
    assert code == 7 and receipt.confirmed is True


@pytest.mark.parametrize("launch_birth,current_birth", [("", "live-birth"), ("launch-birth", "reused-pid")])
def test_foreground_unknown_identity_never_signals(monkeypatch, launch_birth, current_birth):
    proc = Mock(pid=12345, returncode=None)
    proc.poll.return_value = 0
    monkeypatch.setattr(registry, "_IS_WINDOWS", False)
    monkeypatch.setattr(registry, "_process_instance_terminated", lambda *args: True)
    monkeypatch.setattr(registry, "_process_tree_snapshot", lambda pid: ({pid: current_birth}, True))
    signal_tree = Mock()
    monkeypatch.setattr(registry, "_signal_process_snapshot", signal_tree)

    code, receipt = registry.finish_foreground_process(proc, expected_birth_token=launch_birth)

    assert code == 0 and receipt.confirmed is False
    signal_tree.assert_not_called()


def test_closed_output_is_not_a_process_exit(monkeypatch):
    proc = Mock(pid=12345, returncode=None)
    monkeypatch.setattr(registry, "_IS_WINDOWS", False)
    monkeypatch.setattr(registry, "_process_instance_terminated", lambda *args: False)
    terminate = Mock()
    monkeypatch.setattr(registry, "terminate_process_tree", terminate)

    assert registry.finish_foreground_process(proc, expected_birth_token="launch-birth") == (None, None)
    terminate.assert_not_called()
    proc.poll.assert_not_called()


def test_linux_zombie_observation_keeps_parent_for_cleanup(monkeypatch):
    proc = Mock(pid=12345, returncode=None)
    proc.poll.return_value = 0
    monkeypatch.setattr(registry, "_IS_WINDOWS", False)
    monkeypatch.setattr(registry.Path, "is_dir", lambda self: True)
    monkeypatch.setattr(registry.Path, "read_text", lambda *args, **kwargs: "12345 (worker process) Z 1 12345")
    monkeypatch.setattr(registry.os, "kill", lambda *args: None)
    receipt = registry.ProcessTerminationReceipt("SIGTERM", True, 0, 2)
    terminate = Mock(return_value=receipt)
    monkeypatch.setattr(registry, "terminate_process_tree", terminate)

    assert registry.finish_foreground_process(proc, expected_birth_token="proc:111") == (0, receipt)
    terminate.assert_called_once_with(12345, proc, expected_birth_token="proc:111")


@pytest.mark.skipif(os.name == "nt", reason="POSIX 关闭输出后继续运行的前台进程")
def test_closed_pipes_live_parent_has_bounded_exit_probes(monkeypatch):
    original = shell.finish_foreground_process
    probes = []

    def observe(proc, *, expected_birth_token):
        probes.append(time.monotonic())
        return original(proc, expected_birth_token=expected_birth_token)

    monkeypatch.setattr(shell, "finish_foreground_process", observe)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import os,time; os.close(1); os.close(2); time.sleep(0.4)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        result = shell._communicate_process(proc, command="live-parent-after-output-close", timeout=5)
        assert result.returncode == 0
        assert result.termination.confirmed is True
        assert all(later - earlier >= 0.2 for earlier, later in zip(probes, probes[1:]))
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=3)
