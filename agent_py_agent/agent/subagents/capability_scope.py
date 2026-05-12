# LLM: Capability scope helpers keep routing grants/gaps bounded without executing tools.
# 模块用途: 从能力申请里提取 shell、MCP、路径、网络和输出预算范围，供父级路由生成受控授权或缺口记录。

from __future__ import annotations

"""Scoped capability routing helpers."""

import shlex

from .model_capabilities import CapabilityRequest
from .model_task import SubAgentTask
from .services.lifecycle import RecordCapabilityGrantParams
from .utils import _merge_list

_DELETE_COMMAND_REQUESTS = frozenset({"rm", "rmdir", "unlink"})


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


# LLM: grant_command_allowlist normalizes model-provided command strings to shell gateway base commands.
# 函数用途: 生成父级授权给 shell gateway 的命令白名单；完整命令会归一成首个可执行名，删除类命令不进 shell，只能走 task trash。
def grant_command_allowlist(request: CapabilityRequest) -> list[str]:
    return [
        item for item in dict.fromkeys(_requested_command_name(item) for item in request.requested_commands)
        if item and item.lower() not in _DELETE_COMMAND_REQUESTS
    ]


# LLM: existing_delete_trash_grant reuses controlled_exec grants for rm-only follow-up requests.
# 函数用途: 如果模型误把 rm/rmdir/unlink 当成 shell 白名单缺口，但任务已有 controlled_exec grant，则复用旧 grant 让工具走 task_trash。
def existing_delete_trash_grant(task: SubAgentTask, request: CapabilityRequest):
    if not _delete_only_request(request):
        return None
    for grant in getattr(task, "capability_grants", []) or []:
        if _grant_supports_delete_trash(grant, request):
            return grant
    return None


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


# LLM: _requested_command_name is intentionally syntax-only and never executes or expands commands.
# 函数用途: 从模型申请的命令字符串中提取 shell gateway 实际检查的可执行名；解析失败时退回第一个空白分隔片段。
def _requested_command_name(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    return str(parts[0]).strip() if parts else ""


# LLM: _delete_only_request distinguishes trash-routable deletes from normal shell command needs.
# 函数用途: 只有申请命令全是删除类命令时才触发既有 controlled_exec grant 复用，避免误放行其他 shell。
def _delete_only_request(request: CapabilityRequest) -> bool:
    commands = [_requested_command_name(item).lower() for item in request.requested_commands]
    commands = [item for item in commands if item]
    return bool(commands) and all(item in _DELETE_COMMAND_REQUESTS for item in commands)


# LLM: _grant_supports_delete_trash checks parent-controlled grant scope without widening permissions.
# 函数用途: 确认已有 grant 明确包含 controlled_exec/shell 和路径范围，且覆盖当前删除申请的 path_scope。
def _grant_supports_delete_trash(grant, request: CapabilityRequest) -> bool:
    tools = {str(item).strip() for item in getattr(grant, "tools", []) or []}
    if "controlled_exec" not in tools and str(getattr(grant, "grant_type", "") or "") != "shell":
        return False
    if not getattr(grant, "path_scope", None):
        return False
    return _path_scope_covers(getattr(grant, "path_scope", []), request.path_scope)


# LLM: _path_scope_covers is exact and conservative; it never turns parent paths into broader roots.
# 函数用途: 当前申请没写 path_scope 时接受已有任务范围；写了 path_scope 时必须被已有 grant 精确覆盖。
def _path_scope_covers(grant_scope: list[str], request_scope: list[str]) -> bool:
    requested = {str(item).strip() for item in request_scope if str(item).strip()}
    if not requested:
        return True
    granted = {str(item).strip() for item in grant_scope if str(item).strip()}
    return requested.issubset(granted)
