

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_py_agent.agent.artifacts.shell_protection import (
    reconcile_shell_artifacts,
    shell_artifact_protection_note,
    snapshot_ready_artifacts,
)
from agent_py_agent.agent.common.json_io import append_jsonl_capped
from agent_py_agent.agent.concurrency.interrupt import is_interrupted
from agent_py_agent.agent.contracts.gates.command_policy import (
    evaluate_command_policy,
)
from agent_py_agent.agent.path_access_policy import PathAccessPolicy

from .models import BaseTool, ToolExecutionResult, ToolSpec
from .process_registry import process_registry, terminate_process_tree
from .sandbox import SandboxUnavailable
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
    owner_scope_root: str = ""  # 多用户隔离:per-user owner home;空=不隔离
    protected_persona_root: str = ""
    access_mode: str = _DEFAULT_ACCESS_MODE
    default_timeout: int = 30
    max_output_chars: int = _DEFAULT_MAX_OUTPUT_CHARS


def _is_dangerous_command(command: str) -> bool:
    return not evaluate_command_policy(command, allow_shell_operators=True).allowed


class CommandTooLongError(ValueError):
    """command 超过 _MAX_COMMAND_CHARS：命令本身合法，只是太长，不是参数格式错。

    专门区分于"空 command"(那才是参数无效 TOOL_INVALID_ARGUMENTS)，让 _parse_command
    给出 COMMAND_TOO_LONG，引导模型拆成多条命令/改用 write_file，而不是误以为参数 schema 写错。
    仍继承 ValueError，既有 except ValueError 调用方与 match="过长" 测试不受影响。
    """


class CommandInterruptedError(RuntimeError):
    """Foreground command was cancelled by the owning conversation request."""


def _validate_command(command: str) -> str:
    if not command:
        raise ValueError("command 不能为空")
    text = command.strip()
    if not text:
        raise ValueError("command 不能为空或仅包含空白字符")
    if len(text) > _MAX_COMMAND_CHARS:
        raise CommandTooLongError(
            f"command 过长({len(text)} 字符)，最多 {_MAX_COMMAND_CHARS} 个字符；"
            "命令本身没问题,拆成多条 run_command 分别执行,或改用 write_file 写文件。"
        )
    return text


