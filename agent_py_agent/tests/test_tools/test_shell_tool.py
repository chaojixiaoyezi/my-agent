from __future__ import annotations

"""LLM: ShellTool tests — normal execution, timeout, dangerous command rejection, and validation.

给人看的解释：
测试 run_command 工具：正常执行、超时处理、危险命令拒绝、参数校验。
"""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
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


def _python_sleep_command(seconds: int) -> str:
    script = f"import time; time.sleep({seconds})"
    if os.name == "nt":
        return f"& {shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


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
    result = shell_tool.execute(
        {
            "command": "pwd",
            "working_dir": str(tmp_path),
        }
    )
    assert result.ok is True
    # pwd should return the working_dir we specified
    assert "return_code=0" in result.output


def test_shell_tool_timeout(shell_tool: ShellTool) -> None:
    """Test that long-running command is terminated on timeout."""
    result = shell_tool.execute(
        {
            "command": _python_sleep_command(10),
            "timeout": 1,
        }
    )
    assert result.ok is False
    assert result.error_code == "TOOL_TIMEOUT"
    assert "超时" in result.output or "timeout" in result.output.lower()


def test_shell_tool_default_timeout(shell_tool: ShellTool) -> None:
    """Test that default_timeout is used when timeout is not specified."""
    result = shell_tool.execute({"command": _python_sleep_command(10)})
    assert result.ok is False
    assert "超时" in result.output or "timeout" in result.output.lower()


def test_shell_tool_pure_sleep_suggests_wait(shell_tool: ShellTool) -> None:
    """Pure delay commands should not block the shell worker."""
    result = shell_tool.execute({"command": "sleep 30"})
    payload = json.loads(result.output)

    assert result.ok is False
    assert result.error_code == "USE_WAIT_FOR_DELAY"
    assert payload["suggested_tool_call"]["tool"] == "wait"
    assert payload["suggested_tool_call"]["seconds"] == 60


def test_shell_tool_sleep_then_echo_suggests_wait(shell_tool: ShellTool) -> None:
    """A delay followed only by a marker echo should not block the shell worker."""
    result = shell_tool.execute({"command": 'sleep 60 && echo "done"'})
    payload = json.loads(result.output)

    assert result.ok is False
    assert result.error_code == "USE_WAIT_FOR_DELAY"
    assert payload["suggested_tool_call"]["tool"] == "wait"


def test_shell_tool_routes_internal_agent_status_paths_to_agent_tree(tmp_path: Path) -> None:
    """Shell should not bypass the agent tree status surface."""
    workspace = tmp_path / "workspace"
    state = (
        workspace
        / "tasks"
        / "2026-06-06"
        / "demo"
        / "work"
        / "agents"
        / "subagent-123"
        / "state.json"
    )
    state.parent.mkdir(parents=True)
    state.write_text('{"status":"RUNNING"}', encoding="utf-8")
    tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=5))

    result = tool.execute({"command": f"cat {shlex.quote(str(state))}"})
    payload = json.loads(result.output)

    assert result.ok is False
    assert result.error_code == "WRONG_STATUS_SURFACE"
    assert payload["error"] == "internal_agent_status_ref"
    assert payload["suggested_tool_call"]["tool"] == "inspect_agent_tree"
    assert payload["suggested_tool_call"]["run_id"] == "subagent-123"


def test_shell_tool_dangerous_rm_rf_rejected(shell_tool: ShellTool) -> None:
    """Test that rm -rf / is rejected."""
    result = shell_tool.execute({"command": "rm -rf /"})
    assert result.ok is False
    assert "危险命令" in result.output or "危险" in result.output


