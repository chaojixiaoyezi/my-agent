# LLM: Controlled shell gateway plans subagent shell usage before any subprocess execution exists.
# 模块用途: 为子代理提供受控 shell 干跑判断，校验命令白名单、cwd、路径范围、网络范围和输出预算。

from __future__ import annotations

"""Dry-run shell gateway for scoped subagent capability grants."""

from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent_py_agent.agent.contracts.gates.command_policy import (
    CommandPolicyDecision,
    command_name,
    evaluate_command_policy,
)


# LLM: ShellGatewayRequest is the stable bundle for all future shell dry-run/execute checks.
# 类用途: 集中保存一次 shell 网关请求，调用方必须显式传入 workspace、授权命令、网络范围和预算。
@dataclass(frozen=True)
class ShellGatewayRequest:
    command: str | list[str]
    workspace_root: str | Path
    cwd: str | Path = ""
    allowed_roots: list[str | Path] = field(default_factory=list)
    command_allowlist: list[str] = field(default_factory=list)
    network_allowlist: list[str] = field(default_factory=list)
    output_budget: dict[str, object] = field(default_factory=dict)
    request_id: str = ""
    run_id: str = ""
    dry_run: bool = True
    artifact_dir: str | Path = ""


# LLM: ShellGatewayDecision is refs-only approval data; allowed=True never means execution already happened.
# 类用途: 返回 shell 网关判断结果，说明是否可执行、阻断原因、解析后的 argv、cwd 和输出预算。
@dataclass
class ShellGatewayDecision:
    allowed: bool
    dry_run: bool
    would_execute: bool
    executable: str = ""
    argv: list[str] = field(default_factory=list)
    cwd: str = ""
    reason: str = ""
    blockers: list[str] = field(default_factory=list)
    output_budget: dict[str, object] = field(default_factory=dict)
    audit: dict[str, object] = field(default_factory=dict)


# LLM: plan_shell_command validates a command under a scoped grant but intentionally does not run it.
# 函数用途: 对 shell 请求做 dry-run 判断，返回允许或拒绝原因；当前阶段不创建进程、不写文件。
def plan_shell_command(request: ShellGatewayRequest) -> ShellGatewayDecision:
    command_policy = evaluate_command_policy(request.command)
    argv = list(command_policy.argv)
    workspace = Path(request.workspace_root).expanduser().resolve()
    cwd, cwd_error = _resolve_cwd(request.cwd, workspace)
    roots = _resolve_allowed_roots(workspace, request.allowed_roots)
    blockers = _collect_blockers(_BlockerCheck(request, argv, command_policy, cwd, cwd_error, roots))
    executable = command_name(argv[0]) if argv else ""
    allowed = not blockers
    budget = _normalize_output_budget(request.output_budget)
    return ShellGatewayDecision(
        allowed=allowed,
        dry_run=bool(request.dry_run),
        would_execute=allowed,
        executable=executable,
        argv=argv,
        cwd=str(cwd),
        reason="allowed_by_scoped_shell_gateway" if allowed else blockers[0],
        blockers=blockers,
        output_budget=budget,
        audit={
            "request_id": request.request_id,
            "run_id": request.run_id,
            "workspace_root": str(workspace),
            "allowed_roots": [str(root) for root in roots],
            "network_allowlist": list(request.network_allowlist),
            "dry_run": bool(request.dry_run),
            "command_policy_findings": [finding.to_dict() for finding in command_policy.findings],
        },
    )


# LLM: decision_to_dict keeps CLI/report callers from depending on dataclass internals.
# 函数用途: 将 shell 网关判断结果转成 JSON 友好的字典，方便后续写审计和报告。
def decision_to_dict(decision: ShellGatewayDecision) -> dict[str, object]:
    return asdict(decision)


# LLM: _BlockerCheck keeps dry-run policy bundled and below params-count limits.
# 类用途: 汇总一次 shell dry-run 阻断检查所需的解析结果、路径和授权根目录。
@dataclass(frozen=True)
class _BlockerCheck:
    request: ShellGatewayRequest
    argv: list[str]
    command_policy: CommandPolicyDecision
    cwd: Path
    cwd_error: str
    roots: list[Path]


# LLM: _collect_blockers centralizes dry-run policy so execute v1 can reuse the same gate.
# 函数用途: 汇总共享 command policy、白名单、cwd 和网络范围的阻断原因。
def _collect_blockers(check: _BlockerCheck) -> list[str]:
    blockers: list[str] = []
    blockers.extend(_command_policy_blockers(check.request, check.argv))
    if check.command_policy.findings:
        return [finding.code for finding in check.command_policy.findings]
    blockers.extend(_cwd_policy_blockers(check.cwd, check.cwd_error, check.roots))
    blockers.extend(_delete_target_policy_blockers(check.argv, check.cwd, check.roots))
    blockers.extend(_network_policy_blockers(check.request, check.argv))
    return blockers


