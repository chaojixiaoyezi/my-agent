"""Shell 命令执行工具测试 - 命令执行、超时控制、输出捕获。"""
from __future__ import annotations

import json
import os
import shlex
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from agent_py_agent.agent.tooling.cancellation import (
    CancellationToken,
    bind_cancellation_token,
)


def _python_sleep_command(seconds: int) -> str:
    script = f"import time; time.sleep({seconds})"
    if os.name == "nt":
        return f"& {shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


class TestShellToolBasics:
    """测试 ShellTool 基本功能。"""

    def test_tool_contract_exposes_persistent_sandbox_tmp(self, tmp_path: Path):
        """模型必须知道沙箱 /tmp 持久于任务区 .sandbox-tmp，最终产物放工作区。"""
        from agent_py_agent.agent.tooling.shell import ShellTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        model_spec = ShellTool(workspace).model_spec

        assert any(
            "/tmp" in item
            and ".sandbox-tmp" in item
            and "selected workspace" in item
            for item in model_spec.hints.avoid_when
        )

    def test_run_simple_command(self, tmp_path: Path):
        """执行简单命令。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "echo hello"})

        assert result.ok is True
        assert "return_code=0" in result.output
        assert "hello" in result.output

    def test_running_process_obeys_bound_cancellation_token(self, tmp_path: Path):
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        token = CancellationToken()
        observed = {}

        def run() -> None:
            with bind_cancellation_token(token):
                observed["result"] = tool.execute(
                    {"command": _python_sleep_command(20)}
                )

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.3)
        token.cancel("test stop")
        thread.join(5)

        assert not thread.is_alive()
        assert observed["result"].error_code == "CANCELLED"

    def test_background_process_is_terminated_when_owning_token_is_cancelled(
        self,
        tmp_path: Path,
    ):
        from agent_py_agent.agent.tooling.process_registry import process_registry
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        token = CancellationToken()
        with bind_cancellation_token(token):
            result = tool.execute(
                {
                    "command": _python_sleep_command(20),
                    "run_in_background": True,
                }
            )
        assert result.ok is True
        session_id = json.loads(result.output)["session_id"]

        token.cancel("test stop")
        deadline = time.monotonic() + 4
        status = process_registry.status(session_id)
        while status and status["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
            status = process_registry.status(session_id)

        assert status is not None
        assert status["status"] == "exited"

    def test_run_command_with_cwd(self, tmp_path: Path):
        """指定工作目录执行命令。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({
            "command": "pwd",
            "working_dir": str(workspace),
        })

        assert result.ok is True

    def test_run_command_returns_nonzero(self, tmp_path: Path):
        """命令返回非零状态码。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "exit 1"})

        assert result.ok is False
        assert "return_code=1" in result.output

    def test_capture_stdout_and_stderr(self, tmp_path: Path):
        """捕获 stdout 和 stderr。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "echo stdout && echo stderr >&2"})

        assert result.ok is True
        assert "stdout" in result.output
        assert "stderr" in result.output


class TestShellToolDangerousCommands:
    """测试危险命令拦截。"""

    def test_block_rm_rf_root(self, tmp_path: Path):
        """拦截 rm -rf / 危险命令。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "rm -rf /"})

        assert result.ok is False
        assert "危险命令" in result.output

    def test_block_mkfs(self, tmp_path: Path):
        """拦截 mkfs 命令。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "mkfs.ext4 /dev/sda"})

        assert result.ok is False
        assert "危险命令" in result.output

    def test_block_shutdown(self, tmp_path: Path):
        """拦截 shutdown 相关命令。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "shutdown -h now"})

        assert result.ok is False
        assert "危险命令" in result.output

    def test_block_reboot(self, tmp_path: Path):
        """拦截 reboot 命令。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "reboot"})

        assert result.ok is False
        assert "危险命令" in result.output

    def test_block_command_injection(self, tmp_path: Path):
        """拦截包装后的根目录删除。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "sudo rm -rf /"})

        assert result.ok is False
        assert "危险命令" in result.output

    def test_case_insensitive_blocking(self, tmp_path: Path):
        """危险命令拦截不区分大小写。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "SHUTDOWN -H NOW"})

        assert result.ok is False
        assert "危险命令" in result.output


