from __future__ import annotations

"""LLM: ShellTool tests — normal execution, timeout, dangerous command rejection, and validation.

给人看的解释：
测试 run_command 工具：正常执行、超时处理、危险命令拒绝、参数校验。
"""

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.shell import (
    ShellTool,
    ShellToolOptions,
    _is_dangerous_command,
    _validate_command,
)
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call


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
    assert result.effect_outcome == "not_started"
    assert payload["error"] == "internal_agent_status_ref"
    assert payload["next_action"] == "await_direct_child_lifecycle_event"


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


def test_shell_tool_accepts_long_valid_command(shell_tool: ShellTool) -> None:
    """合法长参数仍执行，不因过去的两千字符门要求模型拆写。"""
    value = "a" * 5000
    result = shell_tool.execute({"command": f"echo {value}"})
    assert result.ok is True
    assert value in result.output


def test_shell_gateway_schema_and_handler_accept_same_long_command(
    shell_tool: ShellTool, tmp_path: Path,
) -> None:
    """必须穿过规范执行入口；只测裸 handler 会漏掉 Schema 中的重复长度限制。"""
    assert "maxLength" not in shell_tool.model_spec.input_schema["properties"]["command"]
    value = "b" * 8000
    execution = execute_canonical_test_call(
        tmp_path, tools={"run_command": shell_tool}, tool_name="run_command",
        arguments={"command": f"echo {value}"},
    )
    assert execution.result.ok is True
    assert execution.result.handler_executed is True
    assert value in execution.result.output


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


def test_validate_command_preserves_long_command() -> None:
    """校验入口完整保留长命令，而不是截断造成错误执行。"""
    command = "echo " + "a" * 5000
    assert _validate_command(command) == command


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
    display = result.result_envelope["display"]
    assert display["kind"] == "command"
    assert display["return_code"] == 1


@pytest.mark.skipif(os.name == "nt", reason="pipefail is a POSIX shell contract")
def test_shell_tool_pipeline_cannot_hide_failed_gate(shell_tool: ShellTool) -> None:
    """输出裁剪命令成功也不能覆盖前段测试命令的非零退出码。"""
    result = shell_tool.execute({"command": "(exit 7) | tail -n 20"})

    assert result.ok is False
    assert result.error_code == "COMMAND_FAILED"
    assert "return_code=7" in result.output
    facts = dict(result.result_envelope["process"])
    termination = facts.pop("termination")
    assert termination["confirmed"] is True
    assert termination["return_code"] == 7
    assert facts == {
        "status": "exited",
        "return_code": 7,
        "command_succeeded": False,
        "stderr_chars": 0,
        "stderr_head": "",
        "capture": {
            "complete": True,
            "truncated": False,
            "bytes_seen": {"stdout": 0, "stderr": 0},
            "bytes_retained": {"stdout": 0, "stderr": 0},
            "bytes_discarded": {"stdout": 0, "stderr": 0},
            "streams_truncated": {"stdout": False, "stderr": False},
            "streams_eof": {"stdout": True, "stderr": True},
            "streams_invalid_utf8": {"stdout": False, "stderr": False},
            "errors": {},
            "limit_bytes_per_stream": 4 * 1024 * 1024,
        },
    }


@pytest.mark.skipif(os.name == "nt", reason="exit 127 判定是 POSIX shell 契约")
def test_shell_tool_command_not_found_effect_outcome_failed(shell_tool: ShellTool) -> None:
    """127 证明 shell 失败退出，不证明前置重定向或命令没产生副作用；保留真实原因。"""
    result = shell_tool.execute({"command": "definitely_not_a_command_xyz_12345 -x"})
    assert result.ok is False
    assert result.error_code == "COMMAND_FAILED"
    assert result.effect_outcome == "failed"
    assert result.result_envelope["process"]["return_code"] == 127
    assert result.result_envelope["process"]["stderr_chars"] > 0
    # 输出必须保留 shell 真实原因,模型才能改命令
    assert result.output  # 非空即含 stderr 证据


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell")
def test_shell_127_after_write_is_not_zero_effect(shell_tool: ShellTool) -> None:
    result = shell_tool.execute({
        "command": "printf audit >> audit-127.txt; nonexistent_audit_command_223"
    })
    assert (shell_tool.workspace_root / "audit-127.txt").read_text() == "audit"
    assert result.error_code == "COMMAND_FAILED"
    assert result.result_envelope["process"]["return_code"] == 127
    assert result.effect_outcome == "failed"


@pytest.mark.skipif(os.name == "nt", reason="exit 127 判定是 POSIX shell 契约")
def test_shell_tool_explicit_exit_127_keeps_unknown(shell_tool: ShellTool) -> None:
    """显式 exit 127(无 stderr 报错)不得声明 not_started。

    写命令显式退出 127 无法证明零副作用,保持保守 unknown(防重复副作用)。
    判定必须同时满足: return_code==127 且 stderr_chars>0(结构化, locale 无关)。
    """
    result = shell_tool.execute({"command": "printf 'side-effect-before'; exit 127"})
    assert result.ok is False
    assert result.error_code == "COMMAND_FAILED"
    assert result.result_envelope["process"]["return_code"] == 127
    assert result.result_envelope["process"]["stderr_chars"] == 0
    assert result.effect_outcome != "not_started"


