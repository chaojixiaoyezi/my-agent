# LLM: Controlled exec tool exposes shell planning only through parent-supplied write_boundary grants.
# 模块用途: 给子代理提供受控 exec 工具入口；工具调用参数只传命令，授权范围必须来自父级 grant。

from __future__ import annotations

"""Tool wrapper for grant-backed subagent exec requests."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

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


# LLM: ControlledExecToolRequest keeps registry internals separate from model-controlled JSON params.
# 类用途: 打包 registry 注入的工作区、写入边界和模型工具参数，避免工具执行接口继续变宽。
@dataclass(frozen=True)
class ControlledExecToolRequest:
    params: dict[str, Any]
    workspace_root: Path
    write_boundary: dict[str, object] | None = None


# LLM: ControlledExecTool is a catalog-visible shell gateway; execution is handled with registry context.
# 类用途: 向模型展示 controlled_exec 的参数和用途；真实执行入口需要 registry 注入 write_boundary。
class ControlledExecTool(BaseTool):
    spec = ToolSpec(
        name="controlled_exec",
        category="shell",
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
        examples=[
            '{"tool":"controlled_exec","apply":true,"command":"pwd","cwd":"."}',
            '{"tool":"controlled_exec","apply":true,"grant_id":"grant-shell-1","command":["python3","-c","print(\'x\' * 2000)"]}',
        ],
    )

    # LLM: execute protects callers that bypass registry context by failing closed.
    # 函数用途: 直接调用时拒绝执行，提醒必须走 registry 注入父级授权边界。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        return ToolExecutionResult(self.spec.name, False, "controlled_exec requires registry write_boundary")


# LLM: execute_controlled_exec_tool is the registry-aware dry-run entry point.
# 函数用途: 根据 write_boundary 中的父级 grant 规划命令；不相信模型参数里的授权字段。
def execute_controlled_exec_tool(request: ControlledExecToolRequest) -> ToolExecutionResult:
    grant_ref, grant_error = _select_controlled_exec_grant(request.params, request.write_boundary)
    if grant_error:
        return ToolExecutionResult("controlled_exec", False, grant_error)
    command = request.params.get("command")
    if command is None:
        return ToolExecutionResult("controlled_exec", False, "controlled_exec requires command")
    grant = _grant_from_ref(grant_ref)
    exec_request = ControlledExecRequest(
        command=command,
        workspace_root=request.workspace_root,
        grant=grant,
        cwd=request.params.get("cwd") or request.params.get("working_dir") or "",
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


# LLM: _select_controlled_exec_grant reads only parent-injected grant refs from write_boundary.
# 函数用途: 按 grant_id 选择父级授权；没有父级授权时拒绝模型自填 scope。
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


# LLM: _grant_by_id keeps grant selection flat and exact-match only.
# 函数用途: 从父级 grant refs 里按 id 查找受控 exec 授权；找不到时返回明确错误。
def _grant_by_id(grants: list[dict[str, object]], grant_id: str) -> tuple[dict[str, object], str]:
    for grant in grants:
        if str(grant.get("grant_id") or "") == grant_id:
            return grant, ""
    return {}, f"controlled_exec grant not found: {grant_id}"


# LLM: _boundary_grants normalizes write_boundary grant refs without accepting model-authored fallbacks.
# 函数用途: 提取父级写入边界里的 controlled_exec_grants 列表，过滤非字典项。
def _boundary_grants(write_boundary: dict[str, object] | None) -> list[dict[str, object]]:
    if not isinstance(write_boundary, dict):
        return []
    raw = write_boundary.get("controlled_exec_grants")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


# LLM: _unique_grant_refs lets duplicate persisted grants behave like a single grant.
# 函数用途: 当同一能力申请被重复记录成多个等价 grant 时，无 grant_id 调用仍可安全选择唯一授权边界。
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


# LLM: _grant_ref_signature compares execution boundaries rather than transient grant ids.
# 函数用途: 判断两个 grant 是否给出相同命令、路径、网络和输出预算，避免重复授权影响工具易用性。
def _grant_ref_signature(grant: dict[str, object]) -> tuple:
    return (
        tuple(_string_list(grant.get("command_allowlist"))),
        tuple(_string_list(grant.get("path_scope"))),
        tuple(_string_list(grant.get("network_scope"))),
        tuple(sorted(_output_budget(grant.get("output_budget")).items())),
    )


# LLM: _grant_from_ref reconstructs CapabilityGrant from refs-only boundary data for policy reuse.
# 函数用途: 把父级 grant ref 转成受控 exec gateway 需要的 CapabilityGrant。
def _grant_from_ref(ref: dict[str, object]) -> CapabilityGrant:
    return CapabilityGrant(
        id=str(ref.get("grant_id") or ""),
        request_id=str(ref.get("request_id") or ""),
        grant_to_run_id=str(ref.get("run_id") or ""),
        grant_type="shell",
        tools=["controlled_exec"],
        command_allowlist=_string_list(ref.get("command_allowlist")),
        path_scope=_string_list(ref.get("path_scope")),
        network_scope=_string_list(ref.get("network_scope")),
        output_budget=_output_budget(ref.get("output_budget")),
        risk_level=str(ref.get("risk_level") or ""),
        constraints=_string_dict(ref.get("constraints")),
    )


# LLM: _plan_payload keeps tool output bounded, JSON-readable, and free of stdout/stderr bodies.
# 函数用途: 渲染 controlled_exec dry-run 结果，供模型继续决策或上报阻断原因。
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


# LLM: _plan_result_ok treats task-trash dry-runs as valid next-step plans, not hard failures.
# 函数用途: 删除类命令的 dry-run 虽不允许 shell 执行，但应告诉模型“走 trash 替代”，避免继续申请裸 rm/mv。
def _plan_result_ok(plan) -> bool:
    return bool(plan.allowed or plan.action == "use_task_trash")


# LLM: _execution_payload returns execution metadata and output refs without embedding large streams.
# 函数用途: 渲染 controlled_exec 执行结果，只包含 stdout/stderr 预览、截断标记和审计引用。
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


# LLM: _trash_payload reports deletion replacement results without exposing moved file contents.
# 函数用途: 渲染 rm/rmdir/unlink 被替换为 task trash move 后的审计结果。
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


# LLM: _output_budget accepts old max_* names and the shell gateway's current canonical budget names.
# 函数用途: 兼容父级 grant 的输出预算字段，避免未来格式小改就需要重写工具逻辑。
def _output_budget(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    budget = dict(value)
    if "stdout_bytes" not in budget and "max_stdout_bytes" in budget:
        budget["stdout_bytes"] = budget["max_stdout_bytes"]
    if "stderr_bytes" not in budget and "max_stderr_bytes" in budget:
        budget["stderr_bytes"] = budget["max_stderr_bytes"]
    return budget


# LLM: _string_list makes persisted JSON values safe for CapabilityGrant construction.
# 函数用途: 把列表字段转成字符串列表，丢弃空值和非列表值。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


# LLM: _string_dict keeps constraints JSON-shaped without trusting arbitrary nested params as code.
# 函数用途: 复制字符串键值约束；非字典时返回空字典。
def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


# LLM: _bool_value interprets model JSON booleans conservatively for apply mode.
# 函数用途: 将 apply 参数转成布尔值，只有明确 true/yes/1 才会进入真实执行。
def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "apply", "execute", "run", "full"}


# LLM: _boundary_value extracts registry-injected path refs only from write_boundary.
# 函数用途: 安全读取 task_dir/artifact_dir 等父级注入路径，缺失时返回空字符串。
def _boundary_value(write_boundary: dict[str, object] | None, key: str) -> str:
    if not isinstance(write_boundary, dict):
        return ""
    return str(write_boundary.get(key) or "")
