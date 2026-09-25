# LLM: 普通 shell 关闭宿主 stdin，交互归独立 PTY；命令长度不是权限边界，不再额外卡短脚本。
# 捕获全文与模型预览分离，展示归档不得拿裁剪预览充全文；修改执行入口需联测参数、危险命令和 owner 沙箱。
# 输出行数按采集正文的 LF 分隔计算，模型回执、截断说明与展示事实必须共用口径。
# 后台访问身份由 process_scope 规范，不能拿其会话回退值推断任务执行归属。
# 前台从启动保留出生身份，普通退出先清理原组再回收组长；命令结果和清理未知分开，不影响显式后台。
# 模块用途: 在用户权限内执行命令、收回前台资源并整理输出及产物记录，保留原退出码和真实清理缺口。
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
from dataclasses import asdict, dataclass
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

from ..common.cancellation import (
    ToolCancelled,
    cancellation_requested,
    current_cancellation_token,
    register_cancellation_callback,
)
from .background_process_launch import (
    BACKGROUND_START_SETTLE_SECONDS,
    BackgroundLaunchError,
    BackgroundLaunchRequest,
    start_background_process,
)
from .listen_scope import DEFAULT_LISTEN_SCOPE, LISTEN_SCOPES, normalize_listen_scope
from .models import (
    ApprovalPolicy,
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
    ToolInvocationContext,
    ToolModelHints,
    ToolModelSpec,
    ToolOperationReconciliation,
    ToolOperationReconciliationContext,
    ToolOperationSettlementContext,
    ToolRuntimePolicy,
    TrustedParameterBinding,
)
from .process_output_capture import ProcessOutputCapture
from .process_registry import (
    BackgroundProcess,
    ProcessTerminationReceipt,
    capture_process_birth_token,
    finish_foreground_process,
    process_registry,
    terminate_process_tree,
)
from .process_scope import ProcessAccessScope, ProcessExecutionScope, process_access_scope
from .process_session_store import process_session_store_root
from .sandbox import SandboxUnavailable
from .shell_syntax import contains_unmanaged_background_operator

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
    # 后台服务声明 loopback 却绑到局域网地址时是否由 host 回收；False 只记 listener_warning。来自配置 background_process_listen_scope_enforce。
    listen_scope_enforce: bool = True


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


class CommandInterruptedError(RuntimeError):
    """Foreground command was cancelled by the owning conversation request."""


# LLM: 仅本地进程执行器构造该异常；普通 TimeoutExpired 不含终止证明，不能降级 UNKNOWN。
# 类用途: 将前台命令超时前输出和真实进程回收记录一并交回工具结果层。
class CommandTimeoutError(subprocess.TimeoutExpired):
    # LLM: 构造只携带已发生的执行事实，不执行命令或替模型决定重试。
    # 函数用途: 保存超时、输出、终止回执和管道是否已关闭，供主代理与恢复使用同一结果。
    def __init__(
        self, command: str, timeout: int, stdout: str, stderr: str,
        termination: ProcessTerminationReceipt, pipes_drained: bool,
    ) -> None:
        super().__init__(command, timeout, output=stdout, stderr=stderr)
        self.termination: ProcessTerminationReceipt = termination
        self.pipes_drained: bool = pipes_drained


# LLM: 仅校验非空文本，不按字符数量猜合法性；完整命令仍必须通过原安全策略和执行沙箱。
# 函数用途: 保留真实脚本文本并拒绝空输入，避免人工长度限制迫使模型重复拆改合法命令。
def _validate_command(command: str) -> str:
    if not command:
        raise ValueError("command 不能为空")
    text = command.strip()
    if not text:
        raise ValueError("command 不能为空或仅包含空白字符")
    return text


# LLM: 普通 Shell 与 PTY 共用此解析合同，修改时同步两个工具的测试；这里只转换空输入错误，
# 不执行命令、不截断或代写脚本，后续策略仍校验完整文本。
# 函数用途: 为普通命令和交互终端提供同一输入校验入口，缺少命令时返回明确参数问题。
def parse_shell_command(tool_name: str, raw_command: object) -> str | ToolHandlerOutcome:
    try:
        return _validate_command(str(raw_command or ""))
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
    granted_external_roots: tuple[Path, ...] | None = None,
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
        if not decision.allowed and _path_inside_any_root(target, granted_external_roots or ()):
            # LLM: 用户显式声明的工作目录（宿主写进 write_boundary 的墙外授权根）和文件工具
            #   走同一条逃生规则：危险目录、凭据文件、跨 owner 仍由无墙策略继续拦。
            # 人类: 少了这一步，子代理在用户指定的项目目录里连 pwd/ls 都跑不了。
            decision = PathAccessPolicy.from_values(
                mode=path_access_policy.mode,
                dangerous_roots=path_access_policy.dangerous_roots,
            ).check(target)
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


