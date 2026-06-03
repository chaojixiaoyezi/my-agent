

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_py_agent.agent.artifacts.shell_protection import (
    reconcile_shell_artifacts,
    shell_artifact_protection_note,
    snapshot_ready_artifacts,
)
from agent_py_agent.agent.contracts.gates.command.policy import (
    evaluate_command_policy,
)
from agent_py_agent.agent.path_access_policy import PathAccessPolicy

from .models import BaseTool, ToolExecutionResult, ToolSpec
from .shell_delete_policy import DeleteAccessRequest, delete_target_access_error

_MAX_COMMAND_CHARS = 2000
_DEFAULT_MAX_OUTPUT_CHARS = 12_000
_DEFAULT_ACCESS_MODE = "workspace-write"
_ACCESS_MODES = frozenset({"restricted", "workspace-write", "full-access"})
_ACCESS_MODE_RANK = {"restricted": 0, "workspace-write": 1, "full-access": 2}
_TOOL_DEADLINE_UNIX_ENV = "MY_AGENT_TOOL_DEADLINE_UNIX"
_TOOL_DEADLINE_MARGIN_SECONDS_ENV = "MY_AGENT_TOOL_DEADLINE_MARGIN_SECONDS"

@dataclass(frozen=True)
class ShellToolOptions:
    workspace_roots: list[Path] | None = None
    path_access_mode: str = "normal"
    path_dangerous_roots: list[str] | None = None
    access_mode: str = _DEFAULT_ACCESS_MODE
    default_timeout: int = 30
    max_output_chars: int = _DEFAULT_MAX_OUTPUT_CHARS


def _is_dangerous_command(command: str) -> bool:
    return not evaluate_command_policy(command, allow_shell_operators=True).allowed


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
        timeout = default_timeout
    else:
        try:
            timeout = int(raw_timeout)
        except (ValueError, TypeError):
            timeout = default_timeout
    return _apply_tool_deadline(timeout if timeout > 0 else default_timeout)


def _apply_tool_deadline(timeout: int) -> int:
    deadline = _float_env(_TOOL_DEADLINE_UNIX_ENV)
    if deadline <= 0:
        return timeout
    remaining = deadline - time.time() - _tool_deadline_margin_seconds()
    if remaining <= 0:
        return 0
    return min(timeout, max(1, int(remaining)))


def _tool_deadline_margin_seconds() -> float:
    margin = _float_env(_TOOL_DEADLINE_MARGIN_SECONDS_ENV)
    return margin if margin >= 0 else 10.0


def _float_env(name: str) -> float:
    try:
        return float(os.environ.get(name, "0") or 0)
    except (ValueError, TypeError):
        return 0.0


def _normalize_access_mode(access_mode: str) -> str:
    mode = str(access_mode or "").strip().lower().replace("_", "-")
    return mode if mode in _ACCESS_MODES else _DEFAULT_ACCESS_MODE


def _effective_access_mode(configured: str, override: object = "") -> str:
    configured_mode = _normalize_access_mode(configured)
    override_text = str(override or "").strip()
    if not override_text:
        return configured_mode
    override_mode = _normalize_access_mode(override_text)
    if _ACCESS_MODE_RANK[override_mode] < _ACCESS_MODE_RANK[configured_mode]:
        return override_mode
    return configured_mode


def _path_inside_any_root(path: Path, roots: list[Path]) -> bool:
    resolved = path.expanduser().resolve()
    for root in roots:
        try:
            resolved.relative_to(root.expanduser().resolve())
            return True
        except ValueError:
            continue
    return False


def _working_dir_from_params(
    params: dict[str, Any],
    workspace_root: Path,
    *,
    workspace_roots: list[Path] | None = None,
    path_access_policy: PathAccessPolicy | None = None,
    access_mode: str = _DEFAULT_ACCESS_MODE,
) -> Path | ToolExecutionResult:
    working_dir = str(params.get("working_dir", "")).strip()
    target = Path(working_dir).expanduser() if working_dir else workspace_root
    if not target.is_dir():
        return workspace_root
    mode = _normalize_access_mode(access_mode)
    if mode == "full-access":
        return target
    roots = workspace_roots or [workspace_root]
    if _path_inside_any_root(target, roots):
        return target.resolve()
    if mode == "workspace-write":
        policy = path_access_policy or PathAccessPolicy.from_values()
        decision = policy.check(target)
        if decision.allowed:
            return target.resolve()
        return ToolExecutionResult(
            "run_command",
            False,
            f"COMMAND_ACCESS_DENIED: {decision.message}",
            error_code=decision.code or "PATH_ACCESS_DENIED",
        )
    return ToolExecutionResult(
        "run_command",
        False,
        (
            f"COMMAND_ACCESS_DENIED: access_mode={mode} 只允许在配置的工作区内执行命令。"
            " 如确实需要访问系统其他目录，请把 access_mode 显式改为 full-access。"
        ),
        error_code="PATH_OUTSIDE_WORKSPACE",
    )


