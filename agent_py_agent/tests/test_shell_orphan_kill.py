"""审计 #14 修复真测:同步 run_command 超时杀整个进程组,孙进程不再成孤儿。

真起一个 fork 长 sleep 子进程的命令(把孙进程 PID 写文件),让命令超时,断言孙进程被 killpg 杀掉
(原实现超时只 SIGKILL 直接 shell,孙进程 make/npm/编译器等成孤儿累积耗尽 PID/CPU)。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX session/process-tree semantics")
def test_sync_timeout_kills_descendant_that_created_a_new_session(tmp_path) -> None:
    """A nested bwrap-style session must not keep inherited pipes or the agent alive."""
    tool = ShellTool(tmp_path, options=ShellToolOptions(default_timeout=30))
    pidfile = tmp_path / "escaped-child.pid"
    script = tmp_path / "spawn_escaped_child.py"
    script.write_text(
        "\n".join(
            (
                "import pathlib",
                "import subprocess",
                "import sys",
                "import time",
                f"pidfile = pathlib.Path({str(pidfile)!r})",
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)",
                "pidfile.write_text(str(child.pid), encoding='utf-8')",
                "time.sleep(30)",
            )
        ),
        encoding="utf-8",
    )
    child_pid = 0
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            tool._run_command(f"{sys.executable} {script}", tmp_path, timeout=1)
        assert time.monotonic() - started < 8, "pipe drain remained blocked after timeout"
        child_pid = int(pidfile.read_text(encoding="utf-8").strip())
        deadline = time.monotonic() + 3.0
        while _pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _pid_alive(child_pid), "new-session descendant escaped process-tree cleanup"
    finally:
        if child_pid and _pid_alive(child_pid):
            os.kill(child_pid, 9)


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell quoting")
def test_timeout_keeps_partial_output_and_proven_exit(tmp_path):
    script = "import time,sys; print('中文进度', flush=True); print('stderr-step', file=sys.stderr, flush=True); time.sleep(30)"
    tool = ShellTool(tmp_path, options=ShellToolOptions(default_timeout=30))
    result = tool.execute({"command": f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}", "timeout": 1})
    assert result.ok is False
    assert result.error_code == "TOOL_TIMEOUT"
    assert result.effect_outcome == "failed"
    assert "中文进度" in result.output and "stderr-step" in result.output
    facts = result.result_envelope["process"]
    assert facts["termination"]["confirmed"] is True
    assert facts["termination"]["return_code"] is not None
    assert facts["pipes_drained"] is True


def test_generic_timeout_does_not_claim_termination(tmp_path, monkeypatch):
    tool = ShellTool(tmp_path, options=ShellToolOptions(default_timeout=30))

    def unknown_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("command", 1, output=b"partial-progress")

    monkeypatch.setattr(tool, "_run_command", unknown_timeout)
    result = tool.execute({"command": "echo progress", "timeout": 1})
    assert result.effect_outcome == "unknown"
    assert "partial-progress" in result.output
    assert "termination" not in result.result_envelope["process"]


@pytest.mark.parametrize("complete,still_alive", [(False, False), (True, True)])
def test_termination_cannot_be_confirmed_by_signal_only(monkeypatch, complete, still_alive):
    from unittest.mock import Mock

    from agent_py_agent.agent.tooling import process_registry as module

    monkeypatch.setattr(module, "_IS_WINDOWS", False)
    monkeypatch.setattr(module, "_process_tree_snapshot", lambda pid: ({pid: "birth"}, complete))
    monkeypatch.setattr(module, "_signal_process_snapshot", lambda *args: True)
    monkeypatch.setattr(module, "_wait_process_snapshot_gone", lambda *args: not still_alive)
    monkeypatch.setattr(module, "_process_instance_terminated", lambda *args: not still_alive)
    proc = Mock(pid=12345)
    proc.poll.return_value = None if still_alive else -15
    receipt = module.terminate_process_tree(proc.pid, proc, grace_seconds=0)
    assert receipt.confirmed is False


def test_missing_process_identity_does_not_prove_exit(monkeypatch):
    from agent_py_agent.agent.tooling import process_registry as module

    monkeypatch.setattr(module.os, "kill", lambda *args: None)
    monkeypatch.setattr(module, "capture_process_birth_token", lambda pid: "")
    assert module._process_instance_terminated(99999999, "original-birth") is False


@pytest.mark.parametrize("state,code,expected", [("Z+", 0, True), ("S", 0, False), ("Z", 1, False), ("", 0, False)])
def test_posix_without_proc_uses_kernel_state_without_reaping(monkeypatch, state, code, expected):
    from agent_py_agent.agent.tooling import process_registry as module

    monkeypatch.setattr(module, "_IS_WINDOWS", False)
    monkeypatch.setattr(module.Path, "is_dir", lambda self: False)
    monkeypatch.setattr(
        module.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], code, stdout=state),
    )
    assert module._process_is_zombie(12345) is expected


def test_termination_does_not_resnapshot_reused_root_pid(monkeypatch):
    from unittest.mock import Mock

    from agent_py_agent.agent.tooling import process_registry as module

    snapshot = Mock(return_value=({12345: "old-root", 12346: "original-child"}, True))
    signal_tree = Mock(return_value=True)
    monkeypatch.setattr(module, "_IS_WINDOWS", False)
    monkeypatch.setattr(module, "_process_tree_snapshot", snapshot)
    monkeypatch.setattr(module, "_signal_process_snapshot", signal_tree)
    monkeypatch.setattr(module, "_wait_process_snapshot_gone", lambda *args: False)
    monkeypatch.setattr(module, "_same_process", lambda *args: False)
    monkeypatch.setattr(module, "_process_instance_terminated", lambda *args: True)
    proc = Mock(pid=12345)
    proc.poll.return_value = -15
    receipt = module.terminate_process_tree(proc.pid, proc, grace_seconds=0)
    snapshot.assert_called_once_with(12345)
    assert receipt.confirmed is True
    assert len(signal_tree.call_args_list) == 2
    assert signal_tree.call_args_list[-1].args[0] == {12345: "old-root", 12346: "original-child"}