def _parsed_command_or_error(tool_name: str, raw_command: object) -> str | ToolExecutionResult:
    """校验 command,合法返回字符串,否则返回带精确 error_code 的失败结果。

    too-long 走 COMMAND_TOO_LONG(命令合法但太长→拆条/换 write_file),空/空白走
    TOOL_INVALID_ARGUMENTS(真缺必填参数)。两者分流,避免超长被误标成"参数格式错"。
    """
    try:
        return _validate_command(str(raw_command or ""))
    except CommandTooLongError as exc:
        return ToolExecutionResult(tool_name, False, str(exc), error_code="COMMAND_TOO_LONG")
    except ValueError as exc:
        return ToolExecutionResult(tool_name, False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")


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
    # LLM: owner-scoped shell 的 working_dir 只能来自 workspace_roots；本轮显式授权根由
    #   registry 临时注入。禁止 PathAccessPolicy 的全局公共区放行变成额外可写挂载。
    # 人类: 否则把 service-cwd 填进 working_dir，bwrap 会把它挂成可写目录。
    if path_access_policy is not None and path_access_policy.owner_scope_root is not None:
        return ToolExecutionResult(
            "run_command",
            False,
            "COMMAND_ACCESS_DENIED: 多用户 owner 只能在当前任务工作区或结构化授权目录执行命令。",
            error_code="PATH_OUTSIDE_WORKSPACE",
        )
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


def _subprocess_text_env(owner_home: object = None) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    _apply_owner_scoped_pip_env(env, owner_home)
    # 选项1-C 凭据擦洗:owner-scoped(降权,per-user 不可信命令)+ bwrap 放行外网 → 剥掉自管凭据
    # (MINIMAX_API_KEY / 飞书 app_secret 等),防 `curl 带 $KEY` 外泄(真机实锤 env 泄漏)。
    # admin/单租户(owner_home 空)= 可信 operator shell,保留全量(抄 长期助手 本地后端语义,不破单机)。
    if str(owner_home or "").strip():
        from ._subprocess_env_scrub import scrub_subprocess_env

        env = scrub_subprocess_env(env)
    return env


def _apply_owner_scoped_pip_env(env: dict[str, str], owner_home: object) -> None:
    """owner-scoped(降权)时把 Python 用户级安装/导入目录定向到 owner home 下的 .local(F11⑤)。

    PYTHONUSERBASE 既决定 ``pip install --user`` 装到哪、也决定 Python 的 user-site 从哪导入,
    所以普通用户装的依赖落在自己家、装完能直接 import,且不写系统站点。非 venv 时顺带 PIP_USER=1
    让 pip 默认走 --user(venv 内不设——venv 里 pip 会拒绝 --user;装进 venv 本就隔离)。
    owner_home 空(admin 提权/单租户)= 不动,保持全局/默认行为(可全局装)。"""
    text = str(owner_home or "").strip()
    if not text:
        return
    try:
        user_base = Path(text).expanduser().resolve(strict=False) / ".local"
    except (OSError, RuntimeError):
        return
    env["PYTHONUSERBASE"] = str(user_base)
    if sys.prefix == sys.base_prefix:  # 非 venv:让 pip 默认 --user,避免污染系统站点
        env["PIP_USER"] = "1"


logger = logging.getLogger(__name__)

_MAX_BG_LOG_BYTES = 1_000_000_000  # 后台命令日志字节上限(1GB);超限杀进程组,防失控/恶意命令写满磁盘(审计 #16)
_BG_WATCHDOG_INTERVAL = 2.0
_PROCESS_PIPE_DRAIN_SECONDS = 2.0


class _LogSizeWatchdog(threading.Thread):
    """监控后台进程日志大小,超上限 killpg 杀整组(终端交互 sizeWatchdog 范式)。进程退出即自停。"""

    def __init__(self, proc: subprocess.Popen, log_path: Path, *, max_bytes: int = _MAX_BG_LOG_BYTES, interval: float = _BG_WATCHDOG_INTERVAL) -> None:
        super().__init__(daemon=True)
        self._proc = proc
        self._log_path = log_path
        self._max = max_bytes
        self._interval = interval

    def run(self) -> None:
        while self._proc.poll() is None:
            if self._over_limit():
                _kill_process_group(self._proc)
                logger.error(f"后台命令日志超 {self._max} 字节上限,已杀进程组防写满磁盘: {self._log_path}")
                return
            time.sleep(self._interval)

    def _over_limit(self) -> bool:
        try:
            return self._log_path.stat().st_size > self._max
        except OSError:
            return False


def _kill_process_group(proc: subprocess.Popen) -> None:
    """前台、后台命令共用 process_registry 的完整后代树终止入口。"""
    terminate_process_tree(proc.pid, proc)


def _drain_terminated_process(proc: subprocess.Popen[str]) -> None:
    """Reap the direct child without trusting descendants to close inherited pipes."""
    try:
        proc.communicate(timeout=_PROCESS_PIPE_DRAIN_SECONDS)
        return
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            continue
    try:
        proc.wait(timeout=0.5)
    except (subprocess.TimeoutExpired, ChildProcessError):
        pass


# LLM: 前台宿主 shell 与 bwrap shell 共用同一超时/进程组回收语义；不要让隔离分支
#   复制出另一套 communicate 行为，否则超时会重新产生孙进程孤儿。
# 函数用途: 等待前台命令完成，超时则终止整个进程组并把超时继续交给工具层处理。
def _communicate_process(
    proc: subprocess.Popen[str],
    *,
    command: str,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if is_interrupted():
            _kill_process_group(proc)
            _drain_terminated_process(proc)
            raise CommandInterruptedError(command)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_process_group(proc)
            _drain_terminated_process(proc)
            raise subprocess.TimeoutExpired(command, timeout)
        try:
            out, err = proc.communicate(timeout=min(0.2, remaining))
            return subprocess.CompletedProcess(command, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            continue


# 函数用途: 判断 run_command 是否请求后台模式(布尔或 "true"/"1"/"yes" 字符串)。
def _wants_background(params: dict[str, Any]) -> bool:
    value = params.get("run_in_background")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes"}


def _sandbox_write_roots(params: dict[str, Any]) -> tuple[Path, ...] | None:
    """Return the per-invocation shell write roots carried by the structured boundary."""
    if "__sandbox_write_roots" not in params:
        return None
    raw = params.get("__sandbox_write_roots")
    if not isinstance(raw, (list, tuple)):
        return ()
    roots: list[Path] = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            root = Path(text).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if root not in roots:
            roots.append(root)
    return tuple(roots)


# LLM: 这是 owner-scoped shell 的不可绕过隔离门；owner_home 非空时只能返回
#   bwrap argv(shell=False)或抛 SandboxUnavailable，禁止恢复宿主 shell fallback。
# 函数用途: 为多用户命令选择隔离执行参数；单用户无 owner scope 时保留原有 shell。
def _sandbox_exec(
    command: str,
    target: Path,
    owner_home: object,
    protected_persona_root: object = None,
    write_roots: tuple[Path, ...] | None = None,
) -> tuple[Any, bool]:
    """多用户隔离 1 层:owner-scoped(owner_home 非空)且 bwrap 可用时,把命令包进 bwrap——根视图只有
    自己 owner home + 系统只读,隔离文件/进程,但【放行外网】;返回 (bwrap_argv, shell=False)。
    bwrap 不可用则 fail-closed；owner_home 空(显式全权/单租户)才直接使用原 shell。"""
    if not owner_home and not protected_persona_root:
        return command, True
    from .sandbox import SandboxSpec, find_bwrap, wrap_shell_command

    bwrap = find_bwrap()
    if not bwrap:
        raise SandboxUnavailable("BWRAP_NOT_FOUND:owner-scoped 命令要求 bwrap 隔离")
    argv = wrap_shell_command(
        command,
        SandboxSpec(
            owner_home=Path(owner_home or protected_persona_root),
            workspace=target,
            write_roots=write_roots,
            bwrap_path=bwrap,
            protected_persona_root=Path(protected_persona_root) if protected_persona_root else None,
            full_access=not bool(owner_home),
        ),
    )
    return argv, False


# LLM: 后台 shell 与前台共用 _sandbox_exec 硬门；Windows owner-scoped 也必须拒绝，
#   不能因为平台分支绕开 sandbox。Popen 失败由上层转成结构化工具错误。
# 函数用途: 独立会话启动后台进程,stdout/stderr 合并写入给定日志句柄。
def _spawn_background_process(
    command: str,
    target: Path,
    handle: Any,
    owner_home: object = None,
    protected_persona_root: object = None,
    write_roots: tuple[Path, ...] | None = None,
) -> subprocess.Popen:
    if owner_home or protected_persona_root:
        exec_arg, use_shell = _sandbox_exec(
            command,
            target,
            owner_home,
            protected_persona_root,
            write_roots,
        )
        return subprocess.Popen(
            exec_arg,
            shell=use_shell,
            cwd=str(target),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=_subprocess_text_env(owner_home),
        )
    if os.name == "nt":
        return subprocess.Popen(  # noqa: S602 - 工作区内受控 shell,与同步路径同策略
            ["powershell.exe", "-NoProfile", "-Command", command],
            cwd=str(target),
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=_subprocess_text_env(owner_home),
        )
    from .sandbox import strict_posix_shell_argv

    return subprocess.Popen(
        strict_posix_shell_argv(command),
        shell=False,
        cwd=str(target),
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=_subprocess_text_env(owner_home),
    )


# background_jobs 登记台账上限:后台任务每启一个登记一条,原裸 append 永不回收 → 长跑无界增长
# (审计 #16)。有界 append 只留最近 N 条(纯观测/孤儿排查用,丢最旧可接受),磁盘恒定。
_MAX_BACKGROUND_JOB_RECORDS = 1000


# 函数用途: 把后台任务登记到 .background_jobs/registry.jsonl(供观测/孤儿排查;
#   纯辅助,登记失败不影响进程已启动的事实)。
def _record_background_job(jobs_dir: Path, pid: int, command: str, log_path: Path) -> None:
    record = {"pid": pid, "command": command[:200], "output_file": str(log_path)}
    try:
        append_jsonl_capped(jobs_dir / "registry.jsonl", record, max_records=_MAX_BACKGROUND_JOB_RECORDS)
    except OSError:
        pass


def _build_shell_tool_spec(access_mode: str, default_timeout: int, max_output_chars: int) -> ToolSpec:
    return ToolSpec(
        name="run_command",
        category="shell",
        effect="mutating",
        promotes_task=True,
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
            "Do not keep files needed by a later tool call in /tmp: owner-scoped sandbox /tmp is a per-command tmpfs. Keep cross-command state in the selected workspace.",
            "Use wait for pure delays such as sleep 120 while waiting for subagent progress.",
            "盯守/轮询数据流→用 watch_stream,禁自写轮询脚本(无游标持久/覆盖账目,实测误报泛滥)。",
        ],
        keywords=["shell", "command", "terminal", "bash", "cmd", "script"],
        parameters={
            "command": "Shell command string to execute.",
            "timeout": f"Timeout in seconds; default {default_timeout}.",
            "working_dir": (
                "Execution directory; defaults to the structurally selected task root, "
                "or the workspace root when no task is selected."
            ),
            "run_in_background": "可选。true 时命令在后台运行,立即返回 pid 与 output_file,不阻塞工具循环;适合耗时长的下载/构建/批处理。",
        },
        parameter_details={
            "command": "Required. Full command string, for example 'ls -la' or 'python build.py'.",
            "timeout": f"Optional. Defaults to {default_timeout} seconds.",
            "working_dir": "Optional. In restricted/workspace-write mode it must stay inside workspace roots.",
            "access_mode": f"Runtime policy is configured outside the tool as access_mode={access_mode}.",
            "output": f"Stdout/stderr are bounded previews; each stream preview defaults to {max_output_chars} chars.",
            "temporary_storage": (
                "In an owner-scoped sandbox, /tmp is a per-command tmpfs. It is private "
                "to one run_command invocation and is discarded when that invocation ends. "
                "Persist files needed by later tool calls under the selected workspace instead."
            ),
            "run_in_background": "可选布尔,默认 false。后台模式不等待结束,立即返回 session_id+pid+output_file:用 process_status 查状态+输出、list_processes 看全部、kill_process 终止(杀整个进程组);也可 read_file 读 output_file 看完整日志。",
        },
        parameter_schema={
            "timeout": {"type": "integer", "minimum": 0},
            "run_in_background": {"type": "boolean"},
        },
        required_parameters=["command"],
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
            owner_scope_root=options.owner_scope_root,
        )
        self.access_mode = _normalize_access_mode(options.access_mode)
        self.protected_persona_root = str(options.protected_persona_root or "")
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
        sandbox_write_roots = _sandbox_write_roots(params)
        target = self._execution_target(params, command)
        if isinstance(target, ToolExecutionResult):
            return target
        if _wants_background(params):
            return self._start_background_command(command, target, sandbox_write_roots)
        return self._execute_with_artifact_protection(command, target, timeout, sandbox_write_roots)

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
        sandbox_write_roots: tuple[Path, ...] | None,
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
        output, ok, error_code, process_facts = self._run_process_text(
            command,
            target,
            timeout,
            sandbox_write_roots,
        )
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
            result_envelope={
                "artifact_protection": artifact_summary,
                "process": process_facts,
            },
            error_code="" if ok else error_code,
        )

    def _run_process_text(
        self,
        command: str,
        target: Path,
        timeout: int,
        sandbox_write_roots: tuple[Path, ...] | None,
    ) -> tuple[str, bool, str, dict[str, object]]:
        try:
            result = self._run_command(command, target, timeout, sandbox_write_roots)
            output = _format_process_result(result, self.max_output_chars)
            ok = result.returncode == 0
            if not ok:
                output += f"\n[note] 退出码 {result.returncode} 非零(测试失败/grep无匹配/diff有差异等常见,非命令本身故障);看上方 stdout/stderr 定位修正,勿当工具不可用。"
            return (
                output,
                ok,
                "" if ok else "COMMAND_FAILED",
                {
                    "status": "exited",
                    "return_code": int(result.returncode),
                    "command_succeeded": ok,
                },
            )
        except subprocess.TimeoutExpired:
            return (
                f"TOOL_TIMEOUT: 命令执行超时 timeout ({timeout}s): {command[:100]}...",
                False,
                "TOOL_TIMEOUT",
                {"status": "timed_out", "timeout_seconds": timeout},
            )
        except CommandInterruptedError:
            return (
                "CANCELLED: 当前任务已停止，前台命令及其子进程已终止。",
                False,
                "CANCELLED",
                {"status": "cancelled"},
            )
        except SandboxUnavailable as exc:
            return (
                f"SANDBOX_UNAVAILABLE: {exc}",
                False,
                "SANDBOX_UNAVAILABLE",
                {"status": "not_started", "reason": "sandbox_unavailable"},
            )
        except OSError as exc:
            return (
                f"COMMAND_FAILED: 命令执行失败: {exc}",
                False,
                "COMMAND_FAILED",
                {"status": "not_started", "reason": type(exc).__name__},
            )

    # LLM: 后台执行(P0-2 对照能力补齐:对照组 3/5 有持久/后台 shell,my-agent
    #   run_command 此前只能一次性阻塞执行,跑不了长任务而不卡住工具循环)。
    #   契约:Popen 独立会话启动(start_new_session,与子代理后台进程同款,出口
    #   孤儿回收能发现),stdout/stderr 合并落工作区 .background_jobs/ 日志文件,
    #   立即返回 pid + output_file;模型用 read_file 读进度、kill <pid> 收尾。
    #   不做交互式 stdin(那是 P0-2b,需 PTY 会话池),先覆盖最高频的"长任务
    #   后台化"。
    # 函数用途: 把命令丢到后台跑,马上回 pid 和日志路径,不等它结束。
    def _start_background_command(
        self,
        command: str,
        target: Path,
        sandbox_write_roots: tuple[Path, ...] | None,
    ) -> ToolExecutionResult:
        try:
            jobs_dir = self.workspace_root / ".background_jobs"
            jobs_dir.mkdir(parents=True, exist_ok=True)
            log_path = jobs_dir / f"job-{time.time_ns()}.log"
            handle = log_path.open("wb")
        except OSError as exc:
            return ToolExecutionResult(self.spec.name, False, f"COMMAND_FAILED: 后台日志创建失败: {exc}", error_code="COMMAND_FAILED")
        try:
            process = _spawn_background_process(
                command,
                target,
                handle,
                self.path_access_policy.owner_scope_root,
                self.protected_persona_root,
                sandbox_write_roots,
            )
        except SandboxUnavailable as exc:
            handle.close()
            return ToolExecutionResult(
                self.spec.name,
                False,
                f"SANDBOX_UNAVAILABLE: {exc}",
                error_code="SANDBOX_UNAVAILABLE",
            )
        except OSError as exc:
            handle.close()
            return ToolExecutionResult(self.spec.name, False, f"COMMAND_FAILED: 后台启动失败: {exc}", error_code="COMMAND_FAILED")
        handle.close()  # 子进程已持有 fd 副本,父进程关闭自己的句柄避免泄漏
        _record_background_job(jobs_dir, process.pid, command, log_path)
        _LogSizeWatchdog(process, log_path).start()  # 日志超上限即杀进程组,防写满磁盘
        # 登记进进程内注册表,模型可用 list_processes/process_status/kill_process
        # 按 session_id 查状态、收割、按进程组杀(避免只剩日志文件管不了进程)。
        record = process_registry.register(
            command=command,
            pid=process.pid,
            output_file=str(log_path),
            process=process,
            cwd=str(target),
        )
        payload = {
            "status": "started",
            "session_id": record.session_id,
            "pid": process.pid,
            "output_file": str(log_path),
            "hint": (
                "命令已在后台运行。用 process_status 传 session_id 查状态+最近输出,"
                "list_processes 看所有后台进程,kill_process 传 session_id 终止(会杀整个进程组)。"
                "也可以用 read_file 直接读 output_file 看完整日志。"
            ),
        }
        return ToolExecutionResult(self.spec.name, True, json.dumps(payload, ensure_ascii=False))

    def _parse_command(self, params: dict[str, Any]) -> str | ToolExecutionResult:
        return _parsed_command_or_error(self.spec.name, params.get("command", ""))

    def _run_command(
        self,
        command: str,
        target: Path,
        timeout: int,
        sandbox_write_roots: tuple[Path, ...] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if self.path_access_policy.owner_scope_root or self.protected_persona_root:
            return self._run_owner_scoped_command(command, target, timeout, sandbox_write_roots)
        if os.name == "nt":
            return subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", command],
                cwd=str(target),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_subprocess_text_env(self.path_access_policy.owner_scope_root),
                timeout=timeout,
            )
        # POSIX:独立会话启动(start_new_session)→ 超时时可杀整个进程组,消除孙进程(make/npm/编译器)孤儿。
        # 原 subprocess.run(timeout=) 超时只 SIGKILL 直接 shell,孙进程成孤儿累积耗尽 PID/CPU(审计 #14)。
        from .sandbox import strict_posix_shell_argv

        proc = subprocess.Popen(
            strict_posix_shell_argv(command),
            shell=False,
            cwd=str(target),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_subprocess_text_env(self.path_access_policy.owner_scope_root),
            start_new_session=True,
        )
        return _communicate_process(proc, command=command, timeout=timeout)

    # LLM: owner-scoped 前台命令的 Popen 只能吃 bwrap argv；二进制加载失败必须归一成
    #   SandboxUnavailable，让工具层返回结构化安全错误而不是普通命令失败。
    # 函数用途: 在当前 owner/workspace 隔离环境中运行一条前台命令并等待完成。
    def _run_owner_scoped_command(
        self,
        command: str,
        target: Path,
        timeout: int,
        sandbox_write_roots: tuple[Path, ...] | None,
    ) -> subprocess.CompletedProcess[str]:
        owner_home = self.path_access_policy.owner_scope_root
        exec_arg, use_shell = _sandbox_exec(
            command,
            target,
            owner_home,
            self.protected_persona_root,
            sandbox_write_roots,
        )
        try:
            proc = subprocess.Popen(
                exec_arg,
                shell=use_shell,
                cwd=str(target),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_subprocess_text_env(owner_home),
                start_new_session=True,
            )
        except OSError as exc:
            raise SandboxUnavailable(f"BWRAP_EXEC_FAILED:{exc}") from exc
        return _communicate_process(proc, command=command, timeout=timeout)
