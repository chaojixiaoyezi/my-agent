from __future__ import annotations

import hashlib
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

from agent_py_agent.agent.artifacts.shell_operation_journal import operation_manifest_ref
from agent_py_agent.agent.artifacts.shell_protection import (
    complete_shell_artifact_operation,
    mark_shell_artifact_operation_executing,
    reconcile_shell_artifact_operation,
    reconcile_shell_artifact_operation_items,
    release_shell_artifact_operation,
    settle_shell_artifact_operation,
    shell_artifact_protection_note,
    snapshot_ready_artifacts,
)
from agent_py_agent.agent.common.json_io import append_jsonl_capped
from agent_py_agent.agent.concurrency.interrupt import is_interrupted
from agent_py_agent.agent.contracts.gates.command_policy import (
    analyze_command,
    evaluate_command_policy,
)
from agent_py_agent.agent.path_access_policy import PathAccessPolicy

from .background_process_host import (
    HostedBackgroundProcess,
    start_background_process_host,
)
from .cancellation import (
    CancellationToken,
    cancellation_requested,
    current_cancellation_token,
    register_cancellation_callback,
)
from .models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    SandboxPolicy,
    TimeoutPolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolModelHints,
    ToolModelSpec,
    ToolOperationReconciliation,
    ToolOperationReconciliationContext,
    ToolOperationSettlementContext,
    ToolRuntimePolicy,
    TrustedParameterBinding,
)
from .process_registry import (
    BackgroundProcess,
    ProcessAccessScope,
    ProcessRegistration,
    process_access_scope,
    process_registry,
    terminate_process_tree,
)
from .process_session_store import process_session_store_root
from .sandbox import SandboxUnavailable

_MAX_COMMAND_CHARS = 2000
_DEFAULT_MAX_OUTPUT_CHARS = 12_000
_DEFAULT_ACCESS_MODE = "workspace-write"
_ShellSandboxRoots = tuple[
    tuple[Path, ...] | None,
    tuple[Path, ...] | None,
    tuple[Path, ...] | None,
]
_ACCESS_MODES = frozenset({"restricted", "workspace-write", "full-access"})
_ACCESS_MODE_RANK = {"restricted": 0, "workspace-write": 1, "full-access": 2}
_TOOL_DEADLINE_UNIX_ENV = "MY_AGENT_TOOL_DEADLINE_UNIX"
_TOOL_DEADLINE_MARGIN_SECONDS_ENV = "MY_AGENT_TOOL_DEADLINE_MARGIN_SECONDS"
# shell 解释器错误前缀白名单(command not found 判定用, 2026-08-14):
# bash/sh/zsh 找不到命令时 stderr 固定以 "<shell>: " 开头(如
# "/bin/bash: 行 1: cmd: 未找到命令" 中文 locale 亦然), 不随 locale 变;
# 显式 `exit 127` 的命令自身 stderr 无此前缀——命令确实执行过, 副作用可能
# 已发生, 不得误判 not_started。只做前缀白名单(结构化来源标识), 不匹配
# 错误内容文本(禁 NL 匹配铁律)。
_SHELL_ERROR_PREFIXES = (
    "bash: ",
    "/bin/bash: ",
    "sh: ",
    "/bin/sh: ",
    "dash: ",
    "/bin/dash: ",
    "zsh: ",
    "/bin/zsh: ",
)
_INTERNAL_AGENT_PATH_RE = re.compile(
    r"(?P<prefix>(?:^|[\s'\";|&])(?:\S*/)?tasks/\S+/work/agents(?:/|\b)|(?:^|[\s'\";|&])work/agents(?:/|\b))",
    re.I,
)
_INTERNAL_AGENT_RUN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<run_id>(?:subagent|run)-[A-Za-z0-9_-]+)"
)


@dataclass(frozen=True)
class ShellToolOptions:
    workspace_roots: list[Path] | None = None
    path_access_mode: str = "normal"
    path_dangerous_roots: list[str] | None = None
    owner_scope_root: str = ""  # 多用户隔离:per-user owner home;空=不隔离
    protected_persona_root: str = ""
    artifact_backup_root: Path | None = None
    access_mode: str = _DEFAULT_ACCESS_MODE
    default_timeout: int = 30
    max_output_chars: int = _DEFAULT_MAX_OUTPUT_CHARS


# LLM: This immutable request carries the exact ActionPolicy operation decision into shell recovery;
# it prevents read-only/direct calls from waiting for a settlement notification that will not exist.
# 类用途: 汇总一次前台命令的执行边界、恢复身份及是否由通用副作用账本接管终态。
@dataclass(frozen=True)
class _ShellArtifactExecutionRequest:
    """Host-only inputs for one foreground shell operation."""

    tool: ShellTool
    command: str
    target: Path
    timeout: int
    sandbox_roots: _ShellSandboxRoots
    effective_access_mode: str
    run_scope: object
    tool_call_id: str
    operation_id: str
    operation_managed: bool


# LLM: Prepared state binds snapshots to one stable private operation key and must not be rebuilt
# from the post-command filesystem.
# 类用途: 保存命令启动前已经落盘的产物前像及恢复清单引用，供退出后复核与结算。
@dataclass(frozen=True)
class _ShellArtifactExecutionState:
    """Prepared owner-private artifact state surrounding one shell process."""

    snapshots: list[object]
    source_roots: tuple[Path, ...] | None
    summary: dict[str, Any]
    operation_key: str
    operation_ref: str


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


