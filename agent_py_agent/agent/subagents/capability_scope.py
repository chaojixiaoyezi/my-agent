# LLM: Capability scope helpers keep routing grants/gaps bounded without executing tools.
# 模块用途: 从能力申请里提取 shell、MCP、路径、网络和输出预算范围，供父级路由生成受控授权或缺口记录。

from __future__ import annotations

"""Scoped capability routing helpers."""

from .model_capabilities import CapabilityRequest
from .model_task import SubAgentTask
from .services.lifecycle import RecordCapabilityGrantParams
from .utils import _merge_list


# LLM: request_scope_snapshot is refs-only metadata for grant/gap audit records.
# 函数用途: 把能力申请里的范围字段整理成普通字典，方便报告、授权和缺口记录追踪。
def request_scope_snapshot(request: CapabilityRequest) -> dict[str, object]:
    return {
        "capability_type": request.capability_type,
        "requested_tools": list(request.requested_tools),
        "requested_skills": list(request.requested_skills),
        "requested_mcp_tools": list(request.requested_mcp_tools),
        "requested_commands": list(request.requested_commands),
        "cwd_scope": list(request.cwd_scope),
        "path_scope": list(request.path_scope),
        "network_scope": list(request.network_scope),
        "output_budget": dict(request.output_budget),
        "risk_level": request.risk_level,
        "fallback_attempted": list(request.fallback_attempted),
        "escalation_target": request.escalation_target,
    }


# LLM: scoped_constraints keeps legacy string constraints useful for existing renderers.
# 函数用途: 把新范围字段投影进旧 constraints 字符串字典，避免旧报告视图看不到关键授权边界。
def scoped_constraints(request: CapabilityRequest) -> dict[str, str]:
    constraints = dict(request.constraints)
    if request.capability_type and request.capability_type != "generic":
        constraints.setdefault("capability_type", request.capability_type)
    if request.risk_level:
        constraints.setdefault("risk_level", request.risk_level)
    _set_csv_constraint(constraints, "requested_commands", request.requested_commands)
    _set_csv_constraint(constraints, "path_scope", request.path_scope)
    _set_csv_constraint(constraints, "network_scope", request.network_scope)
    return constraints


# LLM: grant_command_allowlist prefers explicit command requests and never infers shell from tool names.
# 函数用途: 生成父级授权给 shell gateway 的命令白名单；没有显式请求时保持空列表。
def grant_command_allowlist(request: CapabilityRequest) -> list[str]:
    return list(dict.fromkeys(request.requested_commands))


# LLM: grant_tools merges router hits with explicitly requested tool names for auditable scope.
# 函数用途: 合并路由命中的工具和申请中点名的工具，保持顺序去重。
def grant_tools(request: CapabilityRequest, routed_tools: list[str]) -> list[str]:
    return _merge_list(routed_tools, request.requested_tools)


# LLM: scoped_grant_params keeps SubAgentCapabilityMixin thin while preserving all grant scope fields.
# 函数用途: 将路由命中和能力申请合成授权参数包，后续新增授权字段集中在这里维护。
def scoped_grant_params(
    request: CapabilityRequest,
    *,
    routed_skills: list[str],
    routed_tools: list[str],
    selected_cards: list[dict[str, str]],
    hit_count: int,
) -> RecordCapabilityGrantParams:
    return RecordCapabilityGrantParams(
        request_id=request.id,
        skills=routed_skills,
        tools=grant_tools(request, routed_tools),
        mcp_tools=request.requested_mcp_tools,
        command_allowlist=grant_command_allowlist(request),
        grant_type=request.capability_type,
        capability_cards=selected_cards,
        reason=f"CapabilityRouter 命中 {hit_count} 张能力卡。",
        constraints=scoped_constraints(request),
        path_scope=request.path_scope or request.cwd_scope,
        network_scope=request.network_scope,
        output_budget=request.output_budget,
        risk_level=request.risk_level,
        expires_after_task=True,
        reserved={"request_scope": request_scope_snapshot(request)},
    )


# LLM: gap_attempted_tools keeps attempted shell commands visible as unresolved capability facts.
# 函数用途: 生成缺口记录里的已尝试工具列表，便于父级或后续 skill_spark 判断卡点。
def gap_attempted_tools(request: CapabilityRequest) -> list[str]:
    attempted = _merge_list(request.tried, request.requested_tools)
    return _merge_list(attempted, request.requested_commands)


# LLM: escalation_chain records who should see unresolved capability needs next.
# 函数用途: 生成缺口升级链，只写 run/owner/supervisor 引用，不主动通知或执行。
def escalation_chain(task: SubAgentTask, request: CapabilityRequest) -> list[str]:
    chain = [
        request.escalation_target,
        task.supervisor,
        task.owner,
        task.parent_id,
        task.root_id,
    ]
    return [item for item in dict.fromkeys(chain) if item]


# LLM: _set_csv_constraint is intentionally string-only for legacy constraint compatibility.
# 函数用途: 将列表范围字段压成逗号分隔字符串，供旧 constraints 读者显示。
def _set_csv_constraint(target: dict[str, str], key: str, values: list[str]) -> None:
    cleaned = [value for value in values if value]
    if cleaned:
        target.setdefault(key, ",".join(cleaned))
