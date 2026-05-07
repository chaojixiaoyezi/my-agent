
# LLM: 命令安全策略和输出格式会影响自动化执行，放宽前需非常谨慎。
# 模块用途: 受限 shell 工具，负责危险命令拦截、超时和工作目录解析。

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .models import BaseTool, ToolExecutionResult, ToolSpec

_MAX_COMMAND_CHARS = 2000

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

_DANGEROUS_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _DANGEROUS_COMMANDS]


# LLM: _is_dangerous_command 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 判断 is_dangerous_command 是否满足安全或状态条件。
def _is_dangerous_command(command: str) -> bool:
    return any(pattern.search(command) for pattern in _DANGEROUS_PATTERNS)


# LLM: _validate_command 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 validate_command 步骤，并保持调用方依赖的数据形状。
def _validate_command(command: str) -> str:
    if not command:
        raise ValueError("command 不能为空")
    text = command.strip()
    if not text:
        raise ValueError("command 不能为空或仅包含空白字符")
    if len(text) > _MAX_COMMAND_CHARS:
        raise ValueError(f"command 过长，最多 {_MAX_COMMAND_CHARS} 个字符")
    return text


# LLM: _timeout_from_params 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 timeout_from_params 步骤，并保持调用方依赖的数据形状。
def _timeout_from_params(params: dict[str, Any], default_timeout: int) -> int:
    raw_timeout = params.get("timeout")
    if raw_timeout is None:
        return default_timeout
    try:
        timeout = int(raw_timeout)
    except (ValueError, TypeError):
        return default_timeout
    return timeout if timeout > 0 else default_timeout


# LLM: _working_dir_from_params 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 working_dir_from_params 步骤，并保持调用方依赖的数据形状。
def _working_dir_from_params(params: dict[str, Any], workspace_root: Path) -> Path:
    working_dir = str(params.get("working_dir", "")).strip()
    target = Path(working_dir).expanduser() if working_dir else workspace_root
    return target if target.is_dir() else workspace_root


# LLM: _format_process_result 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把 format_process_result 转成人或模型可读的展示文本。
def _format_process_result(result: subprocess.CompletedProcess[str]) -> str:
    stdout = result.stdout if result.stdout else ""
    stderr = result.stderr if result.stderr else ""
    return f"return_code={result.returncode}\nstdout={stdout}\nstderr={stderr}"


# LLM: _subprocess_text_env 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 subprocess_text_env 步骤，并保持调用方依赖的数据形状。
def _subprocess_text_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


# LLM: ShellTool 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: ShellTool 数据模型，集中保存 工具系统 的结构化状态。
class ShellTool(BaseTool):

    # LLM: ShellTool.__init__ 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 初始化 ShellTool 的依赖、配置和运行期字段。
    def __init__(self, workspace_root: Path, default_timeout: int = 30):
        self.workspace_root = workspace_root.resolve()
        self.default_timeout = default_timeout
        self.spec = ToolSpec(
            name="run_command",
            category="shell",
            description="Execute one shell command in the workspace.",
            use_cases=[
                "Run a project build script such as make or npm run.",
                "Inspect processes, ports, network state, or other system information.",
                "Execute a one-off script or command-line tool.",
            ],
            avoid_when=[
                "Use read_file / write_file when only file IO is needed.",
                "Avoid for interactive terminal workflows.",
                "Prefer write_file for file changes instead of shell redirection.",
            ],
            keywords=["shell", "command", "terminal", "bash", "cmd", "script"],
            parameters={
                "command": "Shell command string to execute.",
                "timeout": f"Timeout in seconds; default {default_timeout}.",
                "working_dir": "Execution directory; defaults to the workspace root.",
            },
            parameter_details={
                "command": "Required. Full command string, for example 'ls -la' or 'python build.py'.",
                "timeout": f"Optional. Defaults to {default_timeout} seconds.",
                "working_dir": "Optional. Directory where the command runs.",
            },
            examples=[
                '{"tool": "run_command", "command": "ls -la"}',
                '{"tool": "run_command", "command": "python --version", "working_dir": "."}',
                '{"tool": "run_command", "command": "make build", "timeout": 60}',
            ],
        )

    # LLM: ShellTool.execute 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 执行 ShellTool 的主流程并返回 ToolExecutionResult。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        command_result = self._parse_command(params)
        if isinstance(command_result, ToolExecutionResult):
            return command_result
        command = command_result
        if _is_dangerous_command(command):
            return ToolExecutionResult(self.spec.name, False, f"危险命令被系统拒绝: {command[:50]}...")

        timeout = _timeout_from_params(params, self.default_timeout)
        target = _working_dir_from_params(params, self.workspace_root)
        try:
            result = self._run_command(command, target, timeout)
            return ToolExecutionResult(self.spec.name, True, _format_process_result(result))
        except subprocess.TimeoutExpired:
            return ToolExecutionResult(self.spec.name, False, f"命令执行超时 timeout ({timeout}s): {command[:100]}...")
        except OSError as exc:
            return ToolExecutionResult(self.spec.name, False, f"命令执行失败: {exc}")

    # LLM: ShellTool._parse_command 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 解析 parse_command 数据结构。
    def _parse_command(self, params: dict[str, Any]) -> str | ToolExecutionResult:
        try:
            return _validate_command(str(params.get("command", "")))
        except ValueError as exc:
            return ToolExecutionResult(self.spec.name, False, str(exc))

    # LLM: ShellTool._run_command 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 工具系统 中的 run_command 步骤，并保持调用方依赖的数据形状。
    def _run_command(
        self,
        command: str,
        target: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        if os.name == "nt":
            return subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", command],
                cwd=str(target),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_subprocess_text_env(),
                timeout=timeout,
            )
        return subprocess.run(
            command,
            shell=True,
            cwd=str(target),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_subprocess_text_env(),
            timeout=timeout,
        )
