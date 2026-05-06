
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


def _is_dangerous_command(command: str) -> bool:
    return any(pattern.search(command) for pattern in _DANGEROUS_PATTERNS)


def _validate_command(command: str) -> str:
    if not command:
        raise ValueError("command 不能为空")
    text = command.strip()
    if not text:
        raise ValueError("command 不能为空或仅包含空白字符")
    if len(text) > _MAX_COMMAND_CHARS:
        raise ValueError(f"command 过长，最多 {_MAX_COMMAND_CHARS} 个字符")
    return text


def _timeout_from_params(params: dict[str, Any], default_timeout: int) -> int:
    raw_timeout = params.get("timeout")
    if raw_timeout is None:
        return default_timeout
    try:
        timeout = int(raw_timeout)
    except (ValueError, TypeError):
        return default_timeout
    return timeout if timeout > 0 else default_timeout


def _working_dir_from_params(params: dict[str, Any], workspace_root: Path) -> Path:
    working_dir = str(params.get("working_dir", "")).strip()
    target = Path(working_dir).expanduser() if working_dir else workspace_root
    return target if target.is_dir() else workspace_root


def _format_process_result(result: subprocess.CompletedProcess[str]) -> str:
    stdout = result.stdout if result.stdout else ""
    stderr = result.stderr if result.stderr else ""
    return f"return_code={result.returncode}\nstdout={stdout}\nstderr={stderr}"


def _subprocess_text_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


class ShellTool(BaseTool):

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

    def _parse_command(self, params: dict[str, Any]) -> str | ToolExecutionResult:
        try:
            return _validate_command(str(params.get("command", "")))
        except ValueError as exc:
            return ToolExecutionResult(self.spec.name, False, str(exc))

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