class TestShellToolTimeout:
    """测试超时控制。"""

    @patch("subprocess.Popen")
    def test_custom_timeout(self, mock_popen, tmp_path: Path):
        """自定义超时时间传给可中断的前台等待器。"""
        import subprocess

        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        proc = mock_popen.return_value
        completed = subprocess.CompletedProcess("cmd", 0, "", "")
        with patch("agent_py_agent.agent.tooling.shell._communicate_process", return_value=completed) as communicate:
            tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
            tool.execute({"command": _python_sleep_command(100), "timeout": 5})

        assert communicate.call_args.kwargs.get("timeout") == 5
        assert communicate.call_args.args[0] is proc

    @patch("subprocess.Popen")
    def test_default_timeout_used(self, mock_popen, tmp_path: Path):
        """未指定超时使用默认值。"""
        import subprocess

        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        completed = subprocess.CompletedProcess("cmd", 0, "", "")
        with patch("agent_py_agent.agent.tooling.shell._communicate_process", return_value=completed) as communicate:
            tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
            tool.execute({"command": _python_sleep_command(100)})

        assert communicate.call_args.kwargs.get("timeout") == 30

    @patch("subprocess.Popen")
    def test_invalid_timeout_uses_default(self, mock_popen, tmp_path: Path):
        """无效超时值使用默认值。"""
        import subprocess

        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        completed = subprocess.CompletedProcess("cmd", 0, "", "")
        with patch("agent_py_agent.agent.tooling.shell._communicate_process", return_value=completed) as communicate:
            tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
            tool.execute({"command": "echo hello", "timeout": -5})

        assert communicate.call_args.kwargs.get("timeout") == 30  # 回退到默认值

    def test_timeout_returns_error(self, tmp_path: Path):
        """超时时应返回错误。"""
        import subprocess

        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=1))
        result = tool.execute({"command": _python_sleep_command(100)})

        assert result.ok is False
        assert "超时" in result.output

    def test_pure_delay_uses_wait_surface(self, tmp_path: Path):
        """纯等待命令不应占住 shell worker。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "sleep 100"})

        assert result.ok is False
        assert result.error_code == "USE_WAIT_FOR_DELAY"
        assert '"tool": "wait"' in result.output


class TestShellToolValidation:
    """测试命令校验。"""

    def test_empty_command_rejected(self, tmp_path: Path):
        """空命令被拒绝。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": ""})

        assert result.ok is False
        assert "不能为空" in result.output

    def test_whitespace_only_command_rejected(self, tmp_path: Path):
        """纯空白命令被拒绝。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "   "})

        assert result.ok is False

    def test_command_too_long_rejected(self, tmp_path: Path):
        """超长命令被拒绝。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        long_command = "echo " + "a" * 5000
        result = tool.execute({"command": long_command})

        assert result.ok is False
        assert "过长" in result.output

    def test_missing_command_param(self, tmp_path: Path):
        """缺少 command 参数。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({})

        assert result.ok is False


class TestShellToolEdgeCases:
    """测试边界情况。"""

    def test_invalid_working_dir_is_rejected(self, tmp_path: Path):
        """无效工作目录不会回退到工作区执行。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({
            "command": "pwd",
            "working_dir": "/nonexistent/path",
        })

        assert result.ok is False
        assert result.error_code == "PATH_NOT_FOUND"

    @patch("subprocess.Popen")
    def test_os_error_handled(self, mock_popen, tmp_path: Path):
        """OSError 错误处理(Popen 启动失败抛 OSError,上层转 COMMAND_FAILED)。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        mock_popen.side_effect = OSError("Command not found")

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "nonexistent_command"})

        assert result.ok is False
        assert "命令执行失败" in result.output

    def test_workspace_write_allows_external_working_dir_when_not_dangerous(self, tmp_path: Path):
        """默认 workspace-write 允许切到普通外部目录。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        external = tmp_path / "external"
        workspace.mkdir()
        external.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "pwd", "working_dir": str(external)})

        assert result.ok is True
        assert str(external) in result.output

    @patch("subprocess.Popen")
    def test_full_access_allows_external_working_dir(self, mock_popen, tmp_path: Path):
        """full-access 允许显式使用工作区外的已有目录(cwd 现传给 Popen)。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        external = tmp_path / "external"
        workspace.mkdir()
        external.mkdir()
        proc = mock_popen.return_value
        proc.pid = 999999
        proc.communicate.return_value = ("", "")
        proc.returncode = 0

        tool = ShellTool(workspace, options=ShellToolOptions(access_mode="full-access", default_timeout=30))
        result = tool.execute({"command": "pwd", "working_dir": str(external)})

        assert result.ok is True
        assert mock_popen.call_args.kwargs["cwd"] == str(external)

    def test_full_access_still_rejects_last_resort_dangerous_commands(self, tmp_path: Path):
        """full-access 也不等于可以执行 rm -rf / 这类系统级破坏命令。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(access_mode="full-access", default_timeout=30))
        result = tool.execute({"command": "rm -rf /"})

        assert result.ok is False
        assert "危险命令" in result.output

    def test_child_shell_access_override_can_narrow_full_access_to_normal_path_policy(self, tmp_path: Path):
        """父级 full-access 被降级后仍共用 normal 路径策略，而不是回到工作区白名单。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        external = tmp_path / "external"
        workspace.mkdir()
        external.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(access_mode="full-access", default_timeout=30))
        result = tool.execute({
            "command": "pwd",
            "working_dir": str(external),
            "__access_mode": "workspace-write",
        })

        assert result.ok is True
        assert str(external) in result.output


def test_failure_effect_outcome_three_state_classification():
    """失败副作用三态分类(长期助手 式,判定层只认结构化信号):

    只读命令失败 → not_started(明确失败,重做安全,不再说成「结果不确定」);
    写命令失败 → 不声明(沿通用合同保守 unknown 防重做);超时 → unknown(可能部分生效);
    成功/未启动失败 → 无声明(handler_executed=False 已由上层判 not_started)。
    """
    from agent_py_agent.agent.tooling.shell import ShellTool

    classify = ShellTool._failure_effect_outcome
    # 只读命令(grep)完整退出失败 → not_started
    assert classify("grep foo bar.txt", False, "COMMAND_FAILED") == "not_started"
    # 写命令(重定向)失败 → 不声明 → 保守 unknown
    assert classify("echo x > f.txt", False, "COMMAND_FAILED") == ""
    # 超时 → unknown(进程可能部分生效)
    assert classify("sleep 100", False, "TOOL_TIMEOUT") == "unknown"
    # 成功 → 无声明
    assert classify("grep foo bar.txt", True, "") == ""
    # 未启动的前置失败(策略拒绝)→ 无声明(handler_executed=False 已足够)
    assert classify("rm -rf /", False, "COMMAND_POLICY_BLOCKED") == ""
