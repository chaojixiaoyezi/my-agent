
from __future__ import annotations

"""Tool wrapper for grant-backed subagent exec requests."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..common.value_parsing import sequence_strings
from ..subagents.controlled_exec_gateway import (
    ControlledExecRequest,
    plan_controlled_exec,
    shell_request_from_controlled_exec,
)
from ..subagents.models import CapabilityGrant
from ..subagents.shell_gateway import decision_to_dict
from ..subagents.shell_gateway_execution import execute_shell_command
from ..subagents.task_trash import TaskTrashMoveRequest, move_to_task_trash
from .models import BaseTool, ToolExecutionResult, ToolSpec


@dataclass(frozen=True)
class ControlledExecToolRequest:
    params: dict[str, Any]
    workspace_root: Path
    write_boundary: dict[str, object] | None = None


class ControlledExecTool(BaseTool):
    spec = ToolSpec(
        name="controlled_exec",
        category="shell",
        effect="mutating",
        promotes_task=True,
        requires_idempotency=True,
        requires_approval=False,
        description="Plan or run a parent-granted shell command inside scoped task roots.",
        use_cases=[
            "Run a command only after the parent granted command/path/network scope.",
            "Inspect task-local files or tooling output without using unbounded shell access.",
        ],
        avoid_when=[
            "Do not use for ordinary file reads/writes when read_file/write_file can do it.",
            "Do not pass command_allowlist/path_scope in params; grants must come from parent context.",
        ],
        keywords=["controlled", "exec", "shell", "subagent", "grant", "command"],
        parameters={
            "command": "Command string or argv list to check.",
            "cwd": "Optional working directory, must stay inside the granted path scope.",
            "grant_id": "Optional parent grant id when multiple controlled exec grants exist.",
            "apply": "Optional boolean; false means dry-run planning only.",
        },
        parameter_details={
            "command": "Required. Example: 'pwd' or ['python3', '--version'].",
            "cwd": "Optional. Defaults to workspace root; parent path_scope still applies.",
            "grant_id": "Optional if there is exactly one controlled_exec grant in context.",
            "apply": "Optional. False returns dry-run plan; true runs only after grant checks pass.",
            "delete": "rm/rmdir/unlink intentionally stay out of shell allowlists; with apply=true they route to task_trash and return trash_manifest_ref.",
        },
        parameter_schema={
            "apply": {"type": "boolean"},
        },
        internal_parameters=["command_allowlist", "path_scope"],
        examples=[
            '{"tool":"controlled_exec","apply":true,"command":"pwd","cwd":"."}',
            '{"tool":"controlled_exec","apply":true,"grant_id":"grant-shell-1","command":["python3","-c","print(\'x\' * 2000)"]}',
        ],
    )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        return ToolExecutionResult(self.spec.name, False, "controlled_exec requires registry write_boundary", error_code="TOOL_EXECUTION_FAILED")


def execute_controlled_exec_tool(request: ControlledExecToolRequest) -> ToolExecutionResult:
    grant_ref, grant_error = _select_controlled_exec_grant(request.params, request.write_boundary)
    if grant_error:
        return ToolExecutionResult("controlled_exec", False, grant_error, error_code="WRITE_FORBIDDEN")
    command = request.params.get("command")
    if command is None:
        return ToolExecutionResult("controlled_exec", False, "controlled_exec requires command", error_code="TOOL_INVALID_ARGUMENTS")
    grant = _grant_from_ref(grant_ref)
    exec_request = ControlledExecRequest(
        command=command,
        workspace_root=request.workspace_root,
        grant=grant,
        cwd=request.params.get("cwd") or "",
        task_dir=_boundary_value(request.write_boundary, "task_dir"),
        artifact_dir=_boundary_value(request.write_boundary, "controlled_exec_artifact_dir"),
        apply=_bool_value(request.params.get("apply")),
    )
    plan = plan_controlled_exec(exec_request)
    if exec_request.apply and plan.action == "use_task_trash":
        trash_result = move_to_task_trash(
            TaskTrashMoveRequest(
                task_dir=exec_request.task_dir,
                source_path=plan.trash_hint.get("source_path") or "",
                allowed_roots=grant.path_scope,
                reason=plan.reason,
                actor_run_id=grant.grant_to_run_id,
            )
        )
        return ToolExecutionResult(
            "controlled_exec",
            trash_result.moved,
            _trash_payload(trash_result, grant.id, plan.reason),
        )
    if exec_request.apply and plan.allowed and plan.action == "execute_shell":
        execution = execute_shell_command(shell_request_from_controlled_exec(exec_request))
        ok = bool(execution.executed and execution.exit_code == 0 and not execution.timed_out)
        return ToolExecutionResult("controlled_exec", ok, _execution_payload(execution, grant.id))
    return ToolExecutionResult("controlled_exec", _plan_result_ok(plan), _plan_payload(plan, grant.id))


def _select_controlled_exec_grant(
    params: dict[str, Any],
    write_boundary: dict[str, object] | None,
) -> tuple[dict[str, object], str]:
    grants = _boundary_grants(write_boundary)
    if not grants:
        return {}, "controlled_exec requires parent grant in write_boundary.controlled_exec_grants"
    grant_id = str(params.get("grant_id") or "").strip()
    if grant_id:
        return _grant_by_id(grants, grant_id)
    unique_grants = _unique_grant_refs(grants)
    if len(unique_grants) == 1:
        return unique_grants[0], ""
    return {}, "controlled_exec requires grant_id when multiple parent grants exist"


def _grant_by_id(grants: list[dict[str, object]], grant_id: str) -> tuple[dict[str, object], str]:
    for grant in grants:
        if str(grant.get("grant_id") or "") == grant_id:
            return grant, ""
    return {}, f"controlled_exec grant not found: {grant_id}"


def _boundary_grants(write_boundary: dict[str, object] | None) -> list[dict[str, object]]:
    if not isinstance(write_boundary, dict):
        return []
    raw = write_boundary.get("controlled_exec_grants")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _unique_grant_refs(grants: list[dict[str, object]]) -> list[dict[str, object]]:
    unique: list[dict[str, object]] = []
    seen: set[tuple] = set()
    for grant in grants:
        key = _grant_ref_signature(grant)
        if key in seen:
            continue
        seen.add(key)
        unique.append(grant)
    return unique


def _grant_ref_signature(grant: dict[str, object]) -> tuple:
    return (
        tuple(sequence_strings(grant.get("command_allowlist"))),
        tuple(sequence_strings(grant.get("path_scope"))),
        tuple(sequence_strings(grant.get("network_scope"))),
        tuple(sorted(_output_budget(grant.get("output_budget")).items())),
    )


def _grant_from_ref(ref: dict[str, object]) -> CapabilityGrant:
    return CapabilityGrant(
        id=str(ref.get("grant_id") or ""),
        request_id=str(ref.get("request_id") or ""),
        grant_to_run_id=str(ref.get("run_id") or ""),
        grant_type="shell",
        tools=["controlled_exec"],
        command_allowlist=sequence_strings(ref.get("command_allowlist")),
        path_scope=sequence_strings(ref.get("path_scope")),
        network_scope=sequence_strings(ref.get("network_scope")),
        output_budget=_output_budget(ref.get("output_budget")),
        risk_level=str(ref.get("risk_level") or ""),
        constraints=_string_dict(ref.get("constraints")),
    )


def _plan_payload(plan, grant_id: str) -> str:
    decision = decision_to_dict(plan.shell_decision) if plan.shell_decision else {}
    payload = {
        "mode": "dry_run",
        "grant_id": grant_id,
        "allowed": plan.allowed,
        "action": plan.action,
        "reason": plan.reason,
        "blockers": list(plan.blockers or []),
        "shell_decision": decision,
        "trash_hint": dict(plan.trash_hint or {}),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _plan_result_ok(plan) -> bool:
    return bool(plan.allowed or plan.action == "use_task_trash")


def _execution_payload(execution, grant_id: str) -> str:
    payload = asdict(execution)
    decision = payload.get("decision")
    if isinstance(decision, dict):
        decision.pop("argv", None)
    return json.dumps(
        {
            "mode": "execute",
            "grant_id": grant_id,
            "allowed": bool(execution.decision.allowed),
            "action": "execute_shell",
            "execution": payload,
        },
        ensure_ascii=False,
        indent=2,
    )


def _trash_payload(trash_result, grant_id: str, reason: str) -> str:
    return json.dumps(
        {
            "mode": "task_trash",
            "grant_id": grant_id,
            "allowed": trash_result.moved,
            "action": "move_to_task_trash",
            "reason": reason or trash_result.reason,
            "trash": asdict(trash_result),
        },
        ensure_ascii=False,
        indent=2,
    )


def _output_budget(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return dict(value)


def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true"}


def _boundary_value(write_boundary: dict[str, object] | None, key: str) -> str:
    if not isinstance(write_boundary, dict):
        return ""
    return str(write_boundary.get(key) or "")
