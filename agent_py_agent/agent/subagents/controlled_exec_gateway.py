
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .models import CapabilityGrant
from .shell_gateway import ShellGatewayDecision, ShellGatewayRequest, plan_shell_command

_DELETE_COMMANDS = frozenset({"rm", "rmdir", "unlink"})


@dataclass(frozen=True)
class ControlledExecRequest:
    command: str | list[str]
    workspace_root: str | Path
    grant: CapabilityGrant
    cwd: str | Path = ""
    task_dir: str | Path = ""
    artifact_dir: str | Path = ""
    apply: bool = False


@dataclass
class ControlledExecPlan:
    allowed: bool
    action: str
    reason: str = ""
    blockers: list[str] = field(default_factory=list)
    shell_decision: ShellGatewayDecision | None = None
    trash_hint: dict[str, str] = field(default_factory=dict)


def controlled_exec_grant_refs(grants: list[CapabilityGrant]) -> list[dict[str, object]]:
    return [_controlled_exec_grant_ref(grant) for grant in grants if _is_controlled_exec_grant(grant)]


def _is_controlled_exec_grant(grant: CapabilityGrant) -> bool:
    if grant.grant_type == "shell":
        return True
    tools = {str(item).strip() for item in grant.tools if str(item).strip()}
    return "controlled_exec" in tools and bool(grant.command_allowlist or grant.path_scope)


def shell_request_from_controlled_exec(request: ControlledExecRequest) -> ShellGatewayRequest:
    return _shell_request_from_grant(request)


def plan_controlled_exec(request: ControlledExecRequest) -> ControlledExecPlan:
    argv, parse_error = _parse_command(request.command)
    if parse_error:
        return _blocked(parse_error)
    if delete := _delete_action_plan(request, argv):
        return delete
    if request.grant.grant_type != "shell":
        return _blocked(f"unsupported_grant_type:{request.grant.grant_type}")
    if not request.grant.path_scope:
        return _blocked("missing_parent_path_scope")
    decision = plan_shell_command(_shell_request_from_grant(request))
    if decision.allowed:
        return ControlledExecPlan(True, "execute_shell", decision.reason, shell_decision=decision)
    return ControlledExecPlan(False, "blocked", decision.reason, decision.blockers, decision)


def _controlled_exec_grant_ref(grant: CapabilityGrant) -> dict[str, object]:
    return {
        "grant_id": grant.id,
        "request_id": grant.request_id,
        "run_id": grant.grant_to_run_id,
        "command_allowlist": list(grant.command_allowlist or []),
        "path_scope": list(grant.path_scope or []),
        "network_scope": list(grant.network_scope or []),
        "output_budget": dict(grant.output_budget or {}),
        "risk_level": grant.risk_level,
        "constraints": dict(grant.constraints or {}),
        "delete_policy": {
            "mode": "task_trash",
            "commands": sorted(_DELETE_COMMANDS),
            "requires_apply": True,
            "command_allowlist_required": False,
            "completion_requires": ["moved=true", "trash_manifest_ref"],
        },
    }


def _shell_request_from_grant(request: ControlledExecRequest) -> ShellGatewayRequest:
    grant = request.grant
    return ShellGatewayRequest(
        command=request.command,
        workspace_root=request.workspace_root,
        cwd=request.cwd,
        allowed_roots=grant.path_scope,
        command_allowlist=grant.command_allowlist,
        network_allowlist=grant.network_scope,
        output_budget=grant.output_budget,
        request_id=grant.request_id,
        run_id=grant.grant_to_run_id,
        dry_run=not request.apply,
        artifact_dir=request.artifact_dir,
    )


def _delete_action_plan(request: ControlledExecRequest, argv: list[str]) -> ControlledExecPlan | None:
    executable = Path(argv[0]).name.lower() if argv else ""
    if executable not in _DELETE_COMMANDS:
        return None
    if not str(request.task_dir or "").strip():
        return _blocked(f"delete_requires_task_trash:{executable}", action="use_task_trash")
    hint = {
        "task_dir": str(Path(request.task_dir).expanduser().resolve()),
        "source_path": _delete_source_path(request, argv),
        "replacement": "move_to_task_trash",
    }
    return ControlledExecPlan(
        False,
        "use_task_trash",
        f"delete_requires_task_trash:{executable}",
        [f"delete_requires_task_trash:{executable}"],
        trash_hint=hint,
    )


def _delete_source_arg(argv: list[str]) -> str:
    for item in argv[1:]:
        if not item.startswith("-"):
            return item
    return ""


def _delete_source_path(request: ControlledExecRequest, argv: list[str]) -> str:
    raw = _delete_source_arg(argv)
    if not raw:
        return ""
    source = Path(raw).expanduser()
    if source.is_absolute():
        return str(source)
    base = Path(request.cwd or request.task_dir or request.workspace_root).expanduser()
    return str((base / source).resolve())


def _parse_command(command: str | list[str]) -> tuple[list[str], str]:
    if isinstance(command, list):
        argv = [str(item) for item in command if str(item).strip()]
        return argv, "" if argv else "empty_command"
    try:
        argv = shlex.split(str(command))
    except ValueError as exc:
        return [], f"command_parse_error:{exc}"
    return argv, "" if argv else "empty_command"


def _blocked(reason: str, *, action: str = "blocked") -> ControlledExecPlan:
    return ControlledExecPlan(False, action, reason, [reason])
