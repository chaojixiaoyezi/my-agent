
from __future__ import annotations

"""Dry-run shell gateway for scoped subagent capability grants."""

from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent_py_agent.agent.contracts.gates.command_policy import (
    CommandPolicyDecision,
    command_name,
    evaluate_command_policy,
)


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


def decision_to_dict(decision: ShellGatewayDecision) -> dict[str, object]:
    return asdict(decision)


@dataclass(frozen=True)
class _BlockerCheck:
    request: ShellGatewayRequest
    argv: list[str]
    command_policy: CommandPolicyDecision
    cwd: Path
    cwd_error: str
    roots: list[Path]


def _collect_blockers(check: _BlockerCheck) -> list[str]:
    blockers: list[str] = []
    blockers.extend(_command_policy_blockers(check.request, check.argv))
    if check.command_policy.findings:
        return [finding.code for finding in check.command_policy.findings]
    blockers.extend(_cwd_policy_blockers(check.cwd, check.cwd_error, check.roots))
    blockers.extend(_delete_target_policy_blockers(check.argv, check.cwd, check.roots))
    blockers.extend(_network_policy_blockers(check.request, check.argv))
    return blockers


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


def _cwd_policy_blockers(cwd: Path, cwd_error: str, roots: list[Path]) -> list[str]:
    if cwd_error:
        return [cwd_error]
    if not cwd.exists() or not cwd.is_dir():
        return ["cwd_not_directory"]
    if not any(_is_relative_to(cwd, root) for root in roots):
        return ["cwd_outside_allowed_roots"]
    return []


def _delete_target_policy_blockers(argv: list[str], cwd: Path, roots: list[Path]) -> list[str]:
    if not argv or command_name(argv[0]) not in {"rm", "rmdir", "unlink"}:
        return []
    for raw_target in _delete_targets(argv[1:]):
        target = Path(raw_target).expanduser()
        resolved = target.resolve(strict=False) if target.is_absolute() else (cwd / target).resolve(strict=False)
        if not any(_is_relative_to(resolved, root) for root in roots):
            return [f"delete_target_outside_allowed_roots:{raw_target}"]
    return []


def _delete_targets(args: list[str]) -> list[str]:
    targets: list[str] = []
    for arg in args:
        if arg == "--":
            continue
        if arg.startswith("-") and arg != "-":
            continue
        targets.append(arg)
    return targets


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

def _resolve_cwd(cwd: str | Path, workspace: Path) -> tuple[Path, str]:
    if not str(cwd or "").strip():
        return workspace, ""
    candidate = Path(cwd).expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    if not _is_relative_to(path, workspace):
        return path, "cwd_outside_workspace"
    return path, ""


def _resolve_allowed_roots(workspace: Path, allowed_roots: list[str | Path]) -> list[Path]:
    roots = []
    for raw in allowed_roots:
        candidate = Path(raw).expanduser()
        path = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
        if _is_relative_to(path, workspace):
            roots.append(path)
    return roots or [workspace]


def _normalize_output_budget(value: dict[str, object]) -> dict[str, object]:
    return {
        "stdout_bytes": _positive_int(value.get("stdout_bytes"), 65536),
        "stderr_bytes": _positive_int(value.get("stderr_bytes"), 32768),
        "artifact_bytes": _positive_int(value.get("artifact_bytes"), 262144),
        "timeout_seconds": _positive_int(value.get("timeout_seconds"), 120),
    }


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