def _parsed_command_or_error(tool_name: str, raw_command: object) -> str | ToolHandlerOutcome:
    """校验 command,合法返回字符串,否则返回带精确 error_code 的失败结果。

    too-long 走 COMMAND_TOO_LONG(命令合法但太长→拆条/换 write_file),空/空白走
    TOOL_INVALID_ARGUMENTS(真缺必填参数)。两者分流,避免超长被误标成"参数格式错"。
    """
    try:
        return _validate_command(str(raw_command or ""))
    except CommandTooLongError as exc:
        return ToolHandlerOutcome(tool_name, False, str(exc), error_code="COMMAND_TOO_LONG")
    except ValueError as exc:
        return ToolHandlerOutcome(tool_name, False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")


# LLM: shell 不能成为读取子代理内部状态的旁路；命中时只返回事件等待与交付
# refs 提示，不暴露模型轮询工具。
# 函数用途: 识别并拒绝通过命令行窥探 work/agents 内部状态文件的操作。
def _internal_agent_status_command(command: str) -> dict[str, object] | None:
    normalized = command.replace("\\", "/")
    if not _INTERNAL_AGENT_PATH_RE.search(normalized):
        return None
    run_match = _INTERNAL_AGENT_RUN_RE.search(normalized)
    run_id = run_match.group("run_id") if run_match else ""
    return {
        "ok": False,
        "error": "internal_agent_status_ref",
        "message": "Shell commands must not inspect internal work/agents status files. Wait for the direct-child lifecycle event, then read child_result_index.read_order or declared output files for child results.",
        "run_id": run_id,
        "next_action": "await_direct_child_lifecycle_event",
        "result_fields_to_read": [
            "child_result_index.read_order",
            "child_result_index.expected_outputs",
        ],
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


# LLM: Relative cwd values resolve against the canonical workspace, never process cwd. Owner scope
# is checked before command access mode, and a per-turn root may only narrow—not widen—that scope.
# 函数用途: 裁决命令实际工作目录，阻止普通用户或子代理借 working_dir 跳出自己的工作区。
def _working_dir_from_params(
    params: dict[str, Any],
    workspace_root: Path,
    *,
    workspace_roots: list[Path] | None = None,
    path_access_policy: PathAccessPolicy | None = None,
    access_mode: str = _DEFAULT_ACCESS_MODE,
) -> Path | ToolHandlerOutcome:
    working_dir = str(params.get("working_dir", "")).strip()
    target = Path(working_dir).expanduser() if working_dir else workspace_root
    if not target.is_absolute():
        target = workspace_root / target
    target = target.resolve(strict=False)
    if not target.is_dir():
        field = "working_dir" if working_dir else "workspace_root"
        return ToolHandlerOutcome(
            "run_command",
            False,
            f"COMMAND_ACCESS_DENIED: {field} does not exist or is not a directory: {target}",
            error_code="PATH_NOT_FOUND",
        )
    mode = _normalize_access_mode(access_mode)
    # owner wall is stronger than both a model-provided working_dir and a per-turn workspace
    # projection. A normal user cannot turn full-access text or an injected root into host access.
    if path_access_policy is not None and path_access_policy.owner_scope_root is not None:
        decision = path_access_policy.check(target)
        roots = workspace_roots or [workspace_root]
        if not decision.allowed or not _path_inside_any_root(target, roots):
            return ToolHandlerOutcome(
                "run_command",
                False,
                "COMMAND_ACCESS_DENIED: 当前 owner 只能在自己的 WorkspaceOnly 范围内执行命令。",
                error_code=decision.code or "PATH_OUTSIDE_WORKSPACE",
            )
        return target
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
        return ToolHandlerOutcome(
            "run_command",
            False,
            f"COMMAND_ACCESS_DENIED: {decision.message}",
            error_code=decision.code or "PATH_ACCESS_DENIED",
        )
    return ToolHandlerOutcome(
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


# LLM: shell 富展示直接读取 CompletedProcess 的 stdout/stderr/returncode；renderer 不得从格式化 output 文本反解析机器事实，预览仍有独立硬上限。
# 函数用途: 为终端生成分栏的标准输出、错误输出、行数和截断状态。
def _command_display(
    result: subprocess.CompletedProcess[str],
    max_output_chars: int,
) -> dict[str, object]:
    stdout = str(result.stdout or "")
    stderr = str(result.stderr or "")
    preview_limit = min(max(1, int(max_output_chars or 1)), 12_000)
    stdout_preview, stdout_truncated = _bounded_output(stdout, preview_limit)
    stderr_preview, stderr_truncated = _bounded_output(stderr, preview_limit)
    return {
        "kind": "command",
        "return_code": int(result.returncode),
        "stdout": stdout_preview,
        "stderr": stderr_preview,
        "stdout_lines": stdout.count("\n") + (1 if stdout else 0),
        "stderr_lines": stderr.count("\n") + (1 if stderr else 0),
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }


# LLM: owner-scoped 子进程的 home 是只读底图；标准临时目录和 XDG cache 必须显式指向
# 沙箱内 /tmp。npm 是已验证会忽略 XDG 并写 ``$HOME/.npm`` 的标准工具例外，因此只覆盖其官方
# cache 变量；未知工具仍使用开放世界的 TMPDIR/XDG 路径。凭据擦洗仍先执行，不能因此恢复 secret。
# 函数用途: 构造 shell 子进程环境，并把普通缓存及 npm 缓存安全地放进任务持久临时区。
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
        env["TMPDIR"] = "/tmp"
        env["XDG_CACHE_HOME"] = "/tmp/.cache"
        env["NPM_CONFIG_CACHE"] = "/tmp/.cache/npm"
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

_MAX_BG_LOG_BYTES = (
    1_000_000_000  # 后台命令日志字节上限(1GB);超限杀进程组,防失控/恶意命令写满磁盘(审计 #16)
)
_BG_WATCHDOG_INTERVAL = 2.0
_PROCESS_PIPE_DRAIN_SECONDS = 2.0
# 会话运行时 unified exec 会先 yield 一小段时间：短命令在同一工具结果里返回 exit code，
# 只有仍存活的进程才返回 session。显式后台模式取 0.5 秒，既抓住端口占用/导入失败，
# 又不把持久服务启动拖成阻塞调用。
_BACKGROUND_START_SETTLE_SECONDS = 0.5


class _LogSizeWatchdog(threading.Thread):
    """监控后台进程日志大小,超上限 killpg 杀整组(终端交互 sizeWatchdog 范式)。进程退出即自停。"""

    def __init__(
        self,
        proc: subprocess.Popen,
        log_path: Path,
        *,
        max_bytes: int = _MAX_BG_LOG_BYTES,
        interval: float = _BG_WATCHDOG_INTERVAL,
        cancellation_token: CancellationToken | None = None,
    ) -> None:
        super().__init__(daemon=True)
        self._proc = proc
        self._log_path = log_path
        self._max = max_bytes
        self._interval = interval
        self._cancellation_token = cancellation_token

    def run(self) -> None:
        while self._proc.poll() is None:
            if self._cancellation_token and self._cancellation_token.cancelled:
                _kill_process_group(self._proc)
                return
            if self._over_limit():
                _kill_process_group(self._proc)
                logger.error(
                    f"后台命令日志超 {self._max} 字节上限,已杀进程组防写满磁盘: {self._log_path}"
                )
                return
            if self._cancellation_token is not None:
                self._cancellation_token.wait(self._interval)
            else:
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
    with register_cancellation_callback(lambda: _kill_process_group(proc)):
        while True:
            if is_interrupted() or cancellation_requested():
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
                if cancellation_requested():
                    raise CommandInterruptedError(command)
                return subprocess.CompletedProcess(command, proc.returncode, out, err)
            except subprocess.TimeoutExpired:
                continue


# 函数用途: 判断 run_command 是否请求后台模式(布尔或 "true"/"1"/"yes" 字符串)。
def _wants_background(params: dict[str, Any]) -> bool:
    value = params.get("run_in_background")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes"}


# LLM: Heredoc bodies are payload bytes rather than shell syntax. This helper
# removes only those bodies while retaining opener and later shell lines, so
# background detection cannot mistake source-code address operators for `&`.
# 函数用途: 剔除 heredoc 正文，保留真正会由 shell 解释的命令行。
def _shell_syntax_without_heredoc_bodies(command: str) -> str:
    syntax_lines: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in str(command or "").splitlines():
        if pending:
            delimiter, strip_tabs = pending[0]
            candidate = line.lstrip("\t") if strip_tabs else line
            if candidate == delimiter:
                pending.pop(0)
            continue
        syntax_lines.append(line)
        pending.extend(_heredoc_delimiters_from_shell_line(line))
    return "\n".join(syntax_lines)


# LLM: Delimiter discovery uses the same quote-aware shell lexer as control
# parsing. It recognizes `<<`/`<<-` only on syntax lines and never scans payload.
# 函数用途: 读取一条 shell 命令里按出现顺序声明的 heredoc 结束标记。
def _heredoc_delimiters_from_shell_line(line: str) -> list[tuple[str, bool]]:
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = tuple(lexer)
    except ValueError:
        return []
    delimiters: list[tuple[str, bool]] = []
    for index, token in enumerate(tokens[:-1]):
        if token != "<<":
            continue
        delimiter = str(tokens[index + 1] or "")
        strip_tabs = delimiter.startswith("-")
        if strip_tabs:
            delimiter = delimiter[1:]
        if delimiter and delimiter not in {";", "&", "|", "<", ">"}:
            delimiters.append((delimiter, strip_tabs))
    return delimiters


# LLM: 持久进程只有 run_in_background 一条权威启动路径；这里识别 shell 语法 token，
#   不能用正则匹配自然语言，也不能把 heredoc/引号内的 & 或 2>&1 重定向误判为后台操作符。
# 函数用途: 判断命令是否试图用 shell 的独立 & 操作符绕开后台进程注册表。
def _contains_unmanaged_background_operator(command: str) -> bool:
    try:
        shell_syntax = _shell_syntax_without_heredoc_bodies(command)
        lexer = shlex.shlex(shell_syntax, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return "&" in tuple(lexer)
    except ValueError:
        return False


# LLM: 错误正文只解释如何改成唯一受管形态，控制流仍由已注册 error_code 和
#   effect_outcome=not_started 决定；不要在 ShellTool.execute 内复制这份合同。
# 函数用途: 返回“未启动且应改用结构化后台参数”的标准工具结果。
def _unmanaged_background_result(tool_name: str) -> ToolHandlerOutcome:
    payload = {
        "ok": False,
        "error": "background_process_mode_required",
        "message": (
            "系统没有启动该命令。shell 的 '&' 会绕开后台会话管理；"
            "请删除 '&'（通常也不需要 nohup），用原前台命令重新调用 "
            "run_command，并设置 run_in_background=true。"
        ),
        "required_call_shape": {
            "tool": "run_command",
            "command": "不含 shell '&' 的前台命令",
            "run_in_background": True,
        },
    }
    return ToolHandlerOutcome(
        tool_name,
        False,
        json.dumps(payload, ensure_ascii=False),
        result_envelope=payload,
        error_code="BACKGROUND_PROCESS_MODE_REQUIRED",
        effect_outcome="not_started",
    )


def _sandbox_write_roots(params: dict[str, Any]) -> tuple[Path, ...] | None:
    """Return the per-invocation shell write roots carried by the structured boundary."""
    return _sandbox_roots(params, "__sandbox_write_roots")


def _sandbox_read_roots(params: dict[str, Any]) -> tuple[Path, ...] | None:
    """Return the per-invocation read-only roots carried by the structured boundary."""
    return _sandbox_roots(params, "__sandbox_read_roots")


# LLM: These roots are host-authored deny overlays, not model arguments. Process sandboxes apply
# them after broader writable roots so owner control metadata cannot be changed indirectly.
# 函数用途: 读取当前命令必须保持只读的 owner 控制面路径。
def _sandbox_protected_write_paths(params: dict[str, Any]) -> tuple[Path, ...] | None:
    return _sandbox_roots(params, "__sandbox_protected_write_paths")


def _sandbox_roots(
    params: dict[str, Any],
    key: str,
) -> tuple[Path, ...] | None:
    if key not in params:
        return None
    raw = params.get(key)
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


# LLM: 这是所有生产 shell spawn 的唯一隔离门（G6）：owner-scoped 与单租户
#   都经 AttemptExecutionSandbox 网关（Linux bwrap / macOS Seatbelt），只能
#   返回沙箱 argv(shell=False)或抛 SandboxUnavailable，禁止恢复宿主 shell
#   fallback（3.txt E.6/E.7）。单租户走 full_access 档：文件语义不变，网关
#   统一 + 进程隔离 + readiness 检查仍生效（E.8 不取消 sandbox）。
# 函数用途: 为命令选择跨平台 attempt 沙箱执行参数；返回 (argv, shell=False)。
def _sandbox_exec(
    command: str,
    target: Path,
    owner_home: object,
    protected_persona_root: object = None,
    write_roots: tuple[Path, ...] | None = None,
    read_roots: tuple[Path, ...] | None = None,
    protected_write_paths: tuple[Path, ...] | None = None,
) -> tuple[Any, bool]:
    """把命令包进 attempt 沙箱（G6 接线）。

    owner-scoped（owner_home 非空）：attempt_view=任务工作目录（本 attempt
    可写工作区）、staging_root=同任务目录（publish 棒接入前 staging 语义由
    任务目录承担）、shared_workspace=owner home（只读底图）。
    单租户（owner_home 空）：full_access 档（bwrap 整根 bind / Seatbelt 不
    deny file-write），文件权限语义与宿主一致，隔离=网关统一+进程隔离。
    protected_persona_root 只形成更精确的只读覆盖；它不会把已解除 owner 墙的
    Full Access 降回 WorkspaceOnly。
    """
    from ..attempt.sandbox import AttemptExecutionSandbox, AttemptSandboxSpec
    from .sandbox import strict_posix_shell_argv

    owner_text = str(owner_home or "").strip()
    persona_text = str(protected_persona_root or "").strip()
    # Full Access is the absence of an owner wall. Persona and control metadata remain more
    # specific read-only overlays; they must not silently downgrade the rest of the filesystem.
    full_access = not owner_text
    if owner_text:
        owner = Path(owner_text).expanduser().resolve(strict=False)
        shared = owner
        base = owner
    elif persona_text:
        # Full Access 仍需要一个稳定字段承载 persona 精确只读覆盖；这不是 owner 墙。
        owner = Path(persona_text).expanduser().resolve(strict=False)
        shared = owner
        base = Path(persona_text).expanduser().resolve(strict=False)
    else:
        shared = Path(target).expanduser().resolve(strict=False)
        base = shared
    spec = AttemptSandboxSpec(
        attempt_view=Path(target).expanduser().resolve(strict=False),
        staging_root=Path(target).expanduser().resolve(strict=False),
        shared_workspace=shared,
        owner_home=base,
        protected_persona_root=(
            Path(persona_text).expanduser().resolve(strict=False)
            if persona_text
            else None
        ),
        extra_write_roots=tuple(Path(r).expanduser().resolve(strict=False) for r in (write_roots or ())),
        public_read_roots=tuple(Path(r).expanduser().resolve(strict=False) for r in (read_roots or ())),
        protected_write_paths=tuple(
            Path(r).expanduser().resolve(strict=False)
            for r in (protected_write_paths or ())
        ),
        implicit_attempt_write_roots=write_roots is None,
        full_access=full_access,
    )
    sandbox = AttemptExecutionSandbox(spec)
    # Attempt 网关的 SandboxUnavailableError 继承 SandboxUnavailable，
    # 生产既有 except SandboxUnavailable → SANDBOX_UNAVAILABLE 捕获链直接生效。
    argv = sandbox.build_argv(strict_posix_shell_argv(command))
    return argv, False


# LLM: The explicit background command keeps the same sandbox argv as foreground
# execution, including bwrap --die-with-parent. A detached host becomes that
# parent so a one-shot child-agent runner may exit without killing the command.
# Windows owner-scoped execution remains fail-closed through _sandbox_exec.
# 函数用途: 由独立托管进程启动沙箱命令，并把输出持续写入指定日志。
def _spawn_background_process(
    command: str,
    target: Path,
    log_path: Path,
    owner_home: object = None,
    protected_persona_root: object = None,
    write_roots: tuple[Path, ...] | None = None,
    read_roots: tuple[Path, ...] | None = None,
    protected_write_paths: tuple[Path, ...] | None = None,
) -> HostedBackgroundProcess:
    if owner_home or protected_persona_root or os.name == "posix":
        exec_arg, use_shell = _sandbox_exec(
            command,
            target,
            owner_home,
            protected_persona_root,
            write_roots,
            read_roots,
            protected_write_paths,
        )
        if use_shell or not isinstance(exec_arg, list):
            raise OSError("managed background sandbox must provide argv execution")
        return start_background_process_host(
            exec_arg,
            cwd=target,
            log_path=log_path,
            env=_subprocess_text_env(owner_home),
            max_log_bytes=_MAX_BG_LOG_BYTES,
        )
    if os.name == "nt":
        return start_background_process_host(
            ["powershell.exe", "-NoProfile", "-Command", command],
            cwd=target,
            log_path=log_path,
            env=_subprocess_text_env(owner_home),
            max_log_bytes=_MAX_BG_LOG_BYTES,
        )
    from .sandbox import strict_posix_shell_argv

    return start_background_process_host(
        strict_posix_shell_argv(command),
        cwd=target,
        log_path=log_path,
        env=_subprocess_text_env(owner_home),
        max_log_bytes=_MAX_BG_LOG_BYTES,
    )


# background_jobs 登记台账上限:后台任务每启一个登记一条,原裸 append 永不回收 → 长跑无界增长
# (审计 #16)。有界 append 只留最近 N 条(纯观测/孤儿排查用,丢最旧可接受),磁盘恒定。
_MAX_BACKGROUND_JOB_RECORDS = 1000


# 函数用途: 把后台任务登记到 .background_jobs/registry.jsonl(供观测/孤儿排查;
#   纯辅助,登记失败不影响进程已启动的事实)。
def _record_background_job(
    jobs_dir: Path,
    pid: int,
    command: str,
    log_path: Path,
    *,
    session_id: str = "",
    process_pid: int = 0,
) -> None:
    record = {
        "pid": pid,
        "command": command[:200],
        "output_file": str(log_path),
        "session_id": str(session_id or ""),
        "process_pid": max(0, int(process_pid or 0)),
    }
    try:
        append_jsonl_capped(
            jobs_dir / "registry.jsonl", record, max_records=_MAX_BACKGROUND_JOB_RECORDS
        )
    except OSError:
        pass


# LLM: Durable registration is a fail-closed boundary. Production scopes receive
# one protected cross-process authority record; an unbound internal/test call stays
# process-local. If persistence fails, the exact managed host tree is terminated.
# 函数用途: 登记托管后台进程、写辅助观测记录；登记失败时立即回收，避免失管服务。
def _register_hosted_background_process(
    *,
    workspace_root: Path,
    owner_scope_root: object,
    jobs_dir: Path,
    log_path: Path,
    command: str,
    target: Path,
    hosted: HostedBackgroundProcess,
    access_scope: ProcessAccessScope,
) -> BackgroundProcess:
    store_root = (
        process_session_store_root(workspace_root, owner_scope_root)
        if access_scope.is_bound()
        else None
    )
    try:
        record = process_registry.register(
            ProcessRegistration(
                command=command,
                pid=hosted.process.pid,
                output_file=str(log_path),
                process=hosted.process,
                cwd=str(target),
                access_scope=access_scope,
                child_pid=hosted.child_pid,
                pid_birth_token=hosted.pid_birth_token,
                host_state_file=str(hosted.state_file),
                store_root=store_root,
            )
        )
    except (OSError, TypeError, ValueError):
        _kill_process_group(hosted.process)
        raise
    _record_background_job(
        jobs_dir,
        hosted.process.pid,
        command,
        log_path,
        session_id=record.session_id,
        process_pid=hosted.child_pid,
    )
    return record


# LLM: 模型说明必须把持久进程导向结构化 run_in_background，和 execute 的硬门保持一致。
# 函数用途: 构造模型可见的 run_command 参数、适用场景和安全使用提示。
def _build_shell_tool_model_spec(
    access_mode: str, default_timeout: int, max_output_chars: int
) -> ToolModelSpec:
    return ToolModelSpec(
        name="run_command",
        description=(
            "Execute one shell command in the workspace. For a server or other persistent "
            "process, pass run_in_background=true and keep the command itself in foreground "
            "form; shell '&' backgrounding is rejected because it cannot return a managed session. "
            "The tool already returns the final exit code; do not append '; echo $?' or another "
            "always-successful command because that masks an earlier failure."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": _MAX_COMMAND_CHARS,
                    "description": "Required shell command string, for example 'ls -la' or 'python build.py'.",
                },
                "timeout": {
                    "type": "integer",
                    "minimum": 0,
                    "description": f"Timeout in seconds; default {default_timeout}.",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Execution directory; defaults to this run's trusted effective cwd and must obey workspace access policy.",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "When true, start a managed background command and briefly observe startup. "
                        "If it remains alive, return status=started plus session_id/output_file; "
                        "if it exits immediately, return status=exited plus exit_code/output_tail."
                    ),
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="shell",
            use_cases=(
                "Run a project build script such as make or npm run.",
                "Inspect processes, ports, network state, or other system information.",
                "Execute a one-off script or command-line tool.",
                "脚本里需要调用 LLM（翻译/摘要/分类等）时：子进程环境自带 AGENT_API_KEY、AGENT_API_BASE、AGENT_MODEL_NAME（与本代理同款 anthropic 兼容端点），直接用它们初始化客户端，不要猜测其他服务商端点。",
            ),
            avoid_when=(
                "Use read_file / write_file when only file IO is needed.",
                "Avoid for interactive terminal workflows.",
                "Never append shell '&' or use 'nohup ... &' for a service; remove those wrappers, set run_in_background=true, then use process_session to wait, inspect, or stop it.",
                "Do not append '; echo $?' or another successful command to inspect status; run_command already reports the final return_code, and such suffixes hide earlier failures.",
                "Prefer write_file for file changes instead of shell redirection.",
                "Do not use rm/rmdir/unlink. Delete one text file with apply_patch; route directory or bulk deletion through task_trash.",
                "Files written under /tmp inside the owner-scoped sandbox are kept in the task workspace .sandbox-tmp directory and survive across tool calls and requests; still keep final deliverables in the selected workspace, not in /tmp.",
                "Do not run sleep or polling commands for subagent progress; end the turn and let the host lifecycle event resume the direct parent.",
                "盯守/轮询数据流→用 watch_stream,禁自写轮询脚本(无游标持久/覆盖账目,实测误报泛滥)。",
            ),
            keywords=("shell", "command", "terminal", "bash", "cmd", "script"),
            examples=(
                '{"tool": "run_command", "command": "ls -la"}',
                '{"tool": "run_command", "command": "python --version", "working_dir": "."}',
                '{"tool": "run_command", "command": "make build", "timeout": 60}',
                '{"tool": "run_command", "command": "python download_all.py", "run_in_background": true}',
            ),
        ),
    )


# LLM: shell 的 effect、sandbox、幂等、资源和补参必须由这一个 policy 工厂同步声明。
# 前台和 managed background 都由同一 owner-scoped sandbox argv 启动；后台只改变
# 生命周期/返回 session 的方式，不能再声明为 uncontained 而制造虚假授权。
# 函数用途: 构造 run_command 的唯一运行时策略；后台命令也保留完整沙箱和危险命令拦截。
def _build_shell_runtime_policy(default_timeout: int) -> ToolRuntimePolicy:
    return ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy(
            default_effect="dangerous",
            strategy="command",
            command_parameter="command",
            by_parameter=(("run_in_background", (("true", "dangerous"),)),),
        ),
        sandbox_policy=SandboxPolicy("required"),
        idempotency_policy=IdempotencyPolicy("operation"),
        timeout_policy=TimeoutPolicy(default_timeout),
        resource_scopes=ResourceScopePolicy(parameter_names=("working_dir",)),
        output_policy=OutputPolicy(trust="external_data"),
        input_policy=ToolInputPolicy(
            internal_parameters=(
                "__sandbox_write_roots",
                "__sandbox_read_roots",
                "__sandbox_protected_write_paths",
                "__access_mode",
                "__run_scope",
                "__tool_call_id",
                "__operation_id",
                "__operation_managed",
            ),
            safe_parameter_defaults=(
                ("timeout", default_timeout),
                ("run_in_background", False),
            ),
            trusted_parameter_bindings=(
                (
                    "working_dir",
                    TrustedParameterBinding(source_refs=("registry.effective_cwd",)),
                ),
            ),
        ),
        promotes_task=True,
        mutates_workspace=True,
    )


# LLM: ShellTool 是命令执行的唯一注册入口；后台参数必须在 runtime policy 中声明为 dangerous。
# 类用途: 在工作区内执行一次性命令，或启动可查看、可停止的受管后台进程。
class ShellTool(BaseTool):
    # LLM: model spec、effect、sandbox、幂等和受信补参必须在同一个 runtime policy 中同步构造。
    # 函数用途: 按工作区和访问选项初始化 shell 工具的展示 schema 与执行边界。
    def __init__(
        self,
        workspace_root: Path,
        *,
        options: ShellToolOptions | None = None,
    ):
        options = options or ShellToolOptions()
        self.workspace_root = workspace_root.resolve()
        self.workspace_roots = [
            root.resolve() for root in (options.workspace_roots or [self.workspace_root])
        ]
        self.path_access_policy = PathAccessPolicy.from_values(
            mode=options.path_access_mode,
            dangerous_roots=options.path_dangerous_roots,
            owner_scope_root=options.owner_scope_root,
        )
        self.access_mode = _normalize_access_mode(options.access_mode)
        self.protected_persona_root = str(options.protected_persona_root or "")
        self.artifact_backup_root = (
            Path(options.artifact_backup_root).expanduser().resolve(strict=False)
            if options.artifact_backup_root is not None
            else None
        )
        self.default_timeout = options.default_timeout
        self.max_output_chars = max(0, int(options.max_output_chars))
        self.model_spec = _build_shell_tool_model_spec(
            self.access_mode, self.default_timeout, self.max_output_chars
        )
        self.runtime_policy = _build_shell_runtime_policy(self.default_timeout)

    # seq 253 #5：bwrap 沙箱允许写全部 allowed_write_roots，执行写根不在
    # working_dir 参数里——经 effective_write_roots 协议结构化声明（与写边界
    # 校验同一 resolved_write_roots 解析器），operation lock 全量覆盖。
    def effective_write_roots(
        self,
        arguments: dict[str, Any],
        write_boundary: dict[str, Any] | None,
        workspace_root: Path,
    ) -> tuple[str, ...]:
        _ = (arguments, workspace_root)
        from .write_boundary import resolved_write_roots

        return tuple(str(p) for p in resolved_write_roots(write_boundary, workspace_root))

    # LLM: attempt 沙箱不可用时必须在本轮工具快照阶段消失；最终执行仍会二次
    # 复检并 fail-closed，不能把 availability 当作权限或安全替代品。
    # 函数用途: 防止模型看到当前节点必然无法启动的 run_command，再反复尝试同一失败。
    # G6：跨平台探测（Linux bwrap / macOS Seatbelt binary-only；单租户 POSIX 也
    # 要求沙箱 E.8）；Windows 单租户保留宿主 powershell，owner-scoped 由执行期 fail-closed。
    def availability(self) -> ToolAvailability:
        if os.name == "nt":
            if not self.path_access_policy.owner_scope_root:
                return ToolAvailability.ready()
            return ToolAvailability.unavailable(
                "owner-scoped run_command 要求当前执行节点提供 attempt 沙箱（Windows 不支持）",
                error_code="SANDBOX_UNAVAILABLE",
            )
        from ..attempt.sandbox import AttemptExecutionSandbox, AttemptSandboxSpec

        target = self.workspace_root
        owner_text = str(self.path_access_policy.owner_scope_root or "").strip()
        spec = AttemptSandboxSpec(
            attempt_view=target,
            staging_root=target,
            shared_workspace=Path(owner_text or target),
            owner_home=Path(owner_text or target),
            protected_persona_root=(
                Path(self.protected_persona_root).expanduser().resolve(strict=False)
                if self.protected_persona_root
                else None
            ),
            full_access=not bool(owner_text),
        )
        report = AttemptExecutionSandbox(spec).probe(binary_only=True)
        if report.ready:
            return ToolAvailability.ready()
        return ToolAvailability.unavailable(
            f"run_command 要求当前执行节点提供 attempt 沙箱（{report.detail}）",
            error_code="SANDBOX_UNAVAILABLE",
        )

    # LLM: shell 自带 & 不得进入前台或结构化后台执行；否则 shell 很快退出，模型会把
    #   一次临时探活误当成持久服务成功。拒绝结果必须声明 not_started，允许安全重试。
    # 函数用途: 校验并执行一条命令，长期进程统一登记为可查询、可终止的后台会话。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        command_result = self._parse_command(params)
        if isinstance(command_result, ToolHandlerOutcome):
            return command_result
        command = command_result
        command_policy = evaluate_command_policy(command, allow_shell_operators=True)
        if not command_policy.allowed:
            return ToolHandlerOutcome(
                self.model_spec.name,
                False,
                (
                    "危险命令被系统拒绝: "
                    f"codes={','.join(command_policy.finding_codes)} command={command[:80]}..."
                ),
                error_code="COMMAND_POLICY_BLOCKED",
            )
        if _contains_unmanaged_background_operator(command):
            return _unmanaged_background_result(self.model_spec.name)
        internal_status_ref = _internal_agent_status_command(command)
        if internal_status_ref is not None:
            return ToolHandlerOutcome(
                self.model_spec.name,
                False,
                json.dumps(internal_status_ref, ensure_ascii=False, indent=2),
                error_code="WRONG_STATUS_SURFACE",
                effect_outcome="not_started",
            )
        timeout = _timeout_from_params(params, self.default_timeout)
        if timeout <= 0:
            return ToolHandlerOutcome(
                self.model_spec.name,
                False,
                "TOOL_DEADLINE_EXCEEDED: 外层任务剩余时间不足，系统没有启动新的 shell 命令。",
                error_code="TOOL_TIMEOUT",
            )
        sandbox_write_roots = _sandbox_write_roots(params)
        sandbox_read_roots = _sandbox_read_roots(params)
        sandbox_protected_paths = _sandbox_protected_write_paths(params)
        target = self._execution_target(params, command)
        if isinstance(target, ToolHandlerOutcome):
            return target
        if _wants_background(params):
            return self._start_background_command(
                command,
                target,
                sandbox_write_roots,
                sandbox_read_roots,
                sandbox_protected_paths,
                process_access_scope(
                    params.get("__run_scope"),
                    self.path_access_policy.owner_scope_root,
                ),
            )
        return _execute_with_artifact_protection(
            _ShellArtifactExecutionRequest(
                tool=self,
                command=command,
                target=target,
                timeout=timeout,
                sandbox_roots=(
                    sandbox_write_roots,
                    sandbox_read_roots,
                    sandbox_protected_paths,
                ),
                effective_access_mode=_effective_access_mode(
                    self.access_mode,
                    params.get("__access_mode"),
                ),
                run_scope=params.get("__run_scope"),
                tool_call_id=str(params.get("__tool_call_id") or ""),
                operation_id=str(params.get("__operation_id") or ""),
                operation_managed=params.get("__operation_managed") is True,
            )
        )

    def _execution_target(
        self,
        params: dict[str, Any],
        command: str,
    ) -> Path | ToolHandlerOutcome:
        # LLM: Keep PTY and shell on the same structured cwd resolution helper.
        # 函数用途: 按本轮工作区和访问模式解析命令实际执行目录。
        _ = command
        return _shell_execution_target(self, params)

    @staticmethod
    def _failure_effect_outcome(
        command: str,
        ok: bool,
        error_code: str,
        output: str = "",
        process_facts: dict[str, object] | None = None,
    ) -> str:
        # LLM: Keep this stable seam for callers/tests while the classifier remains one helper.
        # 函数用途: 按结构化退出事实判断失败是否可安全重试或副作用是否未知。
        return _shell_failure_effect_outcome(
            command,
            ok,
            error_code,
            output,
            process_facts,
        )

    # LLM: Generic ToolOperation recovery delegates to the owner-private artifact manifest keyed by
    # the canonical operation_id. It never re-executes command text when the manifest says executing.
    # 函数用途: Gateway 重启或 shell 结果未知后核对同一操作的产物前像和已持久结果。
    def reconcile_operation(
        self,
        params: dict[str, Any],
        context: ToolOperationReconciliationContext,
    ) -> ToolOperationReconciliation:
        _ = params
        if self.artifact_backup_root is None:
            return ToolOperationReconciliation(
                outcome="unknown",
                reason="artifact_backup_store_unavailable",
            )
        try:
            facts = reconcile_shell_artifact_operation(
                self.artifact_backup_root,
                run_scope={
                    "owner_id": context.owner_id,
                    "run_id": context.run_id,
                    "task_id": context.task_id,
                },
                operation_id=context.operation_id,
            )
            payload = facts.get("result")
            result = (
                _shell_outcome_from_manifest(payload)
                if isinstance(payload, dict)
                else None
            )
            return ToolOperationReconciliation(
                outcome=str(facts.get("outcome") or "unknown"),
                source_ref=str(facts.get("source_ref") or ""),
                result=result,
                reason=str(facts.get("reason") or ""),
            )
        except (OSError, TypeError, ValueError):
            logger.exception("shell artifact operation reconciliation failed")
            return ToolOperationReconciliation(
                outcome="unknown",
                reason="artifact_operation_reconciliation_failed",
            )

    # LLM: Generic ToolOperation settlement is the authority that closes this manifest's crash
    # window. Preserve changed preimage blobs; remove only the now-redundant operation journal.
    # 函数用途: 在 run_command 的权威成功/失败终态落库后回收 owner 私有 shell 恢复清单。
    def on_operation_settled(
        self,
        params: dict[str, Any],
        context: ToolOperationSettlementContext,
    ) -> None:
        _ = params
        if self.artifact_backup_root is None:
            return
        settle_shell_artifact_operation(
            self.artifact_backup_root,
            run_scope={
                "owner_id": context.owner_id,
                "run_id": context.run_id,
                "task_id": context.task_id,
            },
            operation_id=context.operation_id,
        )

    # LLM: 正常退出必须同时返回结构化 process facts 和 `_display` 暂存；异常分支保持原错误合同，不能伪造 stdout/stderr。
    # 函数用途: 运行前台命令并将退出事实、模型文本和终端预览一次性整理出来。
    def _run_process_text(
        self,
        command: str,
        target: Path,
        timeout: int,
        sandbox_write_roots: tuple[Path, ...] | None,
        sandbox_read_roots: tuple[Path, ...] | None,
        sandbox_protected_paths: tuple[Path, ...] | None,
    ) -> tuple[str, bool, str, dict[str, object]]:
        return _run_shell_process_text(
            self,
            command,
            target,
            timeout,
            sandbox_write_roots,
            sandbox_read_roots,
            sandbox_protected_paths,
        )

    # LLM: Background execution is owned by a detached managed host, not the
    # one-shot agent runner. Model output exposes only the stable session handle;
    # host and command-entry PIDs remain private lifecycle facts because later
    # descendants may own the actual resource. Exact scope is persisted first.
    # stdin remains unsupported here because interactive processes belong to PTY.
    # 函数用途: 把命令放到当前用户会话的后台，马上返回，不堵住模型工具循环。
    def _start_background_command(
        self,
        command: str,
        target: Path,
        sandbox_write_roots: tuple[Path, ...] | None,
        sandbox_read_roots: tuple[Path, ...] | None,
        sandbox_protected_paths: tuple[Path, ...] | None,
        access_scope: ProcessAccessScope,
    ) -> ToolHandlerOutcome:
        try:
            jobs_dir = self.workspace_root / ".background_jobs"
            jobs_dir.mkdir(parents=True, exist_ok=True)
            log_path = jobs_dir / f"job-{time.time_ns()}.log"
            log_path.touch(exist_ok=False)
        except OSError as exc:
            return ToolHandlerOutcome(
                self.model_spec.name,
                False,
                f"COMMAND_FAILED: 后台日志创建失败: {exc}",
                error_code="COMMAND_FAILED",
            )
        try:
            hosted = _spawn_background_process(
                command,
                target,
                log_path,
                self.path_access_policy.owner_scope_root,
                self.protected_persona_root,
                sandbox_write_roots,
                sandbox_read_roots,
                sandbox_protected_paths,
            )
        except SandboxUnavailable as exc:
            return ToolHandlerOutcome(
                self.model_spec.name,
                False,
                f"SANDBOX_UNAVAILABLE: {exc}",
                error_code="SANDBOX_UNAVAILABLE",
            )
        except OSError as exc:
            return ToolHandlerOutcome(
                self.model_spec.name,
                False,
                f"COMMAND_FAILED: 后台启动失败: {exc}",
                error_code="COMMAND_FAILED",
            )
        try:
            record = _register_hosted_background_process(
                workspace_root=self.workspace_root,
                owner_scope_root=self.path_access_policy.owner_scope_root,
                jobs_dir=jobs_dir,
                log_path=log_path,
                command=command,
                target=target,
                hosted=hosted,
                access_scope=access_scope,
            )
        except (OSError, TypeError, ValueError) as exc:
            return ToolHandlerOutcome(
                self.model_spec.name,
                False,
                f"COMMAND_FAILED: 后台会话登记失败: {exc}",
                error_code="COMMAND_FAILED",
            )
        _LogSizeWatchdog(
            hosted.process,
            log_path,
            cancellation_token=current_cancellation_token(),
        ).start()  # 日志超上限或所属 turn 取消时终止完整进程组
        return _background_start_outcome(
            tool_name=self.model_spec.name,
            record=record,
            log_path=log_path,
        )

    def _parse_command(self, params: dict[str, Any]) -> str | ToolHandlerOutcome:
        return _parsed_command_or_error(self.model_spec.name, params.get("command", ""))

    def _run_command(
        self,
        command: str,
        target: Path,
        timeout: int,
        sandbox_write_roots: tuple[Path, ...] | None = None,
        sandbox_read_roots: tuple[Path, ...] | None = None,
        sandbox_protected_paths: tuple[Path, ...] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return _run_shell_command(
            self,
            command,
            target,
            timeout,
            sandbox_write_roots,
            sandbox_read_roots,
            sandbox_protected_paths,
        )


# LLM: Artifact manifests store the exact handler-level outcome needed by generic ToolOperation
# reconciliation. Only explicit ToolHandlerOutcome fields are serialized; runtime store facts remain external.
# 函数用途: 把 shell handler 结果转换为 owner 私有恢复清单中的纯数据。
def _shell_outcome_payload(outcome: ToolHandlerOutcome) -> dict[str, object]:
    return {
        "tool": outcome.tool,
        "ok": outcome.ok,
        "output": outcome.output,
        "call_id": outcome.call_id,
        "result_envelope": dict(outcome.result_envelope or {}),
        "error_code": outcome.error_code,
        "reported_error_code": outcome.reported_error_code,
        "effect_outcome": outcome.effect_outcome,
        "effect_source_ref": outcome.effect_source_ref,
        "handler_executed": outcome.handler_executed,
        "failure_stage": outcome.failure_stage,
        "duration_ms": outcome.duration_ms,
    }


# LLM: Rehydration is strict data construction; manifest prose cannot select a class, callback, or path.
# 函数用途: 从已校验的私有恢复清单重建一次 shell handler 结果。
def _shell_outcome_from_manifest(payload: dict[str, object]) -> ToolHandlerOutcome:
    if str(payload.get("tool") or "") != "run_command" or not isinstance(
        payload.get("result_envelope"),
        dict,
    ):
        raise ValueError("shell operation result manifest is invalid")
    return ToolHandlerOutcome(
        tool="run_command",
        ok=payload.get("ok") is True,
        output=str(payload.get("output") or ""),
        call_id=str(payload.get("call_id") or ""),
        result_envelope=dict(payload.get("result_envelope") or {}),
        error_code=str(payload.get("error_code") or ""),
        reported_error_code=str(payload.get("reported_error_code") or ""),
        effect_outcome=str(payload.get("effect_outcome") or ""),
        effect_source_ref=str(payload.get("effect_source_ref") or ""),
        handler_executed=payload.get("handler_executed") is True,
        failure_stage=str(payload.get("failure_stage") or ""),
        duration_ms=int(payload.get("duration_ms") or 0),
    )


# LLM: This foreground wrapper is outside ShellTool to keep the tool class below its size gate.
# It must preserve process facts and the exact owner-store artifact lifecycle in one outcome.
# 函数用途: 执行前后保护已登记产物，并把命令输出、沙箱范围和终端展示数据整理成一次结果。
def _execute_with_artifact_protection(
    request: _ShellArtifactExecutionRequest,
) -> ToolHandlerOutcome:
    prepared = _prepare_shell_artifact_execution(request)
    if isinstance(prepared, ToolHandlerOutcome):
        return prepared
    write_roots, read_roots, protected_paths = request.sandbox_roots
    try:
        output, ok, error_code, process_facts = request.tool._run_process_text(
            request.command,
            request.target,
            request.timeout,
            write_roots,
            read_roots,
            protected_paths,
        )
    except BaseException:
        release_shell_artifact_operation(prepared.operation_key)
        raise
    display = process_facts.pop("_display", None)
    output, artifact_summary, postcheck_failed = _postcheck_shell_artifacts(
        request,
        prepared,
        output,
    )
    outcome = _build_protected_shell_outcome(
        request,
        prepared,
        output=output,
        ok=ok,
        error_code=error_code,
        process_facts=process_facts,
        display=display,
        artifact_summary=artifact_summary,
        postcheck_failed=postcheck_failed,
    )
    return _commit_protected_shell_outcome(request, prepared, outcome)


# LLM: Every preimage and its durable prepared phase must exist before the process starts. Failure
# returns not_started and exposes only the opaque operation ref, never the owner-store path.
# 函数用途: 为一次前台 shell 准备已登记产物前像并推进私有恢复清单。
def _prepare_shell_artifact_execution(
    request: _ShellArtifactExecutionRequest,
) -> _ShellArtifactExecutionState | ToolHandlerOutcome:
    tool = request.tool
    write_roots, read_roots, _protected_paths = request.sandbox_roots
    source_roots = _artifact_source_roots(
        tool,
        write_roots,
        read_roots,
        effective_access_mode=request.effective_access_mode,
    )
    try:
        artifact_snapshots = snapshot_ready_artifacts(
            tool.workspace_root,
            tool.artifact_backup_root,
            source_roots=source_roots,
            run_scope=request.run_scope,
            tool_call_id=request.tool_call_id,
            operation_id=request.operation_id,
        )
    except (OSError, ValueError):
        logger.exception("artifact pre-backup failed before shell start")
        return ToolHandlerOutcome(
            tool.model_spec.name,
            False,
            "ARTIFACT_BACKUP_FAILED: shell 执行前无法安全备份已登记产物；命令未启动。",
            error_code="ARTIFACT_BACKUP_FAILED",
            effect_outcome="not_started",
        )
    summary: dict[str, Any] = {
        "snapshots": len(artifact_snapshots),
        "changed": [],
        "invalid": [],
        "cleanup_deferred": [],
    }
    if not artifact_snapshots:
        return _ShellArtifactExecutionState([], source_roots, summary, "", "")
    operation_key = artifact_snapshots[0].operation_key
    operation_ref = operation_manifest_ref(operation_key)
    try:
        if tool.artifact_backup_root is None:
            raise OSError("canonical owner artifact backup root is unavailable")
        mark_shell_artifact_operation_executing(
            tool.artifact_backup_root,
            artifact_snapshots,
        )
    except (OSError, ValueError):
        logger.exception("artifact operation could not enter executing state")
        return ToolHandlerOutcome(
            tool.model_spec.name,
            False,
            "ARTIFACT_BACKUP_FAILED: 产物恢复清单无法安全落盘；命令未启动。",
            error_code="ARTIFACT_BACKUP_FAILED",
            effect_outcome="not_started",
            effect_source_ref=operation_ref,
        )
    return _ShellArtifactExecutionState(
        artifact_snapshots,
        source_roots,
        summary,
        operation_key,
        operation_ref,
    )


# LLM: Postcheck is the sole bridge from process completion to artifact registry projection. Any
# structural read/write failure keeps the command effect unknown and retains all recovery bytes.
# 函数用途: 命令退出后复核所有受保护产物，并生成不泄露宿主路径的简短结果。
def _postcheck_shell_artifacts(
    request: _ShellArtifactExecutionRequest,
    prepared: _ShellArtifactExecutionState,
    output: str,
) -> tuple[str, dict[str, Any], bool]:
    summary = prepared.summary
    if prepared.snapshots:
        try:
            if request.tool.artifact_backup_root is None:
                raise OSError("canonical owner artifact backup root is unavailable")
            summary = reconcile_shell_artifact_operation_items(
                request.tool.workspace_root,
                prepared.snapshots,
                backup_store_root=request.tool.artifact_backup_root,
                source_roots=prepared.source_roots,
            )
        except (OSError, ValueError):
            logger.exception("artifact postcheck failed after shell exit")
            output = (
                f"{output}\nARTIFACT_POSTCHECK_FAILED: "
                "shell 已执行，但无法安全复核已登记产物；结果状态未知，需要人工检查。"
            )
            return output, summary, True
    protection_note = shell_artifact_protection_note(summary)
    if protection_note:
        output = f"{output}\n{protection_note}"
    return output, summary, False


# LLM: The result envelope exposes process and sandbox facts but never owner-private backup paths.
# Artifact postcheck failure overrides provider success and stays UNKNOWN for durable reconciliation.
# 函数用途: 把 shell 进程、沙箱和产物复核事实组装成统一工具结果。
def _build_protected_shell_outcome(
    request: _ShellArtifactExecutionRequest,
    prepared: _ShellArtifactExecutionState,
    *,
    output: str,
    ok: bool,
    error_code: str,
    process_facts: dict[str, Any],
    display: object,
    artifact_summary: dict[str, Any],
    postcheck_failed: bool,
) -> ToolHandlerOutcome:
    tool = request.tool
    owner_scoped = tool.path_access_policy.owner_scope_root is not None
    if owner_scoped:
        output = (
            f"{output}\n[sandbox_scope] file_scope=owner_workspace_only "
            "external_host_paths_hidden=true host_path_absence_proven=false\n"
            "当前命令只在本 owner 的隔离视图内运行；其中的 uid=0、/root 或其它绝对"
            "路径都不代表宿主权限，只有结构化授权写根内的结果会持久化到宿主。未挂载"
            "路径的不存在、拒绝或沙箱内成功都不能证明宿主路径状态。"
        )
    result_envelope: dict[str, object] = {
        "artifact_protection": artifact_summary,
        "process": process_facts,
        "sandbox": {
            "file_scope": "owner_workspace_only" if owner_scoped else "full_access",
            "external_host_paths_hidden": owner_scoped,
            "host_path_absence_proven": False,
        },
    }
    if isinstance(display, dict):
        result_envelope["display"] = display
    outcome = ToolHandlerOutcome(
        tool.model_spec.name,
        ok and not postcheck_failed,
        output,
        result_envelope=result_envelope,
        error_code=(
            "ARTIFACT_POSTCHECK_FAILED"
            if postcheck_failed
            else ("" if ok else error_code)
        ),
        effect_outcome=(
            "unknown"
            if postcheck_failed
            else tool._failure_effect_outcome(
                request.command, ok, error_code, output, process_facts
            )
        ),
        effect_source_ref=prepared.operation_ref,
    )
    return outcome


# LLM: The completed manifest closes the crash window before generic ToolOperation settlement.
# Unknown postcheck leaves the executing manifest intact and only releases the live-process marker.
# 函数用途: 持久化确定的 shell 结果，失败时保守返回未知并保留恢复材料。
def _commit_protected_shell_outcome(
    request: _ShellArtifactExecutionRequest,
    prepared: _ShellArtifactExecutionState,
    outcome: ToolHandlerOutcome,
) -> ToolHandlerOutcome:
    if not prepared.snapshots or request.tool.artifact_backup_root is None:
        return outcome
    if outcome.effect_outcome == "unknown":
        release_shell_artifact_operation(prepared.operation_key)
        return outcome
    try:
        complete_shell_artifact_operation(
            request.tool.artifact_backup_root,
            prepared.snapshots,
            _shell_outcome_payload(outcome),
        )
    except (OSError, TypeError, ValueError):
        logger.exception("artifact operation result could not be committed")
        release_shell_artifact_operation(prepared.operation_key)
        return ToolHandlerOutcome(
            request.tool.model_spec.name,
            False,
            f"{outcome.output}\nARTIFACT_POSTCHECK_FAILED: shell 已执行并完成产物复核，"
            "但恢复终态无法安全落盘；结果状态未知，需要人工检查。",
            result_envelope=outcome.result_envelope,
            error_code="ARTIFACT_POSTCHECK_FAILED",
            effect_outcome="unknown",
            effect_source_ref=prepared.operation_ref,
        )
    if not request.operation_managed:
        try:
            settle_shell_artifact_operation(
                request.tool.artifact_backup_root,
                run_scope=request.run_scope,
                operation_id="",
                operation_key=prepared.operation_key,
            )
        except (OSError, TypeError, ValueError):
            logger.warning("unmanaged shell artifact cleanup deferred", exc_info=True)
    return outcome


# LLM: Source roots come only from the immutable runtime boundary. Owner-scoped runs use the
# explicit sandbox read/write view; explicit local-admin Full Access is the sole unrestricted
# sentinel. A bare workspace-write tool stays bounded to its configured workspace roots.
# 函数用途: 计算 shell 前后允许宿主读取并保护的产物范围，阻止注册表伪造路径越权。
def _artifact_source_roots(
    tool: ShellTool,
    sandbox_write_roots: tuple[Path, ...] | None,
    sandbox_read_roots: tuple[Path, ...] | None,
    *,
    effective_access_mode: str,
) -> tuple[Path, ...] | None:
    owner_root = getattr(tool.path_access_policy, "owner_scope_root", None)
    boundary_supplied = sandbox_write_roots is not None or sandbox_read_roots is not None
    if not owner_root and effective_access_mode == "full-access" and not boundary_supplied:
        return None
    roots: list[Path] = []
    for value in (
        *(sandbox_write_roots or ()),
        *(sandbox_read_roots or ()),
    ):
        resolved = Path(value).expanduser().resolve(strict=False)
        if resolved not in roots:
            roots.append(resolved)
    if boundary_supplied:
        return tuple(roots)
    if owner_root:
        roots.append(Path(owner_root).expanduser().resolve(strict=False))
    if not roots:
        roots.extend(tool.workspace_roots or [tool.workspace_root])
    return tuple(roots)


# LLM: Shell and PTY must resolve the same trusted cwd and access override; command text never
# grants a directory or participates in containment decisions.
# 函数用途: 根据结构化工作目录参数和当前访问策略返回命令的规范执行目录。
def _shell_execution_target(
    tool: ShellTool,
    params: dict[str, Any],
) -> Path | ToolHandlerOutcome:
    effective_access_mode = _effective_access_mode(
        tool.access_mode,
        params.get("__access_mode"),
    )
    return _working_dir_from_params(
        params,
        tool.workspace_root,
        workspace_roots=tool.workspace_roots,
        path_access_policy=tool.path_access_policy,
        access_mode=effective_access_mode,
    )


# LLM: This classifier uses only typed process status, return code, stderr prefix, and the command
# policy parser. Natural-language error text must never decide retry safety or side effects.
# 函数用途: 将 shell 失败归为未启动、确定失败或结果未知，供上层选择是否安全重试。
def _shell_failure_effect_outcome(
    command: str,
    ok: bool,
    error_code: str,
    output: str = "",
    process_facts: dict[str, object] | None = None,
) -> str:
    _ = output
    if ok or not error_code:
        return ""
    if error_code == "TOOL_TIMEOUT":
        return "unknown"
    if error_code != "COMMAND_FAILED":
        return ""
    # exit 127 plus a shell-owned stderr prefix proves the target command never started.
    if isinstance(process_facts, dict) and int(
        process_facts.get("return_code") or 0
    ) == 127:
        stderr_head = str(process_facts.get("stderr_head") or "")
        if stderr_head.startswith(_SHELL_ERROR_PREFIXES):
            return "not_started"
    try:
        analysis = analyze_command(command)
    except Exception:  # noqa: BLE001 - 判定失败时保守沿用通用 unknown 合同
        return ""
    if analysis.resolved_effect == "read_only":
        return "not_started"
    if (
        isinstance(process_facts, dict)
        and str(process_facts.get("status") or "") == "exited"
        and process_facts.get("return_code") is not None
    ):
        return "failed"
    return ""


# LLM: Foreground execution returns one typed process envelope for every exit path. Timeout and
# cancellation preserve distinct codes so callers never infer them from rendered output.
# 函数用途: 运行前台命令并统一整理正常退出、超时、中断、沙箱缺失和启动失败结果。
def _run_shell_process_text(
    tool: ShellTool,
    command: str,
    target: Path,
    timeout: int,
    sandbox_write_roots: tuple[Path, ...] | None,
    sandbox_read_roots: tuple[Path, ...] | None,
    sandbox_protected_paths: tuple[Path, ...] | None,
) -> tuple[str, bool, str, dict[str, object]]:
    try:
        result = tool._run_command(
            command,
            target,
            timeout,
            sandbox_write_roots,
            sandbox_read_roots,
            sandbox_protected_paths,
        )
        output = _format_process_result(result, tool.max_output_chars)
        ok = result.returncode == 0
        if not ok:
            output += (
                f"\n[note] 退出码 {result.returncode} 非零"
                "(测试失败/grep无匹配/diff有差异等常见,非命令本身故障);"
                "看上方 stdout/stderr 定位修正,勿当工具不可用。"
            )
        stderr = str(getattr(result, "stderr", "") or "")
        return (
            output,
            ok,
            "" if ok else "COMMAND_FAILED",
            {
                "status": "exited",
                "return_code": int(result.returncode),
                "command_succeeded": ok,
                "stderr_chars": len(stderr),
                "stderr_head": stderr[:80],
                "_display": _command_display(result, tool.max_output_chars),
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


# LLM: The initial result distinguishes a managed process that is still alive from a command
# that already exited. This preserves the initial yield contract and prevents a session
# handle from being interpreted as service readiness; external reachability remains separate.
# 函数用途: 短暂等待后台命令的首个稳定状态；立即失败就原回合返回退出码和日志，仍运行才报告已启动。
def _background_start_outcome(
    *,
    tool_name: str,
    record: BackgroundProcess,
    log_path: Path,
) -> ToolHandlerOutcome:
    settled = process_registry.wait(
        record.session_id,
        _BACKGROUND_START_SETTLE_SECONDS,
        record.access_scope,
        record.store_root or None,
    )
    state = dict(settled or {})
    status = str(state.get("status") or "running").strip().lower()
    if status == "running":
        payload = {
            "status": "started",
            "session_id": record.session_id,
            "output_file": str(log_path),
            "startup_observation_seconds": _BACKGROUND_START_SETTLE_SECONDS,
            "hint": _background_session_hint(running=True),
        }
        return ToolHandlerOutcome(tool_name, True, json.dumps(payload, ensure_ascii=False))

    exit_code = _background_exit_code(state)
    payload = {
        "status": "exited",
        "session_id": record.session_id,
        "exit_code": exit_code,
        "output_tail": str(state.get("output_tail") or ""),
        "output_file": str(log_path),
        "startup_observation_seconds": _BACKGROUND_START_SETTLE_SECONDS,
        "hint": _background_session_hint(running=False),
    }
    if exit_code == 0:
        return ToolHandlerOutcome(tool_name, True, json.dumps(payload, ensure_ascii=False))
    return ToolHandlerOutcome(
        tool_name,
        False,
        json.dumps(payload, ensure_ascii=False),
        error_code="COMMAND_FAILED",
        effect_outcome="failed",
    )


# LLM: Exit code is a typed host lifecycle fact. Missing or malformed terminal state must not be
# converted to success, so the fallback remains a generic non-zero failure.
# 函数用途: 从后台终态安全读取退出码，缺失时按失败处理而不是说启动成功。
def _background_exit_code(state: dict[str, object]) -> int:
    try:
        return int(state.get("exit_code"))
    except (TypeError, ValueError):
        return -1


# LLM: Guidance explains the stable session handle without claiming port or network readiness.
# It is model context only and never participates in lifecycle or completion decisions.
# 函数用途: 给模型说明后台命令仍运行或已经退出时应该怎样汇报和继续核对。
def _background_session_hint(*, running: bool) -> str:
    if not running:
        return (
            "命令在启动观察期内已经退出；请按 exit_code 和 output_tail 如实汇报，"
            "不要说服务已启动。session_id 只用于追溯这次受管执行。"
        )
    return (
        "命令在启动观察期后仍在后台运行。session_id 是唯一稳定的管理标识；"
        "用 process_session 的 status/wait/list/stop 动作管理，不要猜测或操作系统 PID；"
        "需要结果时用 wait 有界等待，不要运行 sleep 轮询。"
        "也可以用 read_file 读取 output_file 的完整日志。"
        "如果这是网络服务，用 process_session network_status 核对真实监听 PID 和防火墙；"
        "进程仍运行不等于端口已监听，本机监听也不能证明局域网可达。"
    )


def _run_shell_command(
    tool: ShellTool,
    command: str,
    target: Path,
    timeout: int,
    sandbox_write_roots: tuple[Path, ...] | None,
    sandbox_read_roots: tuple[Path, ...] | None,
    sandbox_protected_paths: tuple[Path, ...] | None,
) -> subprocess.CompletedProcess[str]:
    # G6：POSIX（macOS/Linux）一律经 attempt 沙箱路径（单租户=full_access 档）；
    # Windows 单租户保留宿主 powershell（Attempt 网关不支持该平台，owner-scoped
    # 的 Windows 由沙箱路径 fail-closed）。
    if (
        tool.path_access_policy.owner_scope_root
        or tool.protected_persona_root
        or os.name == "posix"
    ):
        return _run_attempt_sandboxed_shell_command(
            tool,
            command,
            target,
            timeout,
            sandbox_write_roots,
            sandbox_read_roots,
            sandbox_protected_paths,
        )
    if os.name == "nt":
        proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-Command", command],
            cwd=str(target),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_subprocess_text_env(tool.path_access_policy.owner_scope_root),
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        return _communicate_process(proc, command=command, timeout=timeout)
    raise SandboxUnavailable(
        "SANDBOX_UNAVAILABLE: 当前平台无 attempt 沙箱实现（POSIX 之外仅支持 Windows 单租户宿主路径）"
    )


# LLM: 前台命令只能吃 attempt 沙箱 argv（G6：owner-scoped 与 POSIX 单租户统一）；
#   加载失败归一成 SandboxUnavailable（fail-closed，绝不回退宿主 shell）。
# 函数用途: 在 attempt 沙箱内运行前台命令并等待完成。
def _run_attempt_sandboxed_shell_command(
    tool: ShellTool,
    command: str,
    target: Path,
    timeout: int,
    sandbox_write_roots: tuple[Path, ...] | None,
    sandbox_read_roots: tuple[Path, ...] | None,
    sandbox_protected_paths: tuple[Path, ...] | None,
) -> subprocess.CompletedProcess[str]:
    owner_home = tool.path_access_policy.owner_scope_root
    exec_arg, use_shell = _sandbox_exec(
        command,
        target,
        owner_home,
        tool.protected_persona_root,
        sandbox_write_roots,
        sandbox_read_roots,
        sandbox_protected_paths,
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
