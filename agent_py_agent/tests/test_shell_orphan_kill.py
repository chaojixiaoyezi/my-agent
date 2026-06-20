"""审计 #14 修复真测:同步 run_command 超时杀整个进程组,孙进程不再成孤儿。

真起一个 fork 长 sleep 子进程的命令(把孙进程 PID 写文件),让命令超时,断言孙进程被 killpg 杀掉
(原实现超时只 SIGKILL 直接 shell,孙进程 make/npm/编译器等成孤儿累积耗尽 PID/CPU)。
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(os.name == "nt", reason="POSIX 进程组语义(用户:不管 Windows)")
def test_sync_timeout_kills_process_group_no_orphan(tmp_path) -> None:
    tool = ShellTool(tmp_path, options=ShellToolOptions(default_timeout=30))
    pidfile = tmp_path / "child.pid"
    # 后台 fork 一个长 sleep(写下其 PID),前台再 sleep 让命令超时
    cmd = f"sleep 30 & echo $! > {pidfile}; sleep 30"
    with pytest.raises(subprocess.TimeoutExpired):
        tool._run_command(cmd, tmp_path, timeout=1)
    child_pid = int(pidfile.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 3.0
    while _pid_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _pid_alive(child_pid), "孙进程成了孤儿(killpg 没杀掉整组)"


def test_normal_command_still_returns_output(tmp_path) -> None:
    # Popen 改写后,正常命令仍返回正确 CompletedProcess(stdout/returncode)
    tool = ShellTool(tmp_path, options=ShellToolOptions(default_timeout=30))
    result = tool._run_command("echo hello-orphan-test", tmp_path, timeout=10)
    assert result.returncode == 0
    assert "hello-orphan-test" in result.stdout


def test_nonzero_exit_preserved(tmp_path) -> None:
    tool = ShellTool(tmp_path, options=ShellToolOptions(default_timeout=30))
    result = tool._run_command("exit 7", tmp_path, timeout=10)
    assert result.returncode == 7  # 退出码原样透传
