from __future__ import annotations

"""LLM: implements shell command execution tool with security controls.

给人看的解释：
这个文件实现 ShellTool，让 agent 能跑终端命令。
安全控制：危险命令黑名单、基本校验、subprocess 超时处理。
"""

import subprocess
import os
import re
from pathlib import Path
from typing import Any

from .models import BaseTool, ToolExecutionResult, ToolSpec

# 命令最大长度限制
_MAX_COMMAND_CHARS = 2000

# 危险命令黑名单（不区分大小写匹配命令名）
_DANGEROUS_COMMANDS = [
    r"^rm\s+-rf\s+/",
    r"^mkfs",
    r"^dd\s+.*of=",
    r"^shutdown",
    r"^reboot",
    r"^init\s+6",
    r"^init\s+0",
    r"^halt",
    r"^poweroff",
    r"^telinit",
    r":\(\;\)\s*;",
    r"rm\s+-rf\s+\$\{",
]

# 编译正则提高匹配效率
_DANGEROUS_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _DANGEROUS_COMMANDS]


def _is_dangerous_command(command: str) -> bool:
    """检查命令是否属于危险命令。"""
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            return True
    return False


def _validate_command(command: str) -> str:
    """校验命令是否合法。"""
    if not command:
        raise ValueError("command 不能为空")
    text = command.strip()
    if not text:
        raise ValueError("command 不能为空或仅包含空白字符")
    if len(text) > _MAX_COMMAND_CHARS:
        raise ValueError(f"command 过长，最多 {_MAX_COMMAND_CHARS} 个字符")
    return text


class ShellTool(BaseTool):
    """执行 shell 命令。

    使用 subprocess.run 执行用户传入的命令，返回 stdout、stderr 和返回码。
    安全控制：危险命令黑名单、命令长度校验、超时处理。
    """

    def __init__(self, workspace_root: Path, default_timeout: int = 30):
        self.workspace_root = workspace_root.resolve()
        self.default_timeout = default_timeout
        self.spec = ToolSpec(
            name="run_command",
            category="shell",
            description="在工作区执行一条 shell 命令，适合编译、运行脚本、查进程等。",
            use_cases=[
                "需要运行项目构建脚本（如 make、npm run）",
                "需要查进程、端口、网络状态等系统信息",
                "需要执行一次性脚本或命令行工具",
            ],
            avoid_when=[
                "只需要读写文件时，用 read_file / write_file 工具",
                "需要交互式输入或终端多步操作时不适合",
                "涉及文件改动优先用 write_file，避免直接 echo 重定向",
            ],
            keywords=["shell", "命令", "终端", "bash", "执行", "运行", "cmd", "command", "script"],
            parameters={
                "command": "要执行的 shell 命令字符串",
                "timeout": f"超时秒数，默认 {default_timeout}",
                "working_dir": "执行目录，默认使用工作区根目录",
            },
            parameter_details={
                "command": "必填。要执行的完整命令字符串，如 'ls -la' 或 'python build.py'。",
                "timeout": f"可选，默认 {default_timeout} 秒。命令执行超过此时间会被强制终止。",
                "working_dir": "可选，执行命令时的工作目录。不传时默认使用工作区根目录。",
            },
            examples=[
                '{"tool": "run_command", "command": "ls -la"}',
                '{"tool": "run_command", "command": "python --version", "working_dir": "."}',
                '{"tool": "run_command", "command": "make build", "timeout": 60}',
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        """执行 shell 命令。"""
        try:
            command = _validate_command(str(params.get("command", "")))
        except ValueError as exc:
            return ToolExecutionResult(self.spec.name, False, str(exc))

        if _is_dangerous_command(command):
            return ToolExecutionResult(
                self.spec.name,
                False,
                f"危险命令被系统拒绝: {command[:50]}...",
            )

        timeout = self.default_timeout
        raw_timeout = params.get("timeout")
        if raw_timeout is not None:
            try:
                timeout = int(raw_timeout)
                if timeout <= 0:
                    timeout = self.default_timeout
            except (ValueError, TypeError):
                timeout = self.default_timeout

        working_dir = str(params.get("working_dir", "")).strip()
        if not working_dir:
            working_dir = str(self.workspace_root)
        target = Path(working_dir).expanduser()
        if not target.is_dir():
            target = self.workspace_root

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(target),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            stdout = result.stdout if result.stdout else ""
            stderr = result.stderr if result.stderr else ""
            output = (
                f"return_code={result.returncode}\n"
                f"stdout={stdout}\n"
                f"stderr={stderr}"
            )
            return ToolExecutionResult(self.spec.name, True, output)
        except subprocess.TimeoutExpired:
            return ToolExecutionResult(
                self.spec.name,
                False,
                f"命令执行超时（{timeout}秒）: {command[:100]}...",
            )
        except OSError as exc:
            return ToolExecutionResult(
                self.spec.name,
                False,
                f"命令执行失败: {exc}",
            )
