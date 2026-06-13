

from __future__ import annotations

import json
import os
import re
import shlex
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
from agent_py_agent.agent.contracts.gates.command_policy import (
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
_WAIT_MIN_SECONDS = 60
_WAIT_MAX_SECONDS = 7200
_INTERNAL_AGENT_PATH_RE = re.compile(
    r"(?P<prefix>(?:^|[\s'\";|&])(?:\S*/)?tasks/\S+/work/agents(?:/|\b)|(?:^|[\s'\";|&])work/agents(?:/|\b))",
    re.I,
)
_INTERNAL_AGENT_RUN_RE = re.compile(r"(?<![A-Za-z0-9_-])(?P<run_id>(?:subagent|run)-[A-Za-z0-9_-]+)")

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


def _pure_delay_seconds(command: str) -> int | None:
    try:
        tokens = shlex.split(command.strip().rstrip(";"))
    except ValueError:
        return None
    if not tokens:
        return None
    head = tokens[0].lower()
    if head == "sleep":
        return _leading_sleep_delay_seconds(tokens[1:])
    if head == "timeout":
        return _timeout_delay_seconds(tokens[1:])
    if head == "start-sleep":
        return _start_sleep_delay_seconds(tokens[1:])
    return None


def _leading_sleep_delay_seconds(tokens: list[str]) -> int | None:
    if len(tokens) == 1:
        return _delay_token_seconds(tokens[0])
    if len(tokens) >= 3 and tokens[1] in {"&&", ";"} and tokens[2].lower() in {"echo", "printf", "true"}:
        return _delay_token_seconds(tokens[0])
    return None


def _delay_token_seconds(value: str) -> int | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    multiplier = 1
    if text.endswith(("s", "m", "h")):
        suffix = text[-1]
        text = text[:-1]
        multiplier = {"s": 1, "m": 60, "h": 3600}[suffix]
    try:
        seconds = float(text) * multiplier
    except ValueError:
        return None
    if seconds <= 0 or seconds != seconds:
        return None
    return max(1, int(seconds))


def _timeout_delay_seconds(tokens: list[str]) -> int | None:
    positional: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index].lower()
        if token in {"/t", "-t"} and index + 1 < len(tokens):
            return _delay_token_seconds(tokens[index + 1])
        if token in {"/nobreak", "-nobreak"}:
            index += 1
            continue
        positional.append(tokens[index])
        index += 1
    return _delay_token_seconds(positional[0]) if len(positional) == 1 else None


def _start_sleep_delay_seconds(tokens: list[str]) -> int | None:
    if len(tokens) == 1:
        return _delay_token_seconds(tokens[0])
    for index, token in enumerate(tokens):
        if token.lower() in {"-seconds", "-s"} and index + 1 < len(tokens):
            return _delay_token_seconds(tokens[index + 1])
    return None


def _delay_command_result(seconds: int) -> ToolExecutionResult:
    suggested_seconds = min(_WAIT_MAX_SECONDS, max(_WAIT_MIN_SECONDS, seconds))
    payload = {
        "ok": False,
        "error": "use_wait_for_delay",
        "message": "run_command does not execute pure delay commands. Use wait so progress watching does not block the local shell.",
        "requested_seconds": seconds,
        "suggested_tool_call": {
            "tool": "wait",
            "seconds": suggested_seconds,
            "reason": "wait before checking progress again",
        },
    }
    return ToolExecutionResult(
        "run_command",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code="USE_WAIT_FOR_DELAY",
    )


