
"""Patch apply test validation and execution helpers.

Human version:
这个模块处理 patch apply 相关的测试命令验证和执行。
不涉及文件写入或边界检查。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ...contracts.gates.command_policy import command_source_too_large

if TYPE_CHECKING:
    from ..models import SubAgentTask


_PATCH_TEST_ALLOWED_PREFIXES = {"python", "python3", "pytest"}
# patch 测试命令的超时秒数；超时即判测试失败，防止测试命令挂死。
_PATCH_TEST_TIMEOUT_SECONDS = 120
_PATCH_TEST_BLOCKED_CHARS = {"&", "|", ">", "<", "`"}


# LLM: 防御性闸门：正常流程由 validate_patch_test_command 先行拦截，这里再挡一次，超长时
#   返回空列表，交由调用方的空 argv 分支处理，绝不把超长文本送进 shlex。
# 函数用途: 为 patch 测试命令构建 argv（含 Windows 的 python3 解释器修正）。
def _patch_test_argv(command: str) -> list[str]:
    """Build the argv used for a patch-apply test command."""

    if command_source_too_large(command):
        return []
    argv = shlex.split(command)
    # interpreter for patch tests while leaving macOS/Linux python3 commands intact.
    if os.name == "nt" and argv and argv[0] == "python3":
        argv[0] = sys.executable
    return argv


# LLM: 测试命令是模型文本；在 shlex 之前先过共享长度闸门（超长文本交给下游 shlex 会卡线程），
#   超长按与空命令同级的"阻止"返回，调用方只读返回值字符串，不进入执行链。
# 函数用途: 校验 patch 测试命令；返回空串表示可执行，否则返回阻止原因。
def validate_patch_test_command(command: str) -> str:
    """Validate test command for security risks.

    Returns empty string if valid, or an error message if blocked.
    """

    stripped = command.strip()
    if not stripped:
        return "空测试命令不能进入 patch apply。"
    if command_source_too_large(stripped):
        return "测试命令过长，已阻止。"
    if any(char in stripped for char in _PATCH_TEST_BLOCKED_CHARS):
        return f"测试命令包含高风险 shell 字符，已阻止: {command}"
    try:
        argv = shlex.split(stripped)
    except ValueError as exc:
        return f"测试命令解析失败，已阻止: {exc}"
    if not argv:
        return "空测试命令不能进入 patch apply。"
    if argv[0] not in _PATCH_TEST_ALLOWED_PREFIXES:
        return f"测试命令不在 allowlist 内，已阻止: {command}"
    return ""


# 函数用途: 记录一条被跳过的 patch 测试命令（命令过长或为空，未执行）。
def _skipped_result(command: str) -> dict:
    return {
        "command": command,
        "ok": False,
        "returncode": -1,
        "stdout": "",
        "stderr": "测试命令过长或为空，已跳过。",
    }


# 函数用途: 记录一条已执行完成的 patch 测试命令结果（stdout/stderr 截尾 4000 字符）。
def _completed_result(command: str, completed) -> dict:
    return {
        "command": command,
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }


# 函数用途: 记录一条执行抛错的 patch 测试命令结果。
def _failed_result(command: str, exc: Exception) -> dict:
    return {
        "command": command,
        "ok": False,
        "returncode": -1,
        "stdout": "",
        "stderr": str(exc),
    }


# LLM: 逐条跑 patch 测试命令：超长/空命令跳过（不执行），正常命令限时执行并截尾输出；
#   三种结果各自收敛到小函数，主循环只保留控制流。
# 函数用途: 顺序执行 patch 测试命令列表，返回每条命令的结果记录。
def run_patch_apply_tests(
    commands: list[str],
    workspace_root: Path,
) -> list[dict]:
    """Run patch apply test commands with timeout and output capture."""

    results = []
    for command in commands:
        argv = _patch_test_argv(command)
        if not argv:
            results.append(_skipped_result(command))
            continue
        try:
            completed = subprocess.run(
                argv,
                cwd=workspace_root,
                capture_output=True,
                text=True,
                timeout=_PATCH_TEST_TIMEOUT_SECONDS,
                check=False,
            )
            results.append(_completed_result(command, completed))
        except Exception as exc:
            results.append(_failed_result(command, exc))
    return results