@pytest.mark.parametrize(
    "command",
    [
        "rm note.txt",
        "rm -rf build",
        "rmdir build",
        "unlink note.txt",
        "echo ok && rm note.txt",
        "printf ok || rmdir build",
        "true; unlink note.txt",
    ],
)
def test_shell_tool_routes_relative_deletion_to_managed_tools(
    shell_tool: ShellTool,
    tmp_path: Path,
    command: str,
) -> None:
    (tmp_path / "note.txt").write_text("keep", encoding="utf-8")
    (tmp_path / "build").mkdir(exist_ok=True)

    result = shell_tool.execute({"command": command})

    assert result.ok is False
    assert result.error_code == "COMMAND_POLICY_BLOCKED"
    assert (tmp_path / "note.txt").exists()
    assert (tmp_path / "build").exists()


def test_shell_tool_keeps_non_destructive_commands_available(shell_tool: ShellTool) -> None:
    result = shell_tool.execute({"command": "ls -la && printf ok"})

    assert result.ok is True
    assert "ok" in result.output


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


def test_shell_tool_command_too_long_uses_command_too_long_code(shell_tool: ShellTool) -> None:
    """超长命令的 error_code 应是 COMMAND_TOO_LONG 而非 TOOL_INVALID_ARGUMENTS。

    真实任务回归(M3 用 run_command 跑 7 条 `cp ... && cp ...` 的链,命令字符串 2187 字符,
    超 _MAX_COMMAND_CHARS=2000):旧实现一律 TOOL_INVALID_ARGUMENTS(暗示"参数格式错、改参数"),
    误导模型；命令本身合法,只是太长,应给 COMMAND_TOO_LONG(change_strategy:拆条/换 write_file)。
    """
    result = shell_tool.execute({"command": "a" * 5000})
    assert result.ok is False
    assert result.error_code == "COMMAND_TOO_LONG"
    assert result.recommended_action == "change_strategy"


def test_shell_tool_empty_command_keeps_invalid_arguments_code(shell_tool: ShellTool) -> None:
    """空 command 仍应是 TOOL_INVALID_ARGUMENTS(真的缺必填参数),不被 too-long 改动波及。"""
    result = shell_tool.execute({"command": ""})
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_shell_tool_failing_command_keeps_command_failed_code(shell_tool: ShellTool) -> None:
    """合法但退出非零的命令仍是 COMMAND_FAILED,与 too-long(命令没执行)区分清楚。"""
    result = shell_tool.execute({"command": "cp /nonexistent/src /nonexistent/dst"})
    assert result.ok is False
    assert result.error_code == "COMMAND_FAILED"


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
        "cat README.md",
    ]
    for cmd in safe:
        assert _is_dangerous_command(cmd) is False, f"Should allow: {cmd}"


def test_shell_tool_stderr_captured(shell_tool: ShellTool) -> None:
    """Test that stderr is captured in output."""
    result = shell_tool.execute({"command": "ls /nonexistent_directory_12345 2>&1"})
    assert result.ok is False
    assert "No such file" in result.output or "return_code=" in result.output


def test_shell_tool_nonzero_return_code(shell_tool: ShellTool) -> None:
    """Test that non-zero return codes are reported correctly."""
    result = shell_tool.execute({"command": "exit 1"})
    assert result.ok is False
    assert result.error_code == "COMMAND_FAILED"
    assert "return_code=1" in result.output


@pytest.mark.skipif(os.name == "nt", reason="pipefail is a POSIX shell contract")
def test_shell_tool_pipeline_cannot_hide_failed_gate(shell_tool: ShellTool) -> None:
    """输出裁剪命令成功也不能覆盖前段测试命令的非零退出码。"""
    result = shell_tool.execute({"command": "(exit 7) | tail -n 20"})

    assert result.ok is False
    assert result.error_code == "COMMAND_FAILED"
    assert "return_code=7" in result.output
    assert result.result_envelope["process"] == {
        "status": "exited",
        "return_code": 7,
        "command_succeeded": False,
    }