def _internal_agent_status_command(command: str) -> dict[str, object] | None:
    normalized = command.replace("\\", "/")
    if not _INTERNAL_AGENT_PATH_RE.search(normalized):
        return None
    run_match = _INTERNAL_AGENT_RUN_RE.search(normalized)
    run_id = run_match.group("run_id") if run_match else ""
    suggestion: dict[str, object] = {"tool": "inspect_agent_tree"}
    if run_id:
        suggestion["run_id"] = run_id
    return {
        "ok": False,
        "error": "internal_agent_status_ref",
        "message": "Shell commands must not inspect internal work/agents status files. Use inspect_agent_tree for run status, then read child_result_index.read_order or declared output files for child results.",
        "run_id": run_id,
        "suggested_tool_call": suggestion,
        "result_fields_to_read": ["child_result_index.read_order", "child_result_index.expected_outputs"],
    }


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
        field = "working_dir" if working_dir else "workspace_root"
        return ToolExecutionResult(
            "run_command",
            False,
            f"COMMAND_ACCESS_DENIED: {field} does not exist or is not a directory: {target}",
            error_code="PATH_NOT_FOUND",
        )
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
    """超限时保留头部+尾部、省略中段（构建/测试输出的结论通常在尾部）。"""
    if max_chars <= 0:
        return "", bool(text)
    if len(text) <= max_chars:
        return text, False
    head_chars = max(1, int(max_chars * 0.6))
    tail_chars = max(1, max_chars - head_chars)
    omitted = len(text) - head_chars - tail_chars
    marker = f"\n...[中段省略 {omitted} 字符，完整输出共 {len(text)} 字符 / {text.count(chr(10)) + 1} 行]...\n"
    return text[:head_chars] + marker + text[-tail_chars:], True