@pytest.mark.skipif(os.name == "nt", reason="exit 127 判定是 POSIX shell 契约")
def test_shell_tool_exit_127_with_stderr_keeps_unknown(shell_tool: ShellTool) -> None:
    """显式 exit 127 且带 stderr 输出的命令不得声明 not_started。

    维护记录 边界: 显式 `exit 127`、带 stderr 的脚本——命令确实执行了
    (stderr 有内容), 副作用可能已发生, 必须保持保守 unknown, 不能仅凭
    return_code==127 就误判成零副作用。
    """
    result = shell_tool.execute(
        {"command": "echo 'warning: something happened' >&2; exit 127"}
    )
    assert result.ok is False
    assert result.error_code == "COMMAND_FAILED"
    assert result.result_envelope["process"]["return_code"] == 127
    assert result.result_envelope["process"]["stderr_chars"] > 0
    # stderr 非空是命令真实执行过的结构化证据, 副作用不确定 → 不得 not_started
    assert result.effect_outcome != "not_started"


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
    from agent_py_agent.agent.artifacts.shell_protection import (
        resolve_shell_artifact_backup,
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

    backup_root = tmp_path / "owner" / "data" / "artifact_backups"
    tool = ShellTool(
        workspace,
        options=ShellToolOptions(
            artifact_backup_root=backup_root,
            default_timeout=30,
        ),
    )
    python = shlex.quote(sys.executable)
    script = "from pathlib import Path; Path('outputs/weekly.xlsx').write_text('broken')"
    result = tool.execute({"command": f"{python} -c {shlex.quote(script)}"})

    latest = latest_artifact_records(workspace)["weekly"]
    backup_ref = latest.metadata.get("backup_ref")
    assert result.ok is True
    assert latest.status == "invalid"
    assert latest.metadata["change_status"] == "invalid_after_shell"
    assert isinstance(backup_ref, str) and backup_ref
    backup = resolve_shell_artifact_backup(backup_root, backup_ref)
    assert backup.is_file()
    assert backup.read_bytes() != b"broken"
    assert backup.suffix == ".blob"
    assert not backup.is_relative_to(workspace)
    assert str(tmp_path) not in backup_ref
    assert "artifact_protection_invalid=1" in result.output


def test_shell_changing_generic_ready_artifact_keeps_ready_with_backup(tmp_path: Path) -> None:
    """Unknown but non-empty artifacts are tracked generically instead of treated as format failures."""
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        latest_artifact_records,
        register_artifact,
    )
    from agent_py_agent.agent.artifacts.shell_protection import (
        resolve_shell_artifact_backup,
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

    backup_root = tmp_path / "owner" / "data" / "artifact_backups"
    tool = ShellTool(
        workspace,
        options=ShellToolOptions(
            artifact_backup_root=backup_root,
            default_timeout=30,
        ),
    )
    result = tool.execute({"command": "printf 'new content' > outputs/notes.custom"})

    latest = latest_artifact_records(workspace)["notes"]
    assert result.ok is True
    assert latest.status == "ready"
    assert latest.metadata["change_status"] == "changed_ready"
    backup_ref = str(latest.metadata["backup_ref"])
    backup = resolve_shell_artifact_backup(backup_root, backup_ref)
    assert backup.read_text(encoding="utf-8") == "old content"
    assert not backup.is_relative_to(workspace)
    assert str(tmp_path) not in backup_ref
    assert "artifact_protection_changed=1" in result.output
    assert "artifact_protection_invalid=0" in result.output


def test_shell_unchanged_artifact_discards_prebackup(tmp_path: Path) -> None:
    """A read-only shell must not accumulate artifact backup blobs or operation dirs."""
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact

    workspace = tmp_path / "workspace"
    output_dir = workspace / "outputs"
    output_dir.mkdir(parents=True)
    artifact = output_dir / "test_generated.py"
    artifact.write_text("def test_generated():\n    assert True\n", encoding="utf-8")
    register_artifact(
        ArtifactRegistration(
            workspace_root=workspace,
            path=artifact,
            artifact_id="generated-test",
            run_id="run-1",
            task_id="task-1",
            agent_id="agent-1",
            kind="python",
            status="ready",
            source="test",
        )
    )
    backup_root = tmp_path / "owner" / "data" / "artifact_backups"
    tool = ShellTool(
        workspace,
        options=ShellToolOptions(
            artifact_backup_root=backup_root,
            default_timeout=30,
        ),
    )

    result = tool.execute({"command": "true"})

    assert result.ok is True
    assert not list(backup_root.rglob("*.blob"))
    schema_root = backup_root / "v1"
    assert not list(schema_root.rglob("operation.json"))
    assert not (workspace / "data" / "artifacts" / "shell_backups").exists()


def test_shell_preimage_skips_tool_archives_but_keeps_open_world_artifacts(
    tmp_path: Path,
) -> None:
    """Tool archives are not deliverables; unknown kinds stay protected by default."""
    from agent_py_agent.agent.artifacts.registry import (
        ARTIFACT_ROLE_METADATA_KEY,
        ARTIFACT_ROLE_TOOL_OUTPUT_ARCHIVE,
        SHELL_PREIMAGE_POLICY_EXCLUDE,
        SHELL_PREIMAGE_POLICY_INCLUDE,
        SHELL_PREIMAGE_POLICY_METADATA_KEY,
        ArtifactRegistration,
        register_artifact,
    )
    from agent_py_agent.agent.artifacts.shell_protection import snapshot_ready_artifacts

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cases = (
        ("legacy-output", "tool_output", {}, False),
        (
            "typed-output",
            "tool_output",
            {
                ARTIFACT_ROLE_METADATA_KEY: ARTIFACT_ROLE_TOOL_OUTPUT_ARCHIVE,
                SHELL_PREIMAGE_POLICY_METADATA_KEY: SHELL_PREIMAGE_POLICY_EXCLUDE,
            },
            False,
        ),
        (
            "explicit-output",
            "tool_output",
            {SHELL_PREIMAGE_POLICY_METADATA_KEY: SHELL_PREIMAGE_POLICY_INCLUDE},
            True,
        ),
        ("future-format", "future_custom_format", {}, True),
    )
    expected: set[str] = set()
    for artifact_id, kind, metadata, protected in cases:
        path = workspace / f"{artifact_id}.data"
        path.write_text(artifact_id, encoding="utf-8")
        register_artifact(
            ArtifactRegistration(
                workspace_root=workspace,
                path=path,
                artifact_id=artifact_id,
                run_id="run-policy",
                task_id="task-policy",
                agent_id="agent-policy",
                kind=kind,
                status="ready",
                source="test",
                metadata=metadata,
            )
        )
        if protected:
            expected.add(artifact_id)

    snapshots = snapshot_ready_artifacts(
        workspace,
        tmp_path / "owner" / "artifact_backups",
        run_scope={"owner_id": "owner", "run_id": "run-policy", "task_id": "task-policy"},
        operation_id="operation-policy",
    )

    assert {item.artifact_id for item in snapshots} == expected


def test_shell_settlement_removes_manifest_but_keeps_changed_preimage(
    tmp_path: Path,
) -> None:
    """Managed settlement closes the manifest window without deleting recovery bytes."""
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        latest_artifact_records,
        register_artifact,
    )
    from agent_py_agent.agent.artifacts.shell_protection import resolve_shell_artifact_backup
    from agent_py_agent.agent.tooling.models import ToolOperationSettlementContext

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "report.md"
    artifact.write_text("before", encoding="utf-8")
    register_artifact(
        ArtifactRegistration(
            workspace_root=workspace,
            path=artifact,
            artifact_id="report",
            run_id="run-settle",
            task_id="task-settle",
            agent_id="agent-settle",
            status="ready",
        )
    )
    backup_root = tmp_path / "owner" / "artifact_backups"
    tool = ShellTool(
        workspace,
        options=ShellToolOptions(artifact_backup_root=backup_root),
    )
    run_scope = {
        "owner_id": "owner-settle",
        "run_id": "run-settle",
        "task_id": "task-settle",
    }

    result = tool.execute(
        {
            "command": "printf after > report.md",
            "__run_scope": run_scope,
            "__tool_call_id": "call-settle",
            "__operation_id": "operation-settle",
            "__operation_managed": True,
        }
    )

    record = latest_artifact_records(workspace)["report"]
    backup_ref = str(record.metadata["backup_ref"])
    assert result.ok is True
    assert list(backup_root.rglob("operation.json"))
    assert resolve_shell_artifact_backup(backup_root, backup_ref).read_text() == "before"

    tool.on_operation_settled(
        {},
        ToolOperationSettlementContext(
            owner_id="owner-settle",
            run_id="run-settle",
            task_id="task-settle",
            operation_id="operation-settle",
            tool_name="run_command",
            args_hash="hash",
            idempotency_key="key",
            idempotency_scope="operation",
            status="succeeded",
        ),
    )

    assert not list(backup_root.rglob("operation.json"))
    assert resolve_shell_artifact_backup(backup_root, backup_ref).read_text() == "before"


