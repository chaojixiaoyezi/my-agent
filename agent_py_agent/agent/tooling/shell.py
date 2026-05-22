
# LLM: 命令安全策略和输出格式会影响自动化执行，放宽前需非常谨慎。
# 模块用途: 受限 shell 工具，负责危险命令拦截、超时和工作目录解析。

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .models import BaseTool, ToolExecutionResult, ToolSpec

_MAX_COMMAND_CHARS = 2000
_DEFAULT_MAX_OUTPUT_CHARS = 12_000
_TOOL_DEADLINE_UNIX_ENV = "MY_AGENT_TOOL_DEADLINE_UNIX"
_TOOL_DEADLINE_MARGIN_SECONDS_ENV = "MY_AGENT_TOOL_DEADLINE_MARGIN_SECONDS"

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


# LLM: _timeout_from_params applies structured run deadlines before launching shell work.
# 函数用途: 从工具参数读取超时，并按外层任务 deadline 自动收紧，避免单个命令吃完整个任务预算。
def _timeout_from_params(params: dict[str, Any], default_timeout: int) -> int:
    raw_timeout = params.get("timeout")
    if raw_timeout is None:
        timeout = default_timeout
    else:
        try:
            timeout = int(raw_timeout)
        except (ValueError, TypeError):
            timeout = default_timeout
    return _apply_tool_deadline(timeout if timeout > 0 else default_timeout)


# LLM: _apply_tool_deadline mirrors 会话运行时 exec expiration at the tool boundary.
# 函数用途: 根据 MY_AGENT_TOOL_DEADLINE_UNIX 和安全余量收紧命令超时；0 表示不应再启动命令。
def _apply_tool_deadline(timeout: int) -> int:
    deadline = _float_env(_TOOL_DEADLINE_UNIX_ENV)
    if deadline <= 0:
        return timeout
    remaining = deadline - time.time() - _tool_deadline_margin_seconds()
    if remaining <= 0:
        return 0
    return min(timeout, max(1, int(remaining)))


# LLM: _tool_deadline_margin_seconds keeps shell completion inside the parent task envelope.
# 函数用途: 读取工具 deadline 安全余量；配置异常时使用保守默认值。
def _tool_deadline_margin_seconds() -> float:
    margin = _float_env(_TOOL_DEADLINE_MARGIN_SECONDS_ENV)
    return margin if margin >= 0 else 10.0


# LLM: _float_env parses runtime deadline env vars without treating prose as facts.
# 函数用途: 将结构化环境变量转为 float，缺失或非法时返回 0。
def _float_env(name: str) -> float:
    try:
        return float(os.environ.get(name, "0") or 0)
    except (ValueError, TypeError):
        return 0.0


# LLM: _working_dir_from_params 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 working_dir_from_params 步骤，并保持调用方依赖的数据形状。
def _working_dir_from_params(params: dict[str, Any], workspace_root: Path) -> Path:
    working_dir = str(params.get("working_dir", "")).strip()
    target = Path(working_dir).expanduser() if working_dir else workspace_root
    return target if target.is_dir() else workspace_root


# LLM: _bounded_output preserves enough command output for diagnosis without flooding the live prompt.
# 函数用途: 按配置截断单个 stdout/stderr 字段，并返回是否截断，避免大日志撑爆上下文。
def _bounded_output(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0:
        return "", bool(text)
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


# LLM: _format_process_result keeps shell results machine-readable so parent/subagents can reason from flags.
# 函数用途: 把命令结果转成包含总长度、预览长度和截断标记的稳定文本格式。
def _format_process_result(result: subprocess.CompletedProcess[str], max_output_chars: int) -> str:
    stdout = result.stdout if result.stdout else ""
    stderr = result.stderr if result.stderr else ""
    stdout_preview, stdout_truncated = _bounded_output(stdout, max_output_chars)
    stderr_preview, stderr_truncated = _bounded_output(stderr, max_output_chars)
    return (
        f"return_code={result.returncode}\n"
        f"stdout_chars={len(stdout)} stdout_preview_chars={len(stdout_preview)} "
        f"stdout_truncated={stdout_truncated}\n"
        f"stdout={stdout_preview}\n"
        f"stderr_chars={len(stderr)} stderr_preview_chars={len(stderr_preview)} "
        f"stderr_truncated={stderr_truncated}\n"
        f"stderr={stderr_preview}"
    )


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
    def __init__(
        self,
        workspace_root: Path,
        default_timeout: int = 30,
        max_output_chars: int = _DEFAULT_MAX_OUTPUT_CHARS,
    ):
        self.workspace_root = workspace_root.resolve()
        self.default_timeout = default_timeout
        self.max_output_chars = max(0, int(max_output_chars))
        self.spec = ToolSpec(
            name="run_command",
            category="shell",
            effect="mutating",
            requires_idempotency=True,
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
                "output": (
                    "Stdout/stderr are returned as bounded previews with *_chars and *_truncated flags; "
                    f"each stream preview defaults to {self.max_output_chars} chars."
                ),
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
        if timeout <= 0:
            return ToolExecutionResult(
                self.spec.name,
                False,
                "TOOL_DEADLINE_EXCEEDED: 外层任务剩余时间不足，系统没有启动新的 shell 命令。",
                error_code="TOOL_TIMEOUT",
            )
        target = _working_dir_from_params(params, self.workspace_root)
        try:
            result = self._run_command(command, target, timeout)
            return ToolExecutionResult(self.spec.name, True, _format_process_result(result, self.max_output_chars))
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