# LLM: _command_policy_blockers requires explicit parent allowlist grants after shared policy passes.
# 函数用途: 共享 command policy 先 deny；通过后再校验父级授权白名单。
def _command_policy_blockers(request: ShellGatewayRequest, argv: list[str]) -> list[str]:
    if not argv:
        return ["COMMAND_EMPTY"]
    executable = command_name(argv[0])
    allowed = {command_name(item) for item in request.command_allowlist if str(item).strip()}
    allowed.update(str(item).strip() for item in request.command_allowlist if str(item).strip())
    if not allowed:
        return ["missing_command_allowlist"]
    if executable not in allowed and str(argv[0]) not in allowed:
        return [f"command_not_granted:{executable}"]
    return []


# LLM: _cwd_policy_blockers keeps subprocess cwd inside workspace-local allowed roots.
# 函数用途: 校验 cwd 是否存在、是否在 workspace 内、是否落在授权根目录里。
def _cwd_policy_blockers(cwd: Path, cwd_error: str, roots: list[Path]) -> list[str]:
    if cwd_error:
        return [cwd_error]
    if not cwd.exists() or not cwd.is_dir():
        return ["cwd_not_directory"]
    if not any(_is_relative_to(cwd, root) for root in roots):
        return ["cwd_outside_allowed_roots"]
    return []


# LLM: _delete_target_policy_blockers keeps granted rm/rmdir/unlink inside the scoped roots.
# 函数用途: 允许普通工作区清理，但拒绝删除授权根目录之外的显式目标。
def _delete_target_policy_blockers(argv: list[str], cwd: Path, roots: list[Path]) -> list[str]:
    if not argv or command_name(argv[0]) not in {"rm", "rmdir", "unlink"}:
        return []
    for raw_target in _delete_targets(argv[1:]):
        target = Path(raw_target).expanduser()
        resolved = target.resolve(strict=False) if target.is_absolute() else (cwd / target).resolve(strict=False)
        if not any(_is_relative_to(resolved, root) for root in roots):
            return [f"delete_target_outside_allowed_roots:{raw_target}"]
    return []


# LLM: _delete_targets filters flags and returns only path-like delete operands.
# 函数用途: 从 rm/rmdir/unlink 参数中提取路径目标，不把 -rf/--force 当路径。
def _delete_targets(args: list[str]) -> list[str]:
    targets: list[str] = []
    for arg in args:
        if arg == "--":
            continue
        if arg.startswith("-") and arg != "-":
            continue
        targets.append(arg)
    return targets


# LLM: _network_policy_blockers requires explicit network scope for curl-like commands.
# 函数用途: 对 curl 等网络命令检查 URL 是否落在授权网络范围；无 URL 的版本查询不阻断。
def _network_policy_blockers(request: ShellGatewayRequest, argv: list[str]) -> list[str]:
    if not argv or command_name(argv[0]) not in {"curl"}:
        return []
    urls = [item for item in argv[1:] if item.startswith(("http://", "https://"))]
    if not urls:
        return []
    allowed = [item.rstrip("/") for item in request.network_allowlist if item.strip()]
    if not allowed:
        return ["missing_network_allowlist"]
    for url in urls:
        normalized = url.rstrip("/")
        if not any(normalized.startswith(prefix) for prefix in allowed):
            return [f"network_scope_denied:{url}"]
    return []

# LLM: _resolve_cwd resolves cwd relative to workspace and blocks escape at the planning layer.
# 函数用途: 把 cwd 解析成绝对路径；未指定时使用 workspace 根目录。
def _resolve_cwd(cwd: str | Path, workspace: Path) -> tuple[Path, str]:
    if not str(cwd or "").strip():
        return workspace, ""
    candidate = Path(cwd).expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    if not _is_relative_to(path, workspace):
        return path, "cwd_outside_workspace"
    return path, ""


# LLM: _resolve_allowed_roots normalizes optional roots and always includes workspace as a safe default.
# 函数用途: 归一化授权根目录；没有传入时默认只允许 workspace 根目录。
def _resolve_allowed_roots(workspace: Path, allowed_roots: list[str | Path]) -> list[Path]:
    roots = []
    for raw in allowed_roots:
        candidate = Path(raw).expanduser()
        path = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
        if _is_relative_to(path, workspace):
            roots.append(path)
    return roots or [workspace]


# LLM: _normalize_output_budget clamps future execute output before any command can run.
# 函数用途: 标准化输出预算字段，避免大日志或大 stdout 后续被默认全量保存。
def _normalize_output_budget(value: dict[str, object]) -> dict[str, object]:
    return {
        "stdout_bytes": _positive_int(value.get("stdout_bytes"), 65536),
        "stderr_bytes": _positive_int(value.get("stderr_bytes"), 32768),
        "artifact_bytes": _positive_int(value.get("artifact_bytes"), 262144),
        "timeout_seconds": _positive_int(value.get("timeout_seconds"), 120),
    }


# LLM: _positive_int accepts config JSON values and falls back for invalid or unbounded inputs.
# 函数用途: 将预算值转成正整数；空值、负数和非法值使用安全默认值。
def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


# LLM: _is_relative_to preserves Python compatibility and avoids exception-heavy policy branches.
# 函数用途: 判断 path 是否在 root 下，供 cwd 和授权根目录检查复用。
def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