def _format_process_result(result: subprocess.CompletedProcess[str], max_output_chars: int) -> str:
    stdout = result.stdout if result.stdout else ""
    stderr = result.stderr if result.stderr else ""
    stdout_preview, stdout_truncated = _bounded_output(stdout, max_output_chars)
    stderr_preview, stderr_truncated = _bounded_output(stderr, max_output_chars)
    return (
        f"return_code={result.returncode}\n"
        f"stdout_chars={len(stdout)} stdout_lines={stdout.count(chr(10)) + (1 if stdout else 0)} "
        f"stdout_preview_chars={len(stdout_preview)} "
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


# 函数用途: 判断 run_command 是否请求后台模式(布尔或 "true"/"1"/"yes" 字符串)。
def _wants_background(params: dict[str, Any]) -> bool:
    value = params.get("run_in_background")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes"}


# 函数用途: 独立会话启动后台进程,stdout/stderr 合并写入给定日志句柄。
def _spawn_background_process(command: str, target: Path, handle: Any) -> subprocess.Popen:
    if os.name == "nt":
        return subprocess.Popen(  # noqa: S602 - 工作区内受控 shell,与同步路径同策略
            ["powershell.exe", "-NoProfile", "-Command", command],
            cwd=str(target),
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=_subprocess_text_env(),
        )
    return subprocess.Popen(  # noqa: S602
        command,
        shell=True,
        cwd=str(target),
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=_subprocess_text_env(),
    )


# 函数用途: 把后台任务登记到 .background_jobs/registry.jsonl(供观测/孤儿排查;
#   纯辅助,登记失败不影响进程已启动的事实)。
def _record_background_job(jobs_dir: Path, pid: int, command: str, log_path: Path) -> None:
    record = {"pid": pid, "command": command[:200], "output_file": str(log_path)}
    try:
        with (jobs_dir / "registry.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


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
            "脚本里需要调用 LLM（翻译/摘要/分类等）时：子进程环境自带 AGENT_API_KEY、AGENT_API_BASE、AGENT_MODEL_NAME（与本代理同款 anthropic 兼容端点），直接用它们初始化客户端，不要猜测其他服务商端点。",
        ],
        avoid_when=[
            "Use read_file / write_file when only file IO is needed.",
            "Avoid for interactive terminal workflows.",
            "Prefer write_file for file changes instead of shell redirection.",
            "Use wait for pure delays such as sleep 120 while waiting for subagent progress.",
        ],
        keywords=["shell", "command", "terminal", "bash", "cmd", "script"],
        parameters={
            "command": "Shell command string to execute.",
            "timeout": f"Timeout in seconds; default {default_timeout}.",
            "working_dir": "Execution directory; defaults to the workspace root.",
            "run_in_background": "可选。true 时命令在后台运行,立即返回 pid 与 output_file,不阻塞工具循环;适合耗时长的下载/构建/批处理。",
        },
        parameter_details={
            "command": "Required. Full command string, for example 'ls -la' or 'python build.py'.",
            "timeout": f"Optional. Defaults to {default_timeout} seconds.",
            "working_dir": "Optional. In restricted/workspace-write mode it must stay inside workspace roots.",
            "access_mode": f"Runtime policy is configured outside the tool as access_mode={access_mode}.",
            "output": f"Stdout/stderr are bounded previews; each stream preview defaults to {max_output_chars} chars.",
            "run_in_background": "可选布尔,默认 false。后台模式不等待结束:用 read_file 读 output_file 看进度,完成后用 run_command 执行 kill <pid> 收尾。",
        },
        examples=[
            '{"tool": "run_command", "command": "ls -la"}',
            '{"tool": "run_command", "command": "python --version", "working_dir": "."}',
            '{"tool": "run_command", "command": "make build", "timeout": 60}',
            '{"tool": "run_command", "command": "python download_all.py", "run_in_background": true}',
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
        internal_status_ref = _internal_agent_status_command(command)
        if internal_status_ref is not None:
            return ToolExecutionResult(
                self.spec.name,
                False,
                json.dumps(internal_status_ref, ensure_ascii=False, indent=2),
                error_code="WRONG_STATUS_SURFACE",
            )
        delay_seconds = _pure_delay_seconds(command)
        if delay_seconds is not None:
            return _delay_command_result(delay_seconds)

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
        if _wants_background(params):
            return self._start_background_command(command, target)
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
        output, ok, error_code = self._run_process_text(command, target, timeout)
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
            error_code="" if ok else error_code,
        )

    def _run_process_text(
        self,
        command: str,
        target: Path,
        timeout: int,
    ) -> tuple[str, bool, str]:
        try:
            result = self._run_command(command, target, timeout)
            output = _format_process_result(result, self.max_output_chars)
            ok = result.returncode == 0
            return output, ok, "" if ok else "COMMAND_FAILED"
        except subprocess.TimeoutExpired:
            return f"TOOL_TIMEOUT: 命令执行超时 timeout ({timeout}s): {command[:100]}...", False, "TOOL_TIMEOUT"
        except OSError as exc:
            return f"COMMAND_FAILED: 命令执行失败: {exc}", False, "COMMAND_FAILED"

    # LLM: 后台执行(P0-2 对照能力补齐:对照组 3/5 有持久/后台 shell,my-agent
    #   run_command 此前只能一次性阻塞执行,跑不了长任务而不卡住工具循环)。
    #   契约:Popen 独立会话启动(start_new_session,与子代理后台进程同款,出口
    #   孤儿回收能发现),stdout/stderr 合并落工作区 .background_jobs/ 日志文件,
    #   立即返回 pid + output_file;模型用 read_file 读进度、kill <pid> 收尾。
    #   不做交互式 stdin(那是 P0-2b,需 PTY 会话池),先覆盖最高频的"长任务
    #   后台化"。
    # 函数用途: 把命令丢到后台跑,马上回 pid 和日志路径,不等它结束。
    def _start_background_command(self, command: str, target: Path) -> ToolExecutionResult:
        try:
            jobs_dir = self.workspace_root / ".background_jobs"
            jobs_dir.mkdir(parents=True, exist_ok=True)
            log_path = jobs_dir / f"job-{time.time_ns()}.log"
            handle = log_path.open("wb")
        except OSError as exc:
            return ToolExecutionResult(self.spec.name, False, f"COMMAND_FAILED: 后台日志创建失败: {exc}", error_code="COMMAND_FAILED")
        try:
            process = _spawn_background_process(command, target, handle)
        except OSError as exc:
            handle.close()
            return ToolExecutionResult(self.spec.name, False, f"COMMAND_FAILED: 后台启动失败: {exc}", error_code="COMMAND_FAILED")
        handle.close()  # 子进程已持有 fd 副本,父进程关闭自己的句柄避免泄漏
        _record_background_job(jobs_dir, process.pid, command, log_path)
        payload = {
            "status": "started",
            "pid": process.pid,
            "output_file": str(log_path),
            "hint": "命令已在后台运行。用 read_file 读 output_file 看进度与结果;需要终止时用 run_command 执行 kill <pid>。",
        }
        return ToolExecutionResult(self.spec.name, True, json.dumps(payload, ensure_ascii=False))

    def _parse_command(self, params: dict[str, Any]) -> str | ToolExecutionResult:
        try:
            return _validate_command(str(params.get("command", "")))
        except ValueError as exc:
            return ToolExecutionResult(self.spec.name, False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")

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