# LLM: 只统计已采集文本，不从预览或业务文件推断；末尾 LF 结束现有行，不新增空行，裸 CR 仍是同一行。
# 函数用途: 给模型回执、截断说明和展示记录提供一致行数；空输出为零，未换行的尾段算一行。
def _captured_line_count(text: str) -> int:
    return text.count("\n") + int(bool(text) and not text.endswith("\n"))


# LLM: 只裁剪模型预览；说明使用原采集文本的行数，归档仍由 _command_display 保留原文。
# 函数用途: 输出超限时保留开头和结尾，避免丢掉构建或测试在尾部给出的结论。
def _bounded_output(text: str, max_chars: int) -> tuple[str, bool]:
    """超限时保留头部+尾部、省略中段（构建/测试输出的结论通常在尾部）。"""
    if max_chars <= 0:
        return "", bool(text)
    if len(text) <= max_chars:
        return text, False
    head_chars = max(1, int(max_chars * 0.6))
    tail_chars = max(1, max_chars - head_chars)
    omitted = len(text) - head_chars - tail_chars
    marker = f"\n...[中段省略 {omitted} 字符，完整输出共 {len(text)} 字符 / {_captured_line_count(text)} 行]...\n"
    return text[:head_chars] + marker + text[-tail_chars:], True


# LLM: 模型正文的计数必须基于 CompletedProcess 原采集文本，与展示层同源；不能把预览截断当实际输出丢失。
# 函数用途: 生成有界命令回执，保留退出码、实际采集长度和行数，不修改进程输出。
def _format_process_result(result: subprocess.CompletedProcess[str], max_output_chars: int) -> str:
    stdout = result.stdout if result.stdout else ""
    stderr = result.stderr if result.stderr else ""
    stdout_preview, stdout_truncated = _bounded_output(stdout, max_output_chars)
    stderr_preview, stderr_truncated = _bounded_output(stderr, max_output_chars)
    return (
        f"return_code={result.returncode}\n"
        f"stdout_chars={len(stdout)} stdout_lines={_captured_line_count(stdout)} "
        f"stdout_preview_chars={len(stdout_preview)} "
        f"stdout_truncated={stdout_truncated}\n"
        f"stdout={stdout_preview}\n"
        f"stderr_chars={len(stderr)} stderr_preview_chars={len(stderr_preview)} "
        f"stderr_truncated={stderr_truncated}\n"
        f"stderr={stderr_preview}"
    )


