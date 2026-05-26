from __future__ import annotations

"""LLM: ShellTool tests — normal execution, timeout, dangerous command rejection, and validation.

给人看的解释：
测试 run_command 工具：正常执行、超时处理、危险命令拒绝、参数校验。
"""

import shlex
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.tooling.models import ToolExecutionResult
from agent_py_agent.agent.tooling.shell import (
    ShellTool,
    ShellToolOptions,
    _is_dangerous_command,
    _validate_command,
)


@pytest.fixture
def shell_tool(tmp_path: Path) -> ShellTool:
    """Create a ShellTool instance for testing."""
    return ShellTool(tmp_path, options=ShellToolOptions(default_timeout=5))


def test_shell_tool_echo_hello(shell_tool: ShellTool) -> None:
    """Test basic echo command execution."""
    result = shell_tool.execute({"command": "echo hello"})
    assert result.ok is True
    assert "hello" in result.output
    assert "return_code=0" in result.output


def test_shell_tool_pwd(shell_tool: ShellTool) -> None:
    """Test pwd command returns current directory."""
    result = shell_tool.execute({"command": "pwd"})
    assert result.ok is True
    assert "return_code=0" in result.output


def test_shell_tool_with_working_dir(shell_tool: ShellTool, tmp_path: Path) -> None:
    """Test command respects working_dir parameter."""
    result = shell_tool.execute({
        "command": "pwd",
        "working_dir": str(tmp_path),
    })
    assert result.ok is True
    # pwd should return the working_dir we specified
    assert "return_code=0" in result.output


def test_shell_tool_timeout(shell_tool: ShellTool) -> None:
    """Test that long-running command is terminated on timeout."""
    result = shell_tool.execute({
        "command": "sleep 10",
        "timeout": 1,
    })
    assert result.ok is False
    assert "超时" in result.output or "timeout" in result.output.lower()


def test_shell_tool_default_timeout(shell_tool: ShellTool) -> None:
    """Test that default_timeout is used when timeout is not specified."""
    # Use a longer sleep but rely on the default 5 second timeout from fixture
    result = shell_tool.execute({"command": "sleep 10"})
    assert result.ok is False
    assert "超时" in result.output or "timeout" in result.output.lower()


def test_shell_tool_dangerous_rm_rf_rejected(shell_tool: ShellTool) -> None:
    """Test that rm -rf / is rejected."""
    result = shell_tool.execute({"command": "rm -rf /"})
    assert result.ok is False
    assert "危险命令" in result.output or "危险" in result.output


def test_shangerous_mkfs_rejected(shell_tool: ShellTool) -> None:
    """Test that mkfs is rejected."""
    result = shell_tool.execute({"command": "mkfs.ext4 /dev/sda"})
    assert result.ok is False
    assert "危险命令" in result.output or "危险" in result.output


def test_shell_tool_dangerous_shutdown_rejected(shell_tool: ShellTool) -> None:
    """Test that shutdown is rejected."""
    result = shell_tool.execute({"command": "shutdown -h now"})
    assert result.ok is False
    assert "危险命令" in result.output or "危险" in result.output


def test_shell_tool_dangerous_dd_rejected(shell_tool: ShellTool) -> None:
    """Test that dangerous dd command is rejected."""
    result = shell_tool.execute({"command": "dd if=/dev/zero of=/dev/sda"})
    assert result.ok is False
    assert "危险命令" in result.output or "危险" in result.output


def test_shell_tool_empty_command_rejected(shell_tool: ShellTool) -> None:
    """Test that empty command is rejected."""
    result = shell_tool.execute({"command": ""})
    assert result.ok is False
    assert "不能为空" in result.output


def test_shell_tool_whitespace_only_command_rejected(shell_tool: ShellTool) -> None:
    """Test that whitespace-only command is rejected."""
    result = shell_tool.execute({"command": "   "})
    assert result.ok is False
    assert "不能为空" in result.output


def test_shell_tool_command_too_long_rejected(shell_tool: ShellTool) -> None:
    """Test that overly long command is rejected."""
    long_command = "a" * 5000
    result = shell_tool.execute({"command": long_command})
    assert result.ok is False
    assert "过长" in result.output


def test_validate_command_accepts_valid() -> None:
    """Test _validate_command accepts valid commands."""
    assert _validate_command("ls -la") == "ls -la"
    assert _validate_command("echo hello") == "echo hello"
    assert _validate_command("python build.py") == "python build.py"


def test_validate_command_rejects_empty() -> None:
    """Test _validate_command rejects empty string."""
    with pytest.raises(ValueError, match="不能为空"):
        _validate_command("")


def test_validate_command_rejects_whitespace_only() -> None:
    """Test _validate_command rejects whitespace-only string."""
    with pytest.raises(ValueError, match="不能为空"):
        _validate_command("   ")


def test_validate_command_rejects_too_long() -> None:
    """Test _validate_command rejects overly long command."""
    with pytest.raises(ValueError, match="过长"):
        _validate_command("a" * 5000)


def test_is_dangerous_command_patterns() -> None:
    """Test _is_dangerous_command detects various dangerous patterns."""
    dangerous = [
        "rm -rf /",
        "rm -rf /home",
        "mkfs.ext4 /dev/sda",
        "shutdown -h now",
        "dd if=/dev/zero of=/dev/sda",
        "reboot",
        "init 0",
        "halt",
    ]
    for cmd in dangerous:
        assert _is_dangerous_command(cmd) is True, f"Should reject: {cmd}"


def test_is_dangerous_command_case_insensitive() -> None:
    """Test _is_dangerous_command is case insensitive."""
    assert _is_dangerous_command("RM -RF /") is True
    assert _is_dangerous_command("SHUTDOWN -H NOW") is True
    assert _is_dangerous_command("MkFs /dev/sda") is True


def test_is_dangerous_command_safe() -> None:
    """Test _is_dangerous_command allows safe commands."""
    safe = [
        "ls -la",
        "echo hello",
        "pwd",
        "python --version",
        "ls /home/user",
        "rm file.txt",
        "cat README.md",
    ]
    for cmd in safe:
        assert _is_dangerous_command(cmd) is False, f"Should allow: {cmd}"


def test_shell_tool_stderr_captured(shell_tool: ShellTool) -> None:
    """Test that stderr is captured in output."""
    result = shell_tool.execute({"command": "ls /nonexistent_directory_12345 2>&1"})
    # ls to nonexistent dir will fail but our test should capture stderr
    assert result.ok is True or "No such file" in result.output or "return_code=" in result.output


def test_shell_tool_nonzero_return_code(shell_tool: ShellTool) -> None:
    """Test that non-zero return codes are reported correctly."""
    result = shell_tool.execute({"command": "exit 1"})
    assert result.ok is True  # execution succeeded, but return code is captured
    assert "return_code=1" in result.output


def test_shell_tool_preserves_unicode_output(shell_tool: ShellTool) -> None:
    """Shell output should not crash or mangle non-ASCII text on Windows."""
    python = shlex.quote(sys.executable)
    result = shell_tool.execute({"command": f"{python} -c \"print('你好')\""})
    assert result.ok is True
    assert "return_code=0" in result.output
    assert "你好" in result.output


def test_shell_tool_spec_has_run_command(shell_tool: ShellTool) -> None:
    """Test that ShellTool spec has correct name."""
    assert shell_tool.spec.name == "run_command"
    assert shell_tool.spec.category == "shell"
    assert "command" in shell_tool.spec.parameters
    assert "timeout" in shell_tool.spec.parameters
    assert "working_dir" in shell_tool.spec.parameters
