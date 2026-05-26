"""Shell 命令执行工具测试 - 命令执行、超时控制、输出捕获。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


class TestShellToolBasics:
    """测试 ShellTool 基本功能。"""

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

        assert result.ok is True  # 命令执行了，只是返回非零
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

    def test_allow_workspace_file_delete(self, tmp_path: Path):
        """允许工作区内普通删除。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target = workspace / "old.txt"
        target.write_text("old", encoding="utf-8")

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "rm old.txt"})

        assert result.ok is True
        assert "return_code=0" in result.output
        assert not target.exists()

    def test_allow_workspace_recursive_cleanup(self, tmp_path: Path):
        """允许工作区内目录清理。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        build = workspace / "build"
        build.mkdir(parents=True)
        (build / "cache.txt").write_text("cache", encoding="utf-8")

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "rm -rf build"})

        assert result.ok is True
        assert "return_code=0" in result.output
        assert not build.exists()

    def test_workspace_write_blocks_external_delete_target(self, tmp_path: Path):
        """workspace-write 下删除目标也不能越出工作区。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        external = tmp_path / "external.txt"
        external.write_text("keep", encoding="utf-8")

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "rm ../external.txt"})

        assert result.ok is False
        assert "delete target outside workspace roots" in result.output
        assert external.exists()

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

    @patch("subprocess.run")
    def test_custom_timeout(self, mock_run, tmp_path: Path):
        """自定义超时时间。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        mock_run.side_effect = TimeoutError()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({
            "command": "sleep 100",
            "timeout": 5,
        })

        # 验证 subprocess.run 被调用时使用了正确的超时
        mock_run.assert_called()
        call_args = mock_run.call_args
        assert call_args.kwargs.get("timeout") == 5

    @patch("subprocess.run")
    def test_default_timeout_used(self, mock_run, tmp_path: Path):
        """未指定超时使用默认值。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        mock_run.side_effect = TimeoutError()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "sleep 100"})

        call_args = mock_run.call_args
        assert call_args.kwargs.get("timeout") == 30

    @patch("subprocess.run")
    def test_invalid_timeout_uses_default(self, mock_run, tmp_path: Path):
        """无效超时值使用默认值。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({
            "command": "echo hello",
            "timeout": -5,
        })

        call_args = mock_run.call_args
        assert call_args.kwargs.get("timeout") == 30  # 回退到默认值

    def test_timeout_returns_error(self, tmp_path: Path):
        """超时时应返回错误。"""
        import subprocess

        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=1))
        result = tool.execute({"command": "sleep 100"})

        assert result.ok is False
        assert "超时" in result.output


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

    def test_invalid_working_dir_fallback(self, tmp_path: Path):
        """无效工作目录回退到工作区。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({
            "command": "pwd",
            "working_dir": "/nonexistent/path",
        })

        assert result.ok is True

    @patch("subprocess.run")
    def test_os_error_handled(self, mock_run, tmp_path: Path):
        """OSError 错误处理。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        mock_run.side_effect = OSError("Command not found")

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "nonexistent_command"})

        assert result.ok is False
        assert "命令执行失败" in result.output

    def test_workspace_write_rejects_external_working_dir(self, tmp_path: Path):
        """默认 workspace-write 不允许把命令工作目录切到工作区外。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        external = tmp_path / "external"
        workspace.mkdir()
        external.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
        result = tool.execute({"command": "pwd", "working_dir": str(external)})

        assert result.ok is False
        assert result.error_code == "PATH_OUTSIDE_WORKSPACE"
        assert "access_mode=workspace-write" in result.output

    @patch("subprocess.run")
    def test_full_access_allows_external_working_dir(self, mock_run, tmp_path: Path):
        """full-access 允许显式使用工作区外的已有目录。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        external = tmp_path / "external"
        workspace.mkdir()
        external.mkdir()
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        tool = ShellTool(workspace, options=ShellToolOptions(access_mode="full-access", default_timeout=30))
        result = tool.execute({"command": "pwd", "working_dir": str(external)})

        assert result.ok is True
        assert mock_run.call_args.kwargs["cwd"] == str(external)

    def test_full_access_still_rejects_last_resort_dangerous_commands(self, tmp_path: Path):
        """full-access 也不等于可以执行 rm -rf / 这类系统级破坏命令。"""
        from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ShellTool(workspace, options=ShellToolOptions(access_mode="full-access", default_timeout=30))
        result = tool.execute({"command": "rm -rf /"})

        assert result.ok is False
        assert "危险命令" in result.output