# LLM: 保存当次 CompletedProcess 的采集全文，供统一 owner/thread archive 在事件预览前冻结；模型正文仍由 _format_process_result 限额，两者使用同一行数口径。
# 函数用途: 保留命令原始标准/错误输出和采集缺口；不能重读业务文件，也不能把采集上限外的字节称为已保存。
def _command_display(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    stdout = str(result.stdout or "")
    stderr = str(result.stderr or "")
    capture = getattr(result, "capture", {})
    capture = capture if isinstance(capture, dict) else {}
    seen, retained = capture.get("bytes_seen", {}), capture.get("bytes_retained", {})
    return {
        "kind": "command",
        "return_code": int(result.returncode) if result.returncode is not None else None,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_lines": _captured_line_count(stdout),
        "stderr_lines": _captured_line_count(stderr),
        "stdout_truncated": seen.get("stdout", 0) > retained.get("stdout", 0),
        "stderr_truncated": seen.get("stderr", 0) > retained.get("stderr", 0),
        "capture_complete": capture.get("complete") is not False,
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
_PROCESS_PIPE_DRAIN_SECONDS = 2.0
_FOREGROUND_EXIT_PROBE_SECONDS = 0.25


# LLM: 前台和后台共用原终止实现；前台传入启动时冻结的出生身份，不能凭旧 PID 或函数被调用就宣称清理成功。
# 函数用途: 终止指定命令的可观察进程树，并把身份与退出核对信息交回取消/超时路径。
def _kill_process_group(proc: subprocess.Popen, expected_birth_token: str | None = None) -> ProcessTerminationReceipt:
    return terminate_process_tree(proc.pid, proc, expected_birth_token=expected_birth_token)


# LLM: subprocess 的 TimeoutExpired 在 text 模式也可能携带 bytes；此处仅做展示解码。
# 函数用途: 将已捕获的输出统一为文本，保留中文并避免 None 或 bytes 破坏结果格式化。
def _process_output_text(value: str | bytes | None) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")


# LLM: 前台宿主与沙箱共用采集和终止；从启动冻结出生身份，管道 EOF 后也不能先 poll，必须先清理可验证的原组。
# EOF 后父仍活时限制内核探测频率；取消与 deadline 继续每轮检查，不把短期探测变成持久轮询器。
# 函数用途: 等待命令与管道完成并收回前台后代；保留原退出码、有限输出及独立清理回执，联测 foreground/orphan cleanup。
def _communicate_process(
    proc: subprocess.Popen[str],
    *,
    command: str,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    deadline = time.monotonic() + max(0.0, float(timeout))
    birth_token = capture_process_birth_token(proc.pid) if os.name != "nt" else None
    capture = ProcessOutputCapture(proc)
    next_exit_probe = 0.0
    with register_cancellation_callback(lambda: _kill_process_group(proc, birth_token)):
        while True:
            if is_interrupted() or cancellation_requested():
                _kill_process_group(proc, birth_token)
                capture.finish(_PROCESS_PIPE_DRAIN_SECONDS)
                raise CommandInterruptedError(command)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                termination = _kill_process_group(proc, birth_token)
                drained = capture.finish(_PROCESS_PIPE_DRAIN_SECONDS)
                out, err, facts = capture.result()
                error = CommandTimeoutError(command, timeout, out, err, termination, drained)
                error.capture = facts
                raise error
            code, termination = None, None
            if capture.done() and time.monotonic() >= next_exit_probe:
                code, termination = finish_foreground_process(proc, expected_birth_token=birth_token)
                next_exit_probe = time.monotonic() + _FOREGROUND_EXIT_PROBE_SECONDS
            if code is not None:
                out, err, facts = capture.result()
                if cancellation_requested():
                    raise CommandInterruptedError(command)
                result = subprocess.CompletedProcess(command, code, out, err)
                result.capture = facts
                result.termination = termination
                return result
            time.sleep(min(0.02, remaining))


# 函数用途: 判断 run_command 是否请求后台模式(布尔或 "true"/"1"/"yes" 字符串)。
def _wants_background(params: dict[str, Any]) -> bool:
    value = params.get("run_in_background")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes"}


# LLM: schema 已把取值限定在 LISTEN_SCOPES；这里只做缺省与规范化，未知值仍由 normalize 拒绝而不是静默放宽。
# 函数用途: 读出这次后台启动声明的监听范围，缺省 loopback。
def _background_listen_scope(params: dict[str, Any]) -> str:
    return normalize_listen_scope(params.get("background_listen_scope") or DEFAULT_LISTEN_SCOPE)


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


# LLM: 显式后台命令使用前台相同的已校验沙箱 argv；保留 bwrap die-with-parent，实际父进程为独立 host。
# 函数用途: 构造后台命令的系统执行参数，此处不启动进程或登记状态。
def _background_command_argv(
    command: str, target: Path, owner_home: object, protected_persona_root: object,
    write_roots: tuple[Path, ...] | None, read_roots: tuple[Path, ...] | None,
    protected_write_paths: tuple[Path, ...] | None,
) -> list[str]:
    if owner_home or protected_persona_root or os.name == "posix":
        argv, use_shell = _sandbox_exec(command, target, owner_home, protected_persona_root,
                                        write_roots, read_roots, protected_write_paths)
        if use_shell or not isinstance(argv, list):
            raise OSError("managed background sandbox must provide argv execution")
        return argv
    if os.name == "nt":
        return ["powershell.exe", "-NoProfile", "-Command", command]
    from .sandbox import strict_posix_shell_argv

    return strict_posix_shell_argv(command)


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


# LLM: 模型与执行使用同一 Schema；持久进程走结构化 run_in_background，命令不设人为字符上限。
# 安全策略、后台归属和时间预算各自保持，修改时联测模型快照与真实执行入口。
# 函数用途: 描述 Shell 工具可接受的参数，避免 Schema 提前挡住合法长脚本而与执行能力冲突。
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
                "background_listen_scope": {
                    "type": "string",
                    "enum": list(LISTEN_SCOPES),
                    "description": (
                        "Only with run_in_background=true. loopback (default): the service may listen on 127.0.0.1/::1 "
                        "only; the host recycles it if it binds a LAN address. lan: the user asked to expose the service to "
                        "the local network; the first such call asks the user once and can be remembered for this owner."
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
                "子进程不继承本代理模型或网关密钥。脚本需要独立外部服务时，使用用户为该用途显式配置的凭据；不要假设 AGENT_API_KEY 可用，也不要读取宿主私有配置。",
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
        # 开放局域网是可被用户长期允许的一类操作：审批面板多出"长期允许"，自主模式不擅自放行未授权的 lan。
        approval_policy=ApprovalPolicy(owner_grant_parameters=(("background_listen_scope", ("lan",)),)),
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
                "__process_completion_target",
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
        # LLM: owner 墙的逃生口只认宿主逐次下发的"墙外已授权工作根"（registry 从
        #   write_boundary 派生），不认 workspace_roots 这种可变列表；默认空 = 完全保持
        #   WorkspaceOnly 语义。
        # 人类: 用户显式 --workspace 声明到 owner home 之外的目录时，命令才能在那里执行。
        self.granted_external_roots: tuple[Path, ...] = ()
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
        self.listen_scope_enforce = bool(options.listen_scope_enforce)
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

    # LLM: callback 属于当前不可变调用上下文，不能写共享 handler 或注入模型参数。
    # 函数用途: 将原执行权限复查交给后台启动，在预留和交接时重新确认当前调用仍有效。
    def execute_scoped(self, params: dict[str, Any], context: ToolInvocationContext) -> ToolHandlerOutcome:
        return self.execute(params, context=context)

    # LLM: shell 自带 & 不得进入执行；deadline 已过等前置拒绝必须声明 not_started，不能遗留 UNKNOWN。
    # 函数用途: 校验并执行命令，未启动与执行后失败分账；长期进程统一登记为受管后台会话。
    def execute(self, params: dict[str, Any], *, context: ToolInvocationContext | None = None) -> ToolHandlerOutcome:
        command_result = parse_shell_command(self.model_spec.name, params.get("command", ""))
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
        if contains_unmanaged_background_operator(command):
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
                effect_outcome="not_started",
                result_envelope={"process": {"status": "not_started", "reason": "deadline_exceeded"}},
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
                (sandbox_write_roots, sandbox_read_roots, sandbox_protected_paths),
                process_access_scope(
                    params.get("__run_scope"),
                    self.path_access_policy.owner_scope_root,
                ),
                completion_target=params.get("__process_completion_target"),
                execution_scope=ProcessExecutionScope.from_run_scope(params.get("__run_scope"), self.path_access_policy.owner_scope_root),
                context=context,
                listen_scope=_background_listen_scope(params),
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

    # LLM: 先预留真实执行归属，再由 host 启动和明确交接；原回合取消只影响交接前，日志上限由 host 独占。
    # 函数用途: 启动可跨工作片管理的后台命令，返回稳定句柄及真实启动或清理结果。
    def _start_background_command(
        self, command: str, target: Path, sandbox_roots: _ShellSandboxRoots,
        access_scope: ProcessAccessScope, completion_target: dict[str, str] | None = None,
        *, execution_scope: ProcessExecutionScope | None = None, context: ToolInvocationContext | None = None,
        listen_scope: str = DEFAULT_LISTEN_SCOPE,
    ) -> ToolHandlerOutcome:
        try:
            argv = _background_command_argv(command, target, self.path_access_policy.owner_scope_root,
                                            self.protected_persona_root, *sandbox_roots)
            jobs_dir = self.workspace_root / ".background_jobs"
            jobs_dir.mkdir(parents=True, exist_ok=True)
            log_path = jobs_dir / f"job-{time.time_ns()}.log"
            log_path.touch(exist_ok=False)
            request = BackgroundLaunchRequest(
                argv=argv, command=command, cwd=target, log_path=log_path,
                env=_subprocess_text_env(self.path_access_policy.owner_scope_root), max_log_bytes=_MAX_BG_LOG_BYTES,
                store_root=process_session_store_root(self.workspace_root, access_scope.owner_home),
                access_scope=access_scope, execution_scope=execution_scope or ProcessExecutionScope(owner_home=access_scope.owner_home),
                completion_target=dict(completion_target or {}), authority_check=context.execution_authority_check if context else None,
                listen_scope=listen_scope, listen_scope_enforce=self.listen_scope_enforce,
            )
            hosted = start_background_process(request)
        except BackgroundLaunchError as exc:
            return _background_launch_failure(self.model_spec.name, exc)
        except (OSError, TypeError, ValueError, SandboxUnavailable) as exc:
            return ToolHandlerOutcome(self.model_spec.name, False, f"后台命令未启动：{type(exc).__name__}",
                                      error_code="SANDBOX_UNAVAILABLE" if isinstance(exc, SandboxUnavailable) else "COMMAND_FAILED",
                                      effect_outcome="not_started")
        try:
            record = process_registry.attach(hosted.record, hosted.process, hosted.store_root)
            _record_background_job(jobs_dir, record.pid, command, log_path, session_id=record.session_id, process_pid=record.child_pid)
            return _background_start_outcome(tool_name=self.model_spec.name, record=record, log_path=log_path,
                                             listen_scope=listen_scope)
        except (OSError, RuntimeError, ValueError) as exc:
            # 已交接资源仍归持久 session，查询故障不能把它当未启动或回滚。
            return ToolHandlerOutcome(self.model_spec.name, False,
                                      json.dumps({"status": "unknown", "session_id": hosted.record["session_id"], "error_type": type(exc).__name__}),
                                      error_code="TOOL_OPERATION_OUTCOME_UNKNOWN", effect_outcome="unknown")


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


# LLM: 当前可保护产物的前像和 prepared 必须先落盘；历史缺失/越界引用只带计数，不能扩大读取权限或阻断无关命令。
# 函数用途: 为一次前台 shell 准备安全范围内的产物前像，保留备份失败关闭和私有恢复清单。
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
    selection_counts: dict[str, int] = {}
    try:
        artifact_snapshots = snapshot_ready_artifacts(
            tool.workspace_root,
            tool.artifact_backup_root,
            source_roots=source_roots,
            run_scope=request.run_scope,
            tool_call_id=request.tool_call_id,
            operation_id=request.operation_id,
            selection_counts=selection_counts,
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
        "skipped_sources": selection_counts,
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


# LLM: 退出后只复核本次实际快照；保留选择计数，不能把跳过的历史引用算为已保护或当前命令破坏。
# 函数用途: 复核命令涉及的受保护产物并保留过时引用诊断；真正复核失败仍报告效果未知。
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
            summary["skipped_sources"] = prepared.summary.get("skipped_sources", {})
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
    if skipped := summary.get("skipped_sources"):
        output += (
            f"\n[artifact_protection] skipped_sources={json.dumps(skipped, sort_keys=True)}; "
            "旧引用缺失或不在本次读取范围，未读取或修改这些路径；仅保护当前范围内实际存在的产物。"
        )
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
# explicit sandbox read/write view; admin Full Access with no write restriction keeps its
# unrestricted sentinel even when the registry projects an empty supplemental read-root list.
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
    if (
        not owner_root
        and effective_access_mode == "full-access"
        and sandbox_write_roots is None
        and not sandbox_read_roots
    ):
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
        granted_external_roots=tuple(getattr(tool, "granted_external_roots", ()) or ()),
    )


# LLM: 只读取执行器的进程状态、退出码和终止回执；普通退出后的清理未知不能被零退出码或 COMMAND_FAILED 掩盖。
# 函数用途: 区分未启动、已终止的失败与未知执行；命令失败仍可能有部分写入，不等同于可自动重放。
def _shell_failure_effect_outcome(
    command: str,
    ok: bool,
    error_code: str,
    output: str = "",
    process_facts: dict[str, object] | None = None,
) -> str:
    _ = command, output
    if ok or not error_code:
        return ""
    if error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN":
        return "unknown"
    if error_code == "TOOL_TIMEOUT":
        facts = process_facts or {}
        termination = facts.get("termination")
        if (
            facts.get("status") == "timed_out"
            and facts.get("pipes_drained") is True
            and isinstance(termination, dict)
            and termination.get("confirmed") is True
            and termination.get("return_code") is not None
        ):
            return "failed"
        return "unknown"
    if error_code != "COMMAND_FAILED":
        return ""
    if (
        isinstance(process_facts, dict)
        and str(process_facts.get("status") or "") == "exited"
        and process_facts.get("return_code") is not None
    ):
        return "failed"
    return ""


# LLM: 命令退出码、成功标志与清理回执分开；清理未确认使工具保持 UNKNOWN，不能改写原命令结果或自动重放。
# 函数用途: 运行前台命令并保留预览与有界采集原文；命令成功也要明确报告未收回的进程资源。
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
        capture = getattr(result, "capture", {})
        if capture and not capture.get("complete", True):
            output += "\n[输出不完整] 管道采集达到保留上限或读取失败；如需完整内容，请将命令输出重定向到文件后分页读取。"
        command_succeeded = result.returncode == 0
        termination = getattr(result, "termination", None)
        cleanup_unknown = termination is not None and not termination.confirmed
        if not command_succeeded:
            output += (
                f"\n[note] 退出码 {result.returncode} 非零"
                "(测试失败/grep无匹配/diff有差异等常见,非命令本身故障);"
                "看上方 stdout/stderr 定位修正,勿当工具不可用。"
            )
        if cleanup_unknown:
            output += "\n[进程清理未确认] 命令已退出，但原前台进程资源尚未确认收回；保留本次结果，不自动重放命令。"
        stderr = str(getattr(result, "stderr", "") or "")
        return (
            output,
            command_succeeded and not cleanup_unknown,
            "TOOL_OPERATION_OUTCOME_UNKNOWN" if cleanup_unknown else "" if command_succeeded else "COMMAND_FAILED",
            {
                "status": "exited",
                "return_code": int(result.returncode),
                "command_succeeded": command_succeeded,
                **({"termination": asdict(termination)} if termination is not None else {}),
                "stderr_chars": len(stderr),
                "capture": capture,
                "stderr_head": stderr[:80],
                "_display": _command_display(result),
            },
        )
    except subprocess.TimeoutExpired as exc:
        output, facts = _shell_timeout_result(exc, command, timeout, tool.max_output_chars)
        return output, False, "TOOL_TIMEOUT", facts
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


# LLM: 只有 CommandTimeoutError 的回执证明终止；所有 timeout 保留已采集display，未确认排空必须标记不完整。
# 函数用途: 超时后保存当次输出供展开排查，仍保持模型预览限额与未知终止事实，不重跑命令。
def _shell_timeout_result(exc, command: str, timeout: int, max_output_chars: int) -> tuple[str, dict]:
    facts: dict[str, object] = {"status": "timed_out", "timeout_seconds": timeout}
    if getattr(exc, "capture", None):
        facts["capture"] = exc.capture
    code = None
    if isinstance(exc, CommandTimeoutError):
        facts.update(termination=asdict(exc.termination), pipes_drained=exc.pipes_drained)
        code = exc.termination.return_code
    result = subprocess.CompletedProcess(
        command, code, _process_output_text(exc.output), _process_output_text(exc.stderr)
    )
    result.capture = dict(facts.get("capture") or {})
    if not isinstance(exc, CommandTimeoutError) or not exc.pipes_drained:
        result.capture["complete"] = False
    facts["_display"] = _command_display(result)
    output = f"TOOL_TIMEOUT: 命令执行超时 timeout ({timeout}s)\n"
    output += _format_process_result(result, max_output_chars)
    output += "\n[note] 超时不是成功；命令可能已经改动部分文件，请根据输出与现有结果排查，不自动重放。"
    return output, facts


# LLM: 启动方已完成观察和交接，这里只读当前事实；缺失、未知和 killed 不冒充启动成功或自然退出。
# 函数用途: 返回稳定后台句柄、真实短命令退出码或未知结果，不把句柄等同服务就绪。
def _background_start_outcome(
    *,
    tool_name: str,
    record: BackgroundProcess,
    log_path: Path,
    listen_scope: str = DEFAULT_LISTEN_SCOPE,
) -> ToolHandlerOutcome:
    settled = process_registry.status(record.session_id, record.access_scope, record.store_root)
    state = dict(settled or {})
    status = str(state.get("status") or "unknown")
    if status == "running":
        payload = {
            "status": "started",
            "session_id": record.session_id,
            "output_file": str(log_path),
            "listen_scope": listen_scope,
            "startup_observation_seconds": BACKGROUND_START_SETTLE_SECONDS,
            "hint": _background_session_hint(running=True),
        }
        return ToolHandlerOutcome(tool_name, True, json.dumps(payload, ensure_ascii=False))

    if status not in {"exited", "killed"} or type(state.get("exit_code")) is not int:
        payload = {"status": status, "session_id": record.session_id, "output_file": str(log_path)}
        return ToolHandlerOutcome(tool_name, False, json.dumps(payload, ensure_ascii=False),
                                  error_code="TOOL_OPERATION_OUTCOME_UNKNOWN", effect_outcome="unknown")
    exit_code = state["exit_code"]
    payload = {
        "status": status,
        "session_id": record.session_id,
        "exit_code": exit_code,
        "output_tail": str(state.get("output_tail") or ""),
        "output_file": str(log_path),
        "startup_observation_seconds": BACKGROUND_START_SETTLE_SECONDS,
        "hint": _background_session_hint(running=False),
    }
    # host 自己结清的终态带结构化原因；监听范围越界另给稳定错误码，让模型知道该改绑定地址还是走 lan 授权。
    for key in ("reason", "listen_scope", "listener_violation"):
        if state.get(key):
            payload[key] = state[key]
    if status == "exited" and exit_code == 0:
        return ToolHandlerOutcome(tool_name, True, json.dumps(payload, ensure_ascii=False))
    violated = status == "killed" and state.get("reason") == "listen_scope_violation"
    return ToolHandlerOutcome(
        tool_name,
        False,
        json.dumps(payload, ensure_ascii=False),
        error_code="BACKGROUND_LISTEN_SCOPE_VIOLATION" if violated else "COMMAND_FAILED",
        effect_outcome="failed",
    )


# LLM: 业务创建标记和终态决定副作用结果，不能因异常或清理完成就假装没有启动。
# 函数用途: 将启动失败映射为工具回执，保留原句柄、取消类型和清理是否确认。
def _background_launch_failure(tool_name: str, error: BackgroundLaunchError) -> ToolHandlerOutcome:
    record = error.record
    status = str(record.get("status") or "unknown")
    effect = "unknown"
    if error.cleanup_confirmed:
        effect = "not_started" if not record.get("child_launch_started") else "failed" if status in {"killed", "exited"} else "unknown"
    if effect in {"unknown", "not_started"}:
        status = effect
    code = "CANCELLED" if isinstance(error.cause, ToolCancelled) else "TOOL_OPERATION_OUTCOME_UNKNOWN" if effect == "unknown" else "COMMAND_FAILED"
    payload = {"session_id": record.get("session_id"), "status": status,
               "cleanup_confirmed": error.cleanup_confirmed, "error_type": type(error.cause).__name__}
    if not record.get("revision"):
        payload.pop("session_id", None)
    if error.cleanup_error:
        payload["cleanup_error"] = error.cleanup_error
    return ToolHandlerOutcome(tool_name, False, json.dumps(payload, ensure_ascii=False), error_code=code,
                              effect_outcome=effect, result_envelope={"process": payload})


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


# LLM: 普通前台命令必须是独立的非交互执行，不能继承宿主终端输入；POSIX 经 attempt 沙箱，Windows 单租户保持同一 stdin 语义。
# 函数用途: 选择平台执行路径并等待命令完成；管道和重定向由命令自身提供输入，人工交互使用 PTY 会话。
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
            stdin=subprocess.DEVNULL,
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


# LLM: 前台命令只能吃 attempt 沙箱 argv；显式关闭继承 stdin，防止 Gateway/TUI 输入被抢读；加载失败不回退宿主 shell。
# 函数用途: 在 attempt 沙箱内运行非交互命令并等待完成；无显式输入时得到 EOF，而不是白等宿主终端四分钟。
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
            stdin=subprocess.DEVNULL,
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