def _bounded_output(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0:
        return "", bool(text)
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


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


def _subprocess_text_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _build_shell_tool_spec(access_mode: str, default_timeout: int, max_output_chars: int) -> ToolSpec:
    return ToolSpec(
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
            "working_dir": "Optional. In restricted/workspace-write mode it must stay inside workspace roots.",
            "access_mode": f"Runtime policy is configured outside the tool as access_mode={access_mode}.",
            "output": f"Stdout/stderr are bounded previews; each stream preview defaults to {max_output_chars} chars.",
        },
        examples=[
            '{"tool": "run_command", "command": "ls -la"}',
            '{"tool": "run_command", "command": "python --version", "working_dir": "."}',
            '{"tool": "run_command", "command": "make build", "timeout": 60}',
        ],
    )


class ShellTool(BaseTool):

    def __init__(
        self,
        workspace_root: Path,
        *,
        options: ShellToolOptions | None = None,
    ):
        options = options or ShellToolOptions()
        self.workspace_root = workspace_root.resolve()
        self.workspace_roots = [root.resolve() for root in (options.workspace_roots or [self.workspace_root])]
        self.path_access_policy = PathAccessPolicy.from_values(
            mode=options.path_access_mode,
            dangerous_roots=options.path_dangerous_roots,
        )
        self.access_mode = _normalize_access_mode(options.access_mode)
        self.default_timeout = options.default_timeout
        self.max_output_chars = max(0, int(options.max_output_chars))
        self.spec = _build_shell_tool_spec(self.access_mode, self.default_timeout, self.max_output_chars)

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        command_result = self._parse_command(params)
        if isinstance(command_result, ToolExecutionResult):
            return command_result
        command = command_result
        command_policy = evaluate_command_policy(command, allow_shell_operators=True)
        if not command_policy.allowed:
            return ToolExecutionResult(
                self.spec.name,
                False,
                (
                    "危险命令被系统拒绝: "
                    f"codes={','.join(command_policy.finding_codes)} command={command[:80]}..."
                ),
                error_code="COMMAND_POLICY_BLOCKED",
            )

        timeout = _timeout_from_params(params, self.default_timeout)
        if timeout <= 0:
            return ToolExecutionResult(
                self.spec.name,
                False,
                "TOOL_DEADLINE_EXCEEDED: 外层任务剩余时间不足，系统没有启动新的 shell 命令。",
                error_code="TOOL_TIMEOUT",
            )
        target = self._execution_target(params, command)
        if isinstance(target, ToolExecutionResult):
            return target
        return self._execute_with_artifact_protection(command, target, timeout)

    def _execution_target(
        self,
        params: dict[str, Any],
        command: str,
    ) -> Path | ToolExecutionResult:
        effective_access_mode = _effective_access_mode(self.access_mode, params.get("__access_mode"))
        target = _working_dir_from_params(
            params,
            self.workspace_root,
            workspace_roots=self.workspace_roots,
            path_access_policy=self.path_access_policy,
            access_mode=effective_access_mode,
        )
        if isinstance(target, ToolExecutionResult):
            return target
        delete_error = delete_target_access_error(
            DeleteAccessRequest(
                command=command,
                cwd=target,
                roots=self.workspace_roots,
                access_mode=effective_access_mode,
                path_access_policy=self.path_access_policy,
            )
        )
        if delete_error:
            return ToolExecutionResult(self.spec.name, False, delete_error, error_code="PATH_OUTSIDE_WORKSPACE")
        return target

    def _execute_with_artifact_protection(
        self,
        command: str,
        target: Path,
        timeout: int,
    ) -> ToolExecutionResult:
        try:
            artifact_snapshots = snapshot_ready_artifacts(self.workspace_root)
        except OSError as exc:
            return ToolExecutionResult(
                self.spec.name,
                False,
                f"ARTIFACT_BACKUP_FAILED: shell 执行前无法备份已登记产物: {exc}",
                error_code="ARTIFACT_BACKUP_FAILED",
            )
        artifact_summary: dict[str, Any] = {"snapshots": len(artifact_snapshots), "changed": [], "invalid": []}
        output, ok = self._run_process_text(command, target, timeout)
        try:
            artifact_summary = reconcile_shell_artifacts(self.workspace_root, artifact_snapshots)
        except OSError as exc:
            output = f"{output}\nARTIFACT_POSTCHECK_FAILED: shell 执行后无法复核已登记产物: {exc}"
        protection_note = shell_artifact_protection_note(artifact_summary)
        if protection_note:
            output = f"{output}\n{protection_note}"
        return ToolExecutionResult(
            self.spec.name,
            ok,
            output,
            result_envelope={"artifact_protection": artifact_summary},
        )

    def _run_process_text(
        self,
        command: str,
        target: Path,
        timeout: int,
    ) -> tuple[str, bool]:
        try:
            result = self._run_command(command, target, timeout)
            output = _format_process_result(result, self.max_output_chars)
            return output, True
        except subprocess.TimeoutExpired:
            return f"命令执行超时 timeout ({timeout}s): {command[:100]}...", False
        except OSError as exc:
            return f"命令执行失败: {exc}", False

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