def test_shell_pytest_does_not_collect_internal_artifact_backup(tmp_path: Path) -> None:
    """The pre-shell copy of a test artifact must stay outside project discovery."""
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    test_file = workspace / "test_generated.py"
    test_file.write_text("def test_generated():\n    assert True\n", encoding="utf-8")
    register_artifact(
        ArtifactRegistration(
            workspace_root=workspace,
            path=test_file,
            artifact_id="generated-test",
            run_id="run-1",
            task_id="task-1",
            agent_id="agent-1",
            kind="python",
            status="ready",
            source="test",
        )
    )
    backup_root = tmp_path / "owner" / "data" / "artifact_backups"
    tool = ShellTool(
        workspace,
        options=ShellToolOptions(
            artifact_backup_root=backup_root,
            default_timeout=30,
        ),
    )

    result = tool.execute(
        {"command": f"{shlex.quote(sys.executable)} -m pytest --collect-only -q"}
    )

    assert result.ok is True
    assert result.output.count("test_generated.py::test_generated") == 1
    assert "shell_backups" not in result.output
    assert not list(backup_root.rglob("*.blob"))


def test_shell_artifact_backup_refs_are_owner_isolated(tmp_path: Path) -> None:
    """Identical artifact/run names in two owners must resolve under different stores."""
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
    from agent_py_agent.agent.artifacts.shell_protection import (
        resolve_shell_artifact_backup,
        snapshot_ready_artifacts,
    )

    resolved: list[Path] = []
    refs: list[str] = []
    for owner_name in ("alice", "bob"):
        workspace = tmp_path / owner_name / "tasks" / "same-task"
        workspace.mkdir(parents=True)
        artifact = workspace / "same.py"
        artifact.write_text("print('same')\n", encoding="utf-8")
        register_artifact(
            ArtifactRegistration(
                workspace_root=workspace,
                path=artifact,
                artifact_id="same-artifact",
                run_id="same-run",
                task_id="same-task",
                agent_id="same-agent",
                kind="python",
                status="ready",
                source="test",
            )
        )
        backup_root = tmp_path / owner_name / "data" / "artifact_backups"
        snapshots = snapshot_ready_artifacts(
            workspace,
            backup_root,
            run_scope={
                "owner_id": owner_name,
                "task_id": "same-task",
                "run_id": "same-run",
                "attempt_id": "attempt-1",
            },
            tool_call_id="call-1",
        )
        refs.append(snapshots[0].backup_ref)
        resolved.append(
            resolve_shell_artifact_backup(backup_root, snapshots[0].backup_ref)
        )

    assert resolved[0] != resolved[1]
    assert resolved[0].is_relative_to(tmp_path / "alice")
    assert resolved[1].is_relative_to(tmp_path / "bob")
    assert all(path.read_text(encoding="utf-8") == "print('same')\n" for path in resolved)
    assert all(str(tmp_path) not in ref for ref in refs)


