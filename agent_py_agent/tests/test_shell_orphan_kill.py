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


@pytest.mark.skipif(os.name == "nt", reason="POSIX orphaned process-group semantics")
@pytest.mark.parametrize("ignore_term", [False, True])
def test_foreground_timeout_cleans_group_after_leader_exits(tmp_path, ignore_term):
    from agent_py_agent.agent.tooling.shell import CommandTimeoutError, _communicate_process

    pidfile = tmp_path / "orphan.pid"
    script = (
        ("trap '' TERM; " if ignore_term else "")
        + f"sleep 30 & echo $! > {shlex.quote(str(pidfile))}"
    )
    proc = subprocess.Popen(
        ["bash", "-c", script], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    child_pid = 0
    try:
        with pytest.raises(CommandTimeoutError) as caught:
            _communicate_process(proc, command=script, timeout=1)
        child_pid = int(pidfile.read_text())
        deadline = time.monotonic() + 2
        while _pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _pid_alive(child_pid), "已退出的组长不能让仍持有管道的后台后代逃过超时清理"
        assert caught.value.pipes_drained is True
        assert caught.value.termination.confirmed is True
        assert caught.value.termination.observed_processes >= 2
        assert unrelated.poll() is None
    finally:
        if not child_pid and pidfile.exists():
            child_pid = int(pidfile.read_text())
        if child_pid and _pid_alive(child_pid):
            os.kill(child_pid, 9)
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=3)
        unrelated.terminate()
        unrelated.wait(timeout=3)


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


@pytest.mark.parametrize(
    "root_group, same_identity, complete",
    [(12345, False, False), (90000, True, True), (None, True, False)],
)
def test_group_expansion_requires_same_present_independent_leader(
    monkeypatch, root_group, same_identity, complete,
):
    from agent_py_agent.agent.tooling import process_registry as module

    rows = "12346 12345\n"
    if root_group is not None:
        rows += f"12345 {root_group}\n"
    monkeypatch.setattr(module, "capture_process_birth_token", lambda pid: "original-birth")
    monkeypatch.setattr(module, "_same_process", lambda *args: same_identity)
    monkeypatch.setattr(module.os, "getpgrp", lambda: 90000)
    monkeypatch.setattr(
        module.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout=rows),
    )

    assert module._exclusive_group_members(12345) == ([], complete)


def test_termination_receipt_keeps_proven_dead_pid_resolved_after_pid_reuse(monkeypatch):
    """宽限期内已证明消失的后代 PID，若在最终核对前被新进程复用，不能记回 unresolved（2026-09-25 CI 复现）。"""
    from agent_py_agent.agent.tooling import process_registry as registry

    root, child = 41000, 41001
    checks: dict[int, int] = {root: 0, child: 0}

    def fake_terminated(pid: int, _token: str) -> bool:
        checks[pid] = checks.get(pid, 0) + 1
        if pid == root:
            return True
        # 第一次核对：sleep 已被 TERM 杀掉；之后同号 PID 被别的进程复用且出生标识读不到。
        return checks[pid] == 1

    monkeypatch.setattr(registry, "_process_tree_snapshot", lambda _pid: ({root: "root-token", child: "child-token"}, True))
    monkeypatch.setattr(registry, "_signal_process_snapshot", lambda _snapshot, _signum: True)
    monkeypatch.setattr(registry, "_process_instance_terminated", fake_terminated)
    receipt = registry.terminate_process_tree(root, None, grace_seconds=0.2)
    assert receipt.method == "SIGTERM" and receipt.observed_processes == 2
    assert receipt.unresolved_pids == () and receipt.confirmed is True


def test_termination_without_handle_waits_long_enough_for_slow_exit(monkeypatch):
    """停止方没有 Popen 句柄时（停别的进程起的插件宿主），SIGKILL 后核对窗口要够慢主机把实例收走（2026-09-25 CI 3.10 复现）。"""
    from agent_py_agent.agent.tooling import process_registry as registry

    root, child = 42000, 42001
    started = time.monotonic()

    def slow_exit(pid: int, _token: str) -> bool:
        # 根立即消失；子进程在 SIGKILL 后 1.2 秒才消失，超过旧的 0.5 秒窗口、在 2 秒窗口内。
        return pid == root or time.monotonic() - started > 1.2

    monkeypatch.setattr(registry, "_process_tree_snapshot", lambda _pid: ({root: "r", child: "c"}, True))
    monkeypatch.setattr(registry, "_signal_process_snapshot", lambda _snapshot, _signum: True)
    monkeypatch.setattr(registry, "_same_process", lambda *_args: False)
    monkeypatch.setattr(registry, "_process_instance_terminated", slow_exit)
    receipt = registry.terminate_process_tree(root, None, grace_seconds=0)
    assert receipt.method == "SIGTERM->SIGKILL"
    assert receipt.confirmed is True and receipt.unresolved_pids == ()
