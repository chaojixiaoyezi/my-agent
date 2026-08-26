
from __future__ import annotations

# LLM: capability grant 只给 command/path/network scope，不得代替全局 ToolExecutor 的用户审批。
# 模块用途: 把子代理获批的精确命令和路径范围投影成可计划、可执行的受控命令工具。
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
from .models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    SandboxPolicy,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)


@dataclass(frozen=True)
class ControlledExecToolRequest:
    params: dict[str, Any]
    workspace_root: Path
    write_boundary: dict[str, object] | None = None


# LLM: apply=true 会直接进入 subprocess.Popen，当前没有 bwrap/OS sandbox；即使已有父级 grant 也必须单独审批。
# 类用途: 让子代理在父级限定的命令和目录内先预览计划，获得用户批准后再真正执行。
class ControlledExecTool(BaseTool):
    model_spec = ToolModelSpec(
        name="controlled_exec",
        description="Plan or run a parent-granted shell command inside scoped task roots.",
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "description": "必填。要检查或执行的命令字符串或 argv 字符串数组。",
                    "anyOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    ],
                },
                "cwd": {
                    "type": "string",
                    "description": "可选工作目录，必须位于父级授权 path_scope 内。",
                },
                "grant_id": {
                    "type": "string",
                    "description": "存在多个 controlled_exec grant 时指定父级 grant_id。",
                },
                "apply": {
                    "type": "boolean",
                    "description": "可选。false 只返回计划；true 在全部授权门通过后执行。",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="shell",
            use_cases=(
                "Run a command only after the parent granted command/path/network scope.",
                "Inspect task-local files or tooling output without using unbounded shell access.",
            ),
            avoid_when=(
                "Do not use for ordinary file reads/writes when read_file/write_file can do it.",
                "Do not pass command_allowlist/path_scope in params; grants must come from parent context.",
            ),
            keywords=("controlled", "exec", "shell", "subagent", "grant", "command"),
            examples=(
                '{"tool":"controlled_exec","apply":true,"command":"pwd","cwd":"."}',
                '{"tool":"controlled_exec","apply":true,"grant_id":"grant-shell-1","command":["python3","-c","print(\'x\' * 2000)"]}',
            ),
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy(
            "mutating",
            by_parameter=(("apply", (("false", "read_only"), ("true", "dangerous"))),),
        ),
        # controlled_exec 的范围 grant 是授权围栏，不是 OS sandbox；
        # execute_shell_command 直接 Popen，所以不能用 sandbox 免掉 apply 审批。
        sandbox_policy=SandboxPolicy("none"),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(parameter_names=("cwd",)),
        input_policy=ToolInputPolicy(
            internal_parameters=("command_allowlist", "path_scope"),
        ),
        promotes_task=True,
        mutates_workspace=True,
    )

    # seq 253 #5：controlled_exec 真实写边界来自父级授权 grant.path_scope
    # （cwd 之外可写授权根）——只锁 resource_scopes 的 cwd 参数会漏掉 grant
    # 授权根；经协议声明全部写根，operation lock 与写边界共用同一 grant 选择器。
    def effective_write_roots(
        self,
        arguments: dict[str, Any],
        write_boundary: dict[str, Any] | None,
        workspace_root: Path,
    ) -> tuple[str, ...]:
        ref, _error = _select_controlled_exec_grant(arguments, write_boundary)
        roots: list[str] = []
        for raw in sequence_strings(ref.get("path_scope")):
            path = Path(raw)
            if not path.is_absolute():
                path = workspace_root / path
            roots.append(str(path.resolve()))
        return tuple(roots)

    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        return ToolHandlerOutcome(self.model_spec.name, False, "controlled_exec requires registry write_boundary", error_code="TOOL_EXECUTION_FAILED")


def execute_controlled_exec_tool(request: ControlledExecToolRequest) -> ToolHandlerOutcome:
    grant_ref, grant_error = _select_controlled_exec_grant(request.params, request.write_boundary)
    if grant_error:
        return ToolHandlerOutcome("controlled_exec", False, grant_error, error_code="WRITE_FORBIDDEN")
    command = request.params.get("command")
    if command is None:
        return ToolHandlerOutcome("controlled_exec", False, "controlled_exec requires command", error_code="TOOL_INVALID_ARGUMENTS")
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
        return ToolHandlerOutcome(
            "controlled_exec",
            trash_result.moved,
            _trash_payload(trash_result, grant.id, plan.reason),
        )
    if exec_request.apply and plan.allowed and plan.action == "execute_shell":
        execution = execute_shell_command(shell_request_from_controlled_exec(exec_request))
        if execution.cancelled:
            return ToolHandlerOutcome(
                "controlled_exec",
                False,
                _execution_payload(execution, grant.id),
                error_code="CANCELLED",
            )
        ok = bool(execution.executed and execution.exit_code == 0 and not execution.timed_out)
        return ToolHandlerOutcome("controlled_exec", ok, _execution_payload(execution, grant.id))
    return ToolHandlerOutcome("controlled_exec", _plan_result_ok(plan), _plan_payload(plan, grant.id))


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