def test_shell_artifact_backup_ref_rejects_traversal_and_symlink_escape(
    tmp_path: Path,
) -> None:
    """An opaque ref cannot climb or follow a symlink beyond the exact owner store."""
    from agent_py_agent.agent.artifacts.shell_protection import (
        resolve_shell_artifact_backup,
    )

    backup_root = tmp_path / "owner" / "data" / "artifact_backups"
    (backup_root / "v1").mkdir(parents=True)
    with pytest.raises(ValueError, match="invalid shell artifact backup ref"):
        resolve_shell_artifact_backup(
            backup_root,
            "owner-artifact-backup:v1/../../outside.blob",
        )
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (backup_root / "v1" / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    with pytest.raises(ValueError, match="escapes owner store"):
        resolve_shell_artifact_backup(
            backup_root,
            "owner-artifact-backup:v1/link/outside.blob",
        )


def test_shell_artifact_backup_failure_does_not_start_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Atomic backup failure must fail closed before any shell side effect starts."""
    from agent_py_agent.agent.artifacts import shell_protection
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "result.txt"
    artifact.write_text("ready", encoding="utf-8")
    second_artifact = workspace / "second.txt"
    second_artifact.write_text("also ready", encoding="utf-8")
    for artifact_id, path in (("result", artifact), ("second", second_artifact)):
        register_artifact(
            ArtifactRegistration(
                workspace_root=workspace,
                path=path,
                artifact_id=artifact_id,
                run_id="run-1",
                task_id="task-1",
                agent_id="agent-1",
                kind="text",
                status="ready",
                source="test",
            )
        )
    backup_root = tmp_path / "owner" / "data" / "artifact_backups"
    tool = ShellTool(
        workspace,
        options=ShellToolOptions(
            artifact_backup_root=backup_root,
            default_timeout=30,
        ),
    )

    real_replace = shell_protection.os.replace
    replace_count = 0

    def reject_second_replace(source: object, target: object) -> None:
        nonlocal replace_count
        replace_count += 1
        if replace_count == 2:
            raise OSError("simulated atomic rename failure")
        real_replace(source, target)

    monkeypatch.setattr(shell_protection.os, "replace", reject_second_replace)
    result = tool.execute({"command": "printf started > command-started.txt"})

    assert result.ok is False
    assert result.error_code == "ARTIFACT_BACKUP_FAILED"
    assert result.effect_outcome == "not_started"
    assert not (workspace / "command-started.txt").exists()
    assert not list(backup_root.rglob(".tmp-*"))
    assert not list(backup_root.rglob("*.blob"))


def test_shell_ready_artifact_without_canonical_backup_root_fails_closed(
    tmp_path: Path,
) -> None:
    """A bare tool may run normally, but cannot protect a ready artifact without an injected store."""
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "result.txt"
    artifact.write_text("ready", encoding="utf-8")
    register_artifact(
        ArtifactRegistration(workspace_root=workspace, path=artifact, status="ready")
    )

    result = ShellTool(workspace).execute(
        {"command": "printf started > command-started.txt"}
    )

    assert result.ok is False
    assert result.error_code == "ARTIFACT_BACKUP_FAILED"
    assert result.effect_outcome == "not_started"
    assert not (workspace / "command-started.txt").exists()
    assert not (workspace.parent / ".my-agent-artifact-backups").exists()


def test_full_access_artifact_roots_allow_empty_supplemental_reads(tmp_path: Path) -> None:
    """管理员 full + 无写根限制时，registry 注入的空读根不能被误解为全盘禁读。"""
    from agent_py_agent.agent.tooling.shell import _artifact_source_roots

    tool = ShellTool(tmp_path, options=ShellToolOptions(access_mode="full-access"))
    assert _artifact_source_roots(tool, None, (), effective_access_mode="full-access") is None
    assert _artifact_source_roots(tool, (), (), effective_access_mode="full-access") == ()
    scoped = ShellTool(tmp_path, options=ShellToolOptions(owner_scope_root=str(tmp_path)))
    assert _artifact_source_roots(scoped, (), (), effective_access_mode="full-access") == ()


def test_full_access_shell_runs_with_registered_artifact_and_empty_read_roots(tmp_path: Path) -> None:
    """复现真实 registry 参数：管理员无写根限制时，已有产物不能让普通命令启动前失败。"""
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "report.md"
    artifact.write_text("ready", encoding="utf-8")
    register_artifact(ArtifactRegistration(workspace_root=workspace, path=artifact, status="ready"))
    backup_root = tmp_path / "owner" / "artifact_backups"
    tool = ShellTool(workspace, options=ShellToolOptions(artifact_backup_root=backup_root))

    result = tool.execute({
        "command": "pwd",
        "__access_mode": "full-access",
        "__sandbox_read_roots": [],
    })

    assert result.ok is True
    assert str(workspace) in result.output
    assert artifact.read_text(encoding="utf-8") == "ready"
    assert not list(backup_root.rglob("*.blob"))


def test_shell_ignores_outside_artifact_without_reading_or_changing_it(
    tmp_path: Path,
) -> None:
    """越界旧引用不授权宿主读取外部文件，也不应阻断范围内普通命令。"""
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        register_observed_artifact,
    )

    owner = tmp_path / "owner"
    owner.mkdir()
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    register_observed_artifact(
        ArtifactRegistration(
            workspace_root=owner,
            path=outside,
            artifact_id="forged",
            status="ready",
            source="forged-test",
        ),
        observed_sha256="forged",
        observed_size_bytes=6,
    )
    backup_root = owner / "private" / "artifact_backups"
    tool = ShellTool(
        owner,
        options=ShellToolOptions(
            owner_scope_root=str(owner),
            artifact_backup_root=backup_root,
        ),
    )

    result = tool.execute({"command": "printf started > command-started.txt"})

    assert result.ok is True
    assert "outside-secret" not in result.output
    assert (owner / "command-started.txt").read_text() == "started"
    assert outside.read_text() == "secret"
    assert not list(backup_root.rglob("*.blob"))
    assert result.result_envelope["artifact_protection"]["skipped_sources"] == {
        "outside_source_roots": 1,
    }


def test_shell_skips_missing_old_source_but_still_protects_current_artifact(tmp_path):
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        register_observed_artifact,
    )

    owner = tmp_path / "owner"
    owner.mkdir()
    current = owner / "current.txt"
    current.write_text("keep", encoding="utf-8")
    for name in ("missing.txt", "current.txt"):
        register_observed_artifact(
            ArtifactRegistration(
                workspace_root=owner, path=owner / name,
                artifact_id=name, kind="file", mime_type="text/plain", status="ready",
                source="historical-test",
            ),
            observed_sha256="before", observed_size_bytes=4,
        )
    tool = ShellTool(owner, options=ShellToolOptions(artifact_backup_root=owner / "private"))

    result = tool.execute({"command": "printf ready"})

    assert result.ok is True
    assert current.read_text() == "keep"
    assert not (owner / "missing.txt").exists()
    summary = result.result_envelope["artifact_protection"]
    assert summary["snapshots"] == 1
    assert summary["skipped_sources"] == {"source_missing": 1}
    assert len(summary["unchanged"]) == 1


def test_shell_rejects_symlink_ready_artifact_before_backup(tmp_path: Path) -> None:
    """A registry leaf symlink is rejected before its target bytes can enter the owner store."""
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        register_observed_artifact,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    artifact = workspace / "result.txt"
    try:
        artifact.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    register_observed_artifact(
        ArtifactRegistration(
            workspace_root=workspace,
            path=artifact,
            artifact_id="symlink",
            status="ready",
        ),
        observed_sha256="forged",
        observed_size_bytes=6,
    )
    backup_root = tmp_path / "owner" / "data" / "artifact_backups"

    result = ShellTool(
        workspace,
        options=ShellToolOptions(artifact_backup_root=backup_root),
    ).execute({"command": "printf started > command-started.txt"})

    assert result.ok is False
    assert result.error_code == "ARTIFACT_BACKUP_FAILED"
    assert result.effect_outcome == "not_started"
    assert not (workspace / "command-started.txt").exists()
    assert not list(backup_root.rglob("*.blob"))


def test_shell_postcheck_marks_replaced_symlink_invalid_without_following(
    tmp_path: Path,
) -> None:
    """A command-created symlink is an invalid postimage and never rewrites the registry to its target."""
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        latest_artifact_records,
        register_artifact,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "result.txt"
    artifact.write_text("ready", encoding="utf-8")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    register_artifact(
        ArtifactRegistration(
            workspace_root=workspace,
            path=artifact,
            artifact_id="result",
            status="ready",
        )
    )
    backup_root = tmp_path / "owner" / "data" / "artifact_backups"
    python = shlex.quote(sys.executable)
    script = (
        "from pathlib import Path; import os; "
        "p=Path('result.txt'); p.unlink(); "
        f"os.symlink({str(outside)!r}, p)"
    )

    result = ShellTool(
        workspace,
        options=ShellToolOptions(artifact_backup_root=backup_root),
    ).execute({"command": f"{python} -c {shlex.quote(script)}"})

    latest = latest_artifact_records(workspace)["result"]
    assert result.ok is True
    assert latest.status == "invalid"
    assert latest.path == str(artifact)
    assert latest.sha256 == ""
    assert latest.size_bytes == 0
    assert latest.metadata["findings"][0]["code"] == "ARTIFACT_SOURCE_SYMLINK_BLOCKED"
    assert outside.read_text(encoding="utf-8") == "secret"


def test_shell_postcheck_failure_is_typed_unknown_after_command_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconciliation failure cannot be reported as a successful or safe-to-retry command."""
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
    from agent_py_agent.agent.tooling import shell as shell_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "result.txt"
    artifact.write_text("ready", encoding="utf-8")
    register_artifact(
        ArtifactRegistration(workspace_root=workspace, path=artifact, status="ready")
    )
    backup_root = tmp_path / "owner" / "data" / "artifact_backups"

    def fail_postcheck(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise OSError("simulated postcheck failure")

    monkeypatch.setattr(
        shell_module,
        "reconcile_shell_artifact_operation_items",
        fail_postcheck,
    )
    result = ShellTool(
        workspace,
        options=ShellToolOptions(artifact_backup_root=backup_root),
    ).execute({"command": "printf changed > result.txt"})

    assert artifact.read_text(encoding="utf-8") == "changed"
    assert result.ok is False
    assert result.error_code == "ARTIFACT_POSTCHECK_FAILED"
    assert result.effect_outcome == "unknown"
    assert result.result_envelope["process"]["return_code"] == 0


def test_shell_unchanged_cleanup_failure_does_not_reclassify_command_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure to delete a proven-unneeded copy is cleanup debt, not unknown command effect."""
    from agent_py_agent.agent.artifacts import shell_protection
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "result.txt"
    artifact.write_text("ready", encoding="utf-8")
    register_artifact(
        ArtifactRegistration(workspace_root=workspace, path=artifact, status="ready")
    )
    backup_root = tmp_path / "owner" / "data" / "artifact_backups"
    monkeypatch.setattr(
        shell_protection,
        "_discard_backup",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("busy")),
    )

    result = ShellTool(
        workspace,
        options=ShellToolOptions(artifact_backup_root=backup_root),
    ).execute({"command": "true"})

    assert result.ok is True
    assert result.error_code == ""
    assert result.effect_outcome == ""
    assert result.result_envelope["artifact_protection"]["cleanup_deferred"]


def test_shell_rejects_tampered_recovery_blob_before_registry_append(
    tmp_path: Path,
) -> None:
    """Changed output cannot advertise a missing or modified recovery preimage."""
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
    from agent_py_agent.agent.artifacts.shell_protection import (
        reconcile_shell_artifacts,
        resolve_shell_artifact_backup,
        snapshot_ready_artifacts,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "result.txt"
    artifact.write_text("ready", encoding="utf-8")
    register_artifact(
        ArtifactRegistration(workspace_root=workspace, path=artifact, status="ready")
    )
    backup_root = tmp_path / "owner" / "data" / "artifact_backups"
    snapshots = snapshot_ready_artifacts(
        workspace,
        backup_root,
        source_roots=(workspace,),
    )
    artifact.write_text("changed", encoding="utf-8")
    resolve_shell_artifact_backup(backup_root, snapshots[0].backup_ref).write_text(
        "tampered", encoding="utf-8"
    )

    with pytest.raises(OSError, match="does not match"):
        reconcile_shell_artifacts(
            workspace,
            snapshots,
            backup_store_root=backup_root,
            source_roots=(workspace,),
        )


def test_shell_legacy_referenced_backup_migrates_and_is_idempotent(
    tmp_path: Path,
) -> None:
    """A latest legacy recovery ref is preserved under the owner store before project cleanup."""
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        latest_artifact_records,
        register_observed_artifact,
        registry_path,
    )
    from agent_py_agent.agent.artifacts.shell_protection import (
        resolve_shell_artifact_backup,
        snapshot_ready_artifacts,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "result.txt"
    artifact.write_text("broken", encoding="utf-8")
    legacy = workspace / "data" / "artifacts" / "shell_backups" / "1" / "result"
    legacy.mkdir(parents=True)
    old_backup = legacy / "result.txt"
    old_backup.write_text("ready", encoding="utf-8")
    register_observed_artifact(
        ArtifactRegistration(
            workspace_root=workspace,
            path=artifact,
            artifact_id="result",
            status="invalid",
            source="shell_postcheck",
            metadata={
                "change_status": "invalid_after_shell",
                "backup_ref": str(old_backup),
                "previous_sha256": hashlib.sha256(b"ready").hexdigest(),
                "previous_size_bytes": 5,
            },
        ),
        observed_sha256="broken-digest",
        observed_size_bytes=6,
    )
    backup_root = tmp_path / "owner" / "data" / "artifact_backups"

    assert snapshot_ready_artifacts(workspace, backup_root) == []
    latest = latest_artifact_records(workspace)["result"]
    migrated_ref = str(latest.metadata["backup_ref"])
    assert migrated_ref.startswith("owner-artifact-backup:v1/")
    assert resolve_shell_artifact_backup(backup_root, migrated_ref).read_text() == "ready"
    assert not (workspace / "data" / "artifacts" / "shell_backups").exists()
    line_count = len(registry_path(workspace).read_text(encoding="utf-8").splitlines())

    assert snapshot_ready_artifacts(workspace, backup_root) == []
    assert len(registry_path(workspace).read_text(encoding="utf-8").splitlines()) == line_count
    assert latest_artifact_records(workspace)["result"].metadata["backup_ref"] == migrated_ref


def test_shell_legacy_unreferenced_backups_are_removed(tmp_path: Path) -> None:
    """Old no-op copies with no authoritative registry ref are safe migration garbage."""
    from agent_py_agent.agent.artifacts.shell_protection import snapshot_ready_artifacts

    workspace = tmp_path / "workspace"
    legacy = workspace / "data" / "artifacts" / "shell_backups" / "1"
    legacy.mkdir(parents=True)
    (legacy / "test_duplicate.py").write_text("def test_x(): pass\n", encoding="utf-8")
    backup_root = tmp_path / "owner" / "data" / "artifact_backups"

    assert snapshot_ready_artifacts(workspace, backup_root) == []
    assert not (workspace / "data" / "artifacts" / "shell_backups").exists()
    assert not list(backup_root.rglob("*.blob"))


def test_shell_legacy_backup_ref_cannot_escape_exact_task_root(tmp_path: Path) -> None:
    """A forged legacy ref blocks migration without reading or deleting the outside file."""
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        register_observed_artifact,
    )
    from agent_py_agent.agent.artifacts.shell_protection import snapshot_ready_artifacts

    workspace = tmp_path / "workspace"
    legacy = workspace / "data" / "artifacts" / "shell_backups"
    legacy.mkdir(parents=True)
    (legacy / "unreferenced.txt").write_text("old", encoding="utf-8")
    artifact = workspace / "result.txt"
    artifact.write_text("broken", encoding="utf-8")
    outside = tmp_path / "outside-backup.txt"
    outside.write_text("secret", encoding="utf-8")
    register_observed_artifact(
        ArtifactRegistration(
            workspace_root=workspace,
            path=artifact,
            artifact_id="result",
            status="invalid",
            source="shell_postcheck",
            metadata={
                "change_status": "invalid_after_shell",
                "backup_ref": str(outside),
            },
        ),
        observed_sha256="broken",
        observed_size_bytes=6,
    )
    backup_root = tmp_path / "owner" / "data" / "artifact_backups"

    with pytest.raises(OSError, match="escapes task backup root"):
        snapshot_ready_artifacts(workspace, backup_root)
    assert outside.read_text(encoding="utf-8") == "secret"
    assert legacy.exists()
    assert not list(backup_root.rglob("*.blob"))


def test_shell_published_blob_is_removed_if_finalization_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure after atomic rename cannot strand an unreferenced final blob."""
    from agent_py_agent.agent.artifacts import shell_protection
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "result.txt"
    artifact.write_text("ready", encoding="utf-8")
    register_artifact(
        ArtifactRegistration(workspace_root=workspace, path=artifact, status="ready")
    )
    backup_root = tmp_path / "owner" / "data" / "artifact_backups"
    real_chmod = shell_protection._chmod_private

    def reject_blob(path: Path, *, directory: bool) -> None:
        if not directory:
            raise OSError("simulated chmod failure")
        real_chmod(path, directory=directory)

    monkeypatch.setattr(shell_protection, "_chmod_private", reject_blob)
    result = ShellTool(
        workspace,
        options=ShellToolOptions(artifact_backup_root=backup_root),
    ).execute({"command": "printf started > command-started.txt"})

    assert result.ok is False
    assert result.error_code == "ARTIFACT_BACKUP_FAILED"
    assert result.effect_outcome == "not_started"
    assert not (workspace / "command-started.txt").exists()
    assert not list(backup_root.rglob("*.blob"))


def test_shell_full_access_handler_honors_per_call_workspace_downgrade(
    tmp_path: Path,
) -> None:
    """A registry built for Full Access cannot bypass an explicit narrow invocation boundary."""
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        register_observed_artifact,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    register_observed_artifact(
        ArtifactRegistration(
            workspace_root=workspace,
            path=outside,
            artifact_id="forged",
            status="ready",
        ),
        observed_sha256="forged",
        observed_size_bytes=6,
    )
    backup_root = tmp_path / "owner" / "data" / "artifact_backups"
    tool = ShellTool(
        workspace,
        options=ShellToolOptions(
            access_mode="full-access",
            artifact_backup_root=backup_root,
        ),
    )

    result = tool.execute(
        {
            "command": "printf started > command-started.txt",
            "__access_mode": "workspace-write",
            "__sandbox_write_roots": [str(workspace)],
            "__sandbox_read_roots": [],
        }
    )

    assert result.ok is True
    assert (workspace / "command-started.txt").read_text() == "started"
    assert outside.read_text() == "secret"
    assert not list(backup_root.rglob("*.blob"))
    # 收窄必须影响真实备份范围；若错误沿用 Full Access，这里不会出现越界跳过计数。
    assert result.result_envelope["artifact_protection"]["skipped_sources"] == {
        "outside_source_roots": 1,
    }


@pytest.mark.parametrize("symlink_component", ["data", "artifacts"])
def test_shell_legacy_cleanup_rejects_parent_symlink_escape(
    tmp_path: Path,
    symlink_component: str,
) -> None:
    """Legacy cleanup never follows a data/artifacts parent link outside the task."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if symlink_component == "data":
        external_legacy = outside / "artifacts" / "shell_backups"
        external_legacy.mkdir(parents=True)
        try:
            (workspace / "data").symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation is unavailable on this platform")
    else:
        (workspace / "data").mkdir()
        external_legacy = outside / "shell_backups"
        external_legacy.mkdir(parents=True)
        try:
            (workspace / "data" / "artifacts").symlink_to(
                outside, target_is_directory=True
            )
        except OSError:
            pytest.skip("symlink creation is unavailable on this platform")
    sentinel = external_legacy / "test_must_survive.py"
    sentinel.write_text("secret", encoding="utf-8")
    backup_root = tmp_path / "owner" / "data" / "artifact_backups"

    result = ShellTool(
        workspace,
        options=ShellToolOptions(artifact_backup_root=backup_root),
    ).execute({"command": "printf started > command-started.txt"})

    assert result.ok is False
    assert result.error_code == "ARTIFACT_BACKUP_FAILED"
    assert result.effect_outcome == "not_started"
    assert sentinel.read_text(encoding="utf-8") == "secret"
    assert not (workspace / "command-started.txt").exists()


def test_shell_legacy_store_overlap_fails_before_copy_or_delete(tmp_path: Path) -> None:
    """A misconfigured canonical store nested in the legacy tree cannot be deleted by migration."""
    workspace = tmp_path / "workspace"
    legacy = workspace / "data" / "artifacts" / "shell_backups"
    legacy.mkdir(parents=True)
    sentinel = legacy / "test_must_survive.py"
    sentinel.write_text("secret", encoding="utf-8")

    result = ShellTool(
        workspace,
        options=ShellToolOptions(artifact_backup_root=legacy / "canonical"),
    ).execute({"command": "printf started > command-started.txt"})

    assert result.ok is False
    assert result.error_code == "ARTIFACT_BACKUP_FAILED"
    assert result.effect_outcome == "not_started"
    assert sentinel.read_text(encoding="utf-8") == "secret"
    assert not (workspace / "command-started.txt").exists()


def test_shell_ready_file_group_does_not_block_foreground_commands(tmp_path: Path) -> None:
    """The legacy single-file protector preserves prior skip semantics for typed file groups."""
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactGroupRegistration,
        register_artifact_group,
    )

    workspace = tmp_path / "workspace"
    output = workspace / "output"
    output.mkdir(parents=True)
    member = output / "one.txt"
    member.write_text("one", encoding="utf-8")
    register_artifact_group(
        ArtifactGroupRegistration(
            workspace_root=workspace,
            paths=[member],
            artifact_id="group-one",
            status="ready",
        )
    )

    result = ShellTool(workspace).execute({"command": "true"})

    assert result.ok is True
    assert result.error_code == ""


def test_shell_tool_model_spec_has_run_command(shell_tool: ShellTool) -> None:
    """Test that ShellTool model spec has correct name."""
    assert shell_tool.model_spec.name == "run_command"
    assert shell_tool.model_spec.category == "shell"
    assert "command" in shell_tool.model_spec.parameter_descriptions
    assert "timeout" in shell_tool.model_spec.parameter_descriptions
    assert "working_dir" in shell_tool.model_spec.parameter_descriptions


# LLM: Build real registered bytes under the test root; callers exercise the production journal.
# 函数用途: 创建多产物工作区，供性能与恢复测试共享。
def _shell_perf_case(tmp_path: Path, count: int = 4):
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for index in range(count):
        path = workspace / f"report-{index}.custom"
        path.write_text(f"before-{index}", encoding="utf-8")
        register_artifact(ArtifactRegistration(
            workspace_root=workspace, path=path, artifact_id=f"artifact-{index}",
            kind="custom", status="ready",
        ))
    return workspace, tmp_path / "backups"


# LLM: The no-op hot path must perform one ledger scan and two durable item transitions, independent of N.
# 函数用途: 验证未变化产物不写校验副本，且账本读取和清单写入次数固定。
@pytest.mark.parametrize("count", [1, 8, 32])
def test_shell_perf_unchanged_batch_io(tmp_path: Path, count: int) -> None:
    from agent_py_agent.agent.artifacts import registry
    from agent_py_agent.agent.artifacts import shell_protection as protection

    workspace, store = _shell_perf_case(tmp_path, count)
    snapshots = protection.snapshot_ready_artifacts(workspace, store)
    key = protection.mark_shell_artifact_operation_executing(store, snapshots)
    protection.release_shell_artifact_operation(key)
    with (
        patch.object(registry, "read_text_beneath", wraps=registry.read_text_beneath) as reads,
        patch.object(protection, "write_operation_manifest", wraps=protection.write_operation_manifest) as writes,
        patch.object(protection, "_write_validation_copy", wraps=protection._write_validation_copy) as copies,
    ):
        summary = protection.reconcile_shell_artifact_operation_items(
            workspace, snapshots, backup_store_root=store,
        )
    assert len(summary["unchanged"]) == count
    assert not summary["changed"] and not summary["cleanup_deferred"]
    assert not list(store.rglob("*.blob"))
    assert copies.call_count == 0
    assert reads.call_count == 1
    assert writes.call_count == 2
    manifest = protection.read_operation_manifest(store, key)
    assert {item["state"] for item in manifest["items"]} == {"unchanged"}


# LLM: Each blob still fsyncs its bytes; only repeated directory initialization and directory sync are coalesced.
# 函数用途: 验证批量前像只初始化三层目录一次，并在发布清单前统一同步。
def test_shell_perf_snapshot_directory_work_is_constant(tmp_path: Path) -> None:
    from agent_py_agent.agent.artifacts import shell_protection as protection

    workspace, store = _shell_perf_case(tmp_path)
    with (
        patch.object(protection, "_chmod_private", wraps=protection._chmod_private) as chmod,
        patch.object(protection, "_fsync_directory", wraps=protection._fsync_directory) as sync,
    ):
        snapshots = protection.snapshot_ready_artifacts(workspace, store)
    assert len(snapshots) == 4
    assert sync.call_count == 1
    assert sum(call.kwargs["directory"] for call in chmod.call_args_list) == 3
    for snapshot in snapshots:
        backup = protection.resolve_shell_artifact_backup(store, snapshot.backup_ref)
        assert hashlib.sha256(backup.read_bytes()).hexdigest() == snapshot.sha256
        if os.name == "posix":
            assert backup.stat().st_mode & 0o777 == 0o600
    assert protection.read_operation_manifest(store, snapshots[0].operation_key)["state"] == "prepared"


# LLM: Empty and unchanged postimages need no validator copy; changed nonempty bytes still use the real validator.
# 函数用途: 逐类核对哈希快路与深度校验分路，不牺牲变更登记的哈希和大小。
@pytest.mark.parametrize("content,status,copies", [("before-0", "unchanged", 0), ("", "invalid_after_shell", 0), ("new", "changed_ready", 1)])
def test_shell_perf_postcheck_copy_only_for_changed_nonempty(tmp_path, content, status, copies):
    from agent_py_agent.agent.artifacts import shell_protection as protection

    workspace, store = _shell_perf_case(tmp_path, 1)
    snapshot = protection.snapshot_ready_artifacts(workspace, store)[0]
    path = Path(snapshot.path)
    path.write_text(content, encoding="utf-8")
    with patch.object(protection, "_write_validation_copy", wraps=protection._write_validation_copy) as copy:
        observation = protection._post_shell_status(path, workspace, snapshot, (workspace,), store)
    assert observation.change_status == status
    assert observation.sha256 == hashlib.sha256(content.encode()).hexdigest()
    assert observation.size_bytes == len(content.encode())
    assert copy.call_count == copies
    assert not list(store.rglob(".postcheck-*.blob"))


# LLM: Historical matches require exact operation/ref pairs and a single no-follow read, not latest-row lookup.
# 函数用途: 核对批量查询的历史命中、引用隔离、缺失项与读取次数。
def test_shell_perf_registry_batch_matches_history_once(tmp_path: Path) -> None:
    from agent_py_agent.agent.artifacts import registry

    query = getattr(registry, "artifact_operation_recorded_ids", None)
    assert callable(query)
    path = registry.registry_path(tmp_path)
    path.parent.mkdir(parents=True)
    rows = [
        {"artifact_id": "a", "metadata": {"protection_operation_id": "op", "backup_ref": "ref-a"}},
        {"artifact_id": "a", "metadata": {}},
        {"artifact_id": "b", "metadata": {"protection_operation_id": "other", "backup_ref": "ref-b"}},
        {"artifact_id": "c", "metadata": {"protection_operation_id": "op", "backup_ref": "wrong"}},
        {"artifact_id": "d", "metadata": {"protection_operation_id": "op", "backup_ref": "ref-d"}},
    ]
    path.write_text("\n" + "\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    with patch.object(registry, "read_text_beneath", wraps=registry.read_text_beneath) as reads:
        assert query(tmp_path, operation_key="op", expected_backup_refs={
            "a": "ref-a", "b": "ref-b", "c": "ref-c", "d": "ref-d", "missing": "ref-m",
        }) == {"a", "d"}
    assert reads.call_count == 1


# LLM: Registry corruption must fail closed even when an earlier row matched; no-follow failures remain errors.
# 函数用途: 防止批量查询把坏账本当作未落账而触发重复追加或错误清理。
@pytest.mark.parametrize("tail", ["{broken", "[]", "null"])
def test_shell_perf_registry_batch_rejects_corruption(tmp_path: Path, tail: str) -> None:
    from agent_py_agent.agent.artifacts import registry

    query = getattr(registry, "artifact_operation_recorded_ids", None)
    assert callable(query)
    path = registry.registry_path(tmp_path)
    path.parent.mkdir(parents=True)
    row = {"artifact_id": "a", "metadata": {"protection_operation_id": "op", "backup_ref": "ref"}}
    path.write_text(json.dumps(row) + "\n" + tail, encoding="utf-8")
    with pytest.raises(OSError, match="artifact registry contains"):
        query(tmp_path, operation_key="op", expected_backup_refs={"a": "ref"})


# LLM: Release only the process marker, keeping the real executing journal for restart tests.
# 函数用途: 建立可供重启复核的真实前像和执行阶段清单。
def _shell_perf_executing(tmp_path: Path):
    from agent_py_agent.agent.artifacts import shell_protection as protection

    workspace, store = _shell_perf_case(tmp_path)
    snapshots = protection.snapshot_ready_artifacts(workspace, store)
    key = protection.mark_shell_artifact_operation_executing(store, snapshots)
    protection.release_shell_artifact_operation(key)
    return workspace, store, snapshots, key


# LLM: Inject loss around both durable batch barriers and midway through deletion; replay must not reclassify observed bytes.
# 函数用途: 验证批量阶段提交前后及清理中断的恢复语义。
@pytest.mark.parametrize("phase", ["observe_before", "observe_after", "cleanup_mid", "finish_before"])
def test_shell_perf_batch_crash_replay(tmp_path, monkeypatch, phase):
    from agent_py_agent.agent.artifacts import shell_protection as protection

    workspace, store, snapshots, key = _shell_perf_executing(tmp_path)
    real_write = protection.write_operation_manifest
    real_discard = protection._discard_backup
    writes = 0

    # LLM: Fail at the selected persistence boundary without falsifying the on-disk manifest.
    # 函数用途: 模拟阶段写入前失败或写入成功后回执丢失。
    def fail_write(root, operation_key, manifest):
        nonlocal writes
        writes += 1
        if (phase == "observe_before" and writes == 1) or (phase == "finish_before" and writes == 2):
            raise RuntimeError("crash")
        real_write(root, operation_key, manifest)
        if phase == "observe_after" and writes == 1:
            raise RuntimeError("crash")

    # LLM: Every deletion must be preceded by a durable observation of every unchanged item.
    # 函数用途: 删除前读回清单核对屏障，并可在首个 blob 删除后模拟崩溃。
    def checked_discard(root, ref):
        durable = protection.read_operation_manifest(root, key)
        assert {item["state"] for item in durable["items"]} == {"unchanged_observed"}
        real_discard(root, ref)
        if phase == "cleanup_mid":
            raise RuntimeError("crash")

    with monkeypatch.context() as faults:
        faults.setattr(protection, "write_operation_manifest", fail_write)
        faults.setattr(protection, "_discard_backup", checked_discard)
        with pytest.raises(RuntimeError, match="crash"):
            protection.reconcile_shell_artifact_operation_items(workspace, snapshots, backup_store_root=store)
    durable = protection.read_operation_manifest(store, key)
    expected_state = "prepared" if phase == "observe_before" else "unchanged_observed"
    assert {item["state"] for item in durable["items"]} == {expected_state}
    assert len(list(store.rglob("*.blob"))) == {"observe_before": 4, "observe_after": 4, "cleanup_mid": 3, "finish_before": 0}[phase]
    with patch.object(protection, "_post_shell_status", wraps=protection._post_shell_status) as observations:
        summary = protection.reconcile_shell_artifact_operation_items(workspace, snapshots, backup_store_root=store)
    assert observations.call_count == (4 if phase == "observe_before" else 0)
    assert len(summary["unchanged"]) == 4
    assert not list(store.rglob("*.blob"))
    assert {item["state"] for item in protection.read_operation_manifest(store, key)["items"]} == {"unchanged"}


# LLM: Cleanup failures are durable debt, not lost observations or new registry appends.
# 函数用途: 验证部分 blob 删除失败时保存债务，重试只清理失败项。
def test_shell_perf_batch_cleanup_debt(tmp_path: Path) -> None:
    from agent_py_agent.agent.artifacts import shell_protection as protection

    workspace, store, snapshots, key = _shell_perf_executing(tmp_path)
    real_discard = protection._discard_backup
    failed_ref = snapshots[1].backup_ref

    # LLM: Ref-specific injected failure leaves the actual preimage untouched.
    # 函数用途: 模拟一份备份忙碌，其他备份正常清理。
    def busy(root, ref):
        if ref == failed_ref:
            raise OSError("busy")
        real_discard(root, ref)

    with patch.object(protection, "_discard_backup", side_effect=busy):
        summary = protection.reconcile_shell_artifact_operation_items(workspace, snapshots, backup_store_root=store)
    assert summary["cleanup_deferred"] == [failed_ref]
    assert len(list(store.rglob("*.blob"))) == 1
    assert protection.read_operation_manifest(store, key)["items"][1]["state"] == "cleanup_pending"
    with (
        patch.object(protection, "_post_shell_status", side_effect=AssertionError("must not reobserve")),
        patch.object(protection, "write_operation_manifest", wraps=protection.write_operation_manifest) as writes,
    ):
        summary = protection.reconcile_shell_artifact_operation_items(workspace, snapshots, backup_store_root=store)
    assert len(summary["unchanged"]) == 4 and not summary["cleanup_deferred"]
    assert writes.call_count == 1
    assert not list(store.rglob("*.blob"))


# LLM: A durable changed-row append with a lost acknowledgment must not be repeated after restart.
# 函数用途: 混合批次中核对变更落账回执丢失，确保恢复不重复追加或删除变更前像。
def test_shell_perf_mixed_batch_lost_append_ack(tmp_path: Path) -> None:
    from agent_py_agent.agent.artifacts import registry
    from agent_py_agent.agent.artifacts import shell_protection as protection

    workspace, store, snapshots, key = _shell_perf_executing(tmp_path)
    Path(snapshots[1].path).write_text("changed", encoding="utf-8")
    real_append = protection.register_observed_artifact

    # LLM: Persist the real registry row before losing its acknowledgment.
    # 函数用途: 模拟账本写成但调用方收到异常。
    def lost_ack(*args, **kwargs):
        real_append(*args, **kwargs)
        raise OSError("lost acknowledgment")

    with patch.object(protection, "register_observed_artifact", side_effect=lost_ack):
        with pytest.raises(OSError, match="lost acknowledgment"):
            protection.reconcile_shell_artifact_operation_items(workspace, snapshots, backup_store_root=store)
    before = registry.registry_path(workspace).read_bytes()
    assert len(list(store.rglob("*.blob"))) == 4
    with patch.object(protection, "register_observed_artifact", side_effect=AssertionError("duplicate append")):
        summary = protection.reconcile_shell_artifact_operation_items(workspace, snapshots, backup_store_root=store)
    assert registry.registry_path(workspace).read_bytes() == before
    assert len(summary["changed"]) == 1 and len(summary["unchanged"]) == 3
    assert summary["changed"][0]["change_status"] == "recovered_committed_record"
    assert len(list(store.rglob("*.blob"))) == 1
    assert protection.resolve_shell_artifact_backup(store, snapshots[1].backup_ref).read_text() == "before-1"
    assert protection.read_operation_manifest(store, key)["items"][1]["state"] == "registered"


# LLM: The fast hash and second copy are separate observations; registry facts must describe validated copy bytes.
# 函数用途: 验证两次读取之间发生修改时不混用第一次哈希与第二次副本。
@pytest.mark.parametrize("second", ["another-version", "", "before-0"])
def test_shell_perf_copy_identity_after_hash(tmp_path, second):
    from agent_py_agent.agent.artifacts import shell_protection as protection

    workspace, store = _shell_perf_case(tmp_path, 1)
    snapshot = protection.snapshot_ready_artifacts(workspace, store)[0]
    path = Path(snapshot.path)
    path.write_text("first-change", encoding="utf-8")
    real_copy = protection._write_validation_copy

    # LLM: Mutate before the copy starts; the copier still enforces stability during its read.
    # 函数用途: 模拟哈希之后、校验副本读取之前发生的完整版本替换。
    def change_then_copy(*args, **kwargs):
        path.write_text(second, encoding="utf-8")
        return real_copy(*args, **kwargs)

    with patch.object(protection, "_write_validation_copy", side_effect=change_then_copy):
        observation = protection._post_shell_status(path, workspace, snapshot, (workspace,), store)
    assert observation.sha256 == hashlib.sha256(second.encode()).hexdigest()
    assert observation.size_bytes == len(second.encode())
    assert observation.change_status == {"": "invalid_after_shell", "before-0": "unchanged"}.get(second, "changed_ready")
    assert not list(store.rglob(".postcheck-*.blob"))


# LLM: Directory-sync failure must happen before manifest publication and before command launch.
# 函数用途: 验证批量同步失败时清理所有已复制前像，不发布可执行清单。
def test_shell_perf_snapshot_sync_failure_rolls_back(tmp_path: Path) -> None:
    from agent_py_agent.agent.artifacts import shell_protection as protection

    workspace, store = _shell_perf_case(tmp_path)
    with (
        patch.object(protection, "_fsync_directory", side_effect=OSError("sync failed")),
        patch.object(protection, "_write_shell_operation_state", wraps=protection._write_shell_operation_state) as publish,
    ):
        with pytest.raises(OSError, match="sync failed"):
            protection.snapshot_ready_artifacts(workspace, store)
    assert publish.call_count == 0
    assert not list(store.rglob("*.blob")) and not list(store.rglob("operation.json"))


# LLM: Default standalone backups retain directory initialization and fsync, including migration callers.
# 函数用途: 核对新增可选参数没有改变单份备份的默认耐久行为。
def test_shell_perf_standalone_backup_default_sync(tmp_path: Path) -> None:
    from agent_py_agent.agent.artifacts import shell_protection as protection

    workspace, store = _shell_perf_case(tmp_path, 1)
    opened = protection._open_regular_source(workspace / "report-0.custom", (workspace,))
    try:
        with patch.object(protection, "_fsync_directory", wraps=protection._fsync_directory) as sync:
            backup, digest, size = protection._write_atomic_backup(opened, store / "v1" / "op", artifact_id="a")
    finally:
        opened.close()
    assert sync.call_count == 1
    assert backup.read_bytes() == b"before-0"
    assert digest == hashlib.sha256(b"before-0").hexdigest() and size == 8


# LLM: No expected refs require no read, while a missing safe registry is empty and linked registry paths fail closed.
# 函数用途: 核对批量查询空输入、缺账本和符号链接拒绝边界。
def test_shell_perf_registry_empty_missing_and_symlink(tmp_path: Path) -> None:
    from agent_py_agent.agent.artifacts import registry

    with patch.object(registry, "read_text_beneath", wraps=registry.read_text_beneath) as reads:
        assert registry.artifact_operation_recorded_ids(tmp_path, operation_key="op", expected_backup_refs={}) == set()
        assert reads.call_count == 0
        assert registry.artifact_operation_recorded_ids(tmp_path, operation_key="op", expected_backup_refs={"a": "r"}) == set()
        assert reads.call_count == 1
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "data").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError, match="cannot be scanned safely"):
        registry.artifact_operation_recorded_ids(tmp_path, operation_key="op", expected_backup_refs={"a": "r"})