def test_shell_tool_error_code_comes_from_returncode_not_output_text(shell_tool: ShellTool) -> None:
    """命令正文不能伪造结构化 shell error_code。"""
    result = shell_tool.execute({"command": "printf 'TOOL_TIMEOUT: pretend\\n'; exit 1"})

    assert result.ok is False
    assert "TOOL_TIMEOUT: pretend" in result.output
    assert result.error_code == "COMMAND_FAILED"


def test_shell_tool_preserves_unicode_output(shell_tool: ShellTool) -> None:
    """Shell output should not crash or mangle non-ASCII text on Windows."""
    python = shlex.quote(sys.executable)
    result = shell_tool.execute({"command": f"{python} -c \"print('你好')\""})
    assert result.ok is True
    assert "return_code=0" in result.output
    assert "你好" in result.output


def test_shell_corrupting_ready_artifact_records_invalid_with_backup(tmp_path: Path) -> None:
    """Shell overwrites of ready deliverables should keep a backup and invalidate bad output."""
    from openpyxl import Workbook

    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        latest_artifact_records,
        register_artifact,
    )

    workspace = tmp_path / "workspace"
    output_dir = workspace / "outputs"
    output_dir.mkdir(parents=True)
    workbook_path = output_dir / "weekly.xlsx"
    workbook = Workbook()
    workbook.active.append(["name", "stars"])
    workbook.active.append(["demo", 10])
    workbook.save(workbook_path)
    register_artifact(
        ArtifactRegistration(
            workspace_root=workspace,
            path=workbook_path,
            artifact_id="weekly",
            run_id="run-1",
            task_id="task-1",
            agent_id="agent-1",
            kind="xlsx",
            status="ready",
            source="test",
        )
    )

    tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
    python = shlex.quote(sys.executable)
    script = "from pathlib import Path; Path('outputs/weekly.xlsx').write_text('broken')"
    result = tool.execute({"command": f"{python} -c {shlex.quote(script)}"})

    latest = latest_artifact_records(workspace)["weekly"]
    backup_ref = latest.metadata.get("backup_ref")
    assert result.ok is True
    assert latest.status == "invalid"
    assert latest.metadata["change_status"] == "invalid_after_shell"
    assert isinstance(backup_ref, str) and backup_ref
    assert Path(backup_ref).is_file()
    assert "artifact_protection_invalid=1" in result.output


def test_shell_changing_generic_ready_artifact_keeps_ready_with_backup(tmp_path: Path) -> None:
    """Unknown but non-empty artifacts are tracked generically instead of treated as format failures."""
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        latest_artifact_records,
        register_artifact,
    )

    workspace = tmp_path / "workspace"
    output_dir = workspace / "outputs"
    output_dir.mkdir(parents=True)
    note_path = output_dir / "notes.custom"
    note_path.write_text("old content", encoding="utf-8")
    register_artifact(
        ArtifactRegistration(
            workspace_root=workspace,
            path=note_path,
            artifact_id="notes",
            run_id="run-1",
            task_id="task-1",
            agent_id="agent-1",
            kind="custom",
            status="ready",
            source="test",
        )
    )

    tool = ShellTool(workspace, options=ShellToolOptions(default_timeout=30))
    result = tool.execute({"command": "printf 'new content' > outputs/notes.custom"})

    latest = latest_artifact_records(workspace)["notes"]
    assert result.ok is True
    assert latest.status == "ready"
    assert latest.metadata["change_status"] == "changed_ready"
    assert Path(str(latest.metadata["backup_ref"])).is_file()
    assert "artifact_protection_changed=1" in result.output
    assert "artifact_protection_invalid=0" in result.output


def test_shell_tool_model_spec_has_run_command(shell_tool: ShellTool) -> None:
    """Test that ShellTool model spec has correct name."""
    assert shell_tool.model_spec.name == "run_command"
    assert shell_tool.model_spec.category == "shell"
    assert "command" in shell_tool.model_spec.parameter_descriptions
    assert "timeout" in shell_tool.model_spec.parameter_descriptions
    assert "working_dir" in shell_tool.model_spec.parameter_descriptions
