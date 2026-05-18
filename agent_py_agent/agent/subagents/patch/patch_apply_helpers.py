# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

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

if TYPE_CHECKING:
    from ..models import SubAgentTask


_PATCH_TEST_ALLOWED_PREFIXES = {"python", "python3", "pytest"}
_PATCH_TEST_TIMEOUT_SECONDS = 120
_PATCH_TEST_BLOCKED_CHARS = {"&", "|", ">", "<", "`"}


# LLM: _patch_test_argv 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 处理补丁testargv相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持补丁文件、预演结果和应用报告上的返回值和副作用边界稳定。
def _patch_test_argv(command: str) -> list[str]:
    """Build the argv used for a patch-apply test command."""

    argv = shlex.split(command)
    # LLM: Windows 上 python3.exe 可能是商店占位程序，这里优先复用当前解释器。
    # interpreter for patch tests while leaving macOS/Linux python3 commands intact.
    if os.name == "nt" and argv and argv[0] == "python3":
        argv[0] = sys.executable
    return argv


# LLM: validate_patch_test_command 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 校验补丁testcommand需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def validate_patch_test_command(command: str) -> str:
    """Validate test command for security risks.

    Returns empty string if valid, or an error message if blocked.
    """

    stripped = command.strip()
    if not stripped:
        return "空测试命令不能进入 patch apply。"
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


# LLM: run_patch_apply_tests 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 推进补丁应用tests的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响补丁文件、预演结果和应用报告，需保持重试、超时和状态迁移语义。
def run_patch_apply_tests(
    commands: list[str],
    workspace_root: Path,
) -> list[dict]:
    """Run patch apply test commands with timeout and output capture."""

    results = []
    for command in commands:
        argv = _patch_test_argv(command)
        try:
            completed = subprocess.run(
                argv,
                cwd=workspace_root,
                capture_output=True,
                text=True,
                timeout=_PATCH_TEST_TIMEOUT_SECONDS,
                check=False,
            )
            ok = completed.returncode == 0
            results.append(
                {
                    "command": command,
                    "ok": ok,
                    "returncode": completed.returncode,
                    "stdout": completed.stdout[-4000:],
                    "stderr": completed.stderr[-4000:],
                }
            )
        except Exception as exc:
            results.append(
                {
                    "command": command,
                    "ok": False,
                    "returncode": -1,
                    "stdout": "",
                    "stderr": str(exc),
                }
            )
    return results
