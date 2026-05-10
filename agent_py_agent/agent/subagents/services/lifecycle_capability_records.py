# LLM: Capability lifecycle record builders keep SubAgentLifecycleService small as schemas grow.
# 模块用途: 集中构造能力申请、授权和缺口记录，避免生命周期服务文件随字段扩展变大。

from __future__ import annotations

"""Builder helpers for capability lifecycle records."""

import time
from dataclasses import dataclass
from typing import Any

from ..models import CapabilityGap, CapabilityGrant, CapabilityRequest
from ..utils import _new_id


# LLM: build_capability_request keeps request schema construction in one place.
# 函数用途: 根据参数包构造 CapabilityRequest，避免服务类方法随字段扩展不断变长。
def build_capability_request(run_id: str, params: Any) -> CapabilityRequest:
    return CapabilityRequest(
        id=_new_id("capreq"),
        from_run_id=run_id,
        problem=params.problem,
        needed_capability=params.needed_capability,
        expected_output=params.expected_output,
        capability_type=params.capability_type,
        tried=params.tried or [],
        evidence=params.evidence or [],
        constraints=params.constraints or {},
        requested_tools=params.requested_tools or [],
        requested_skills=params.requested_skills or [],
        requested_mcp_tools=params.requested_mcp_tools or [],
        requested_commands=params.requested_commands or [],
        cwd_scope=params.cwd_scope or [],
        path_scope=params.path_scope or [],
        network_scope=params.network_scope or [],
        output_budget=params.output_budget or {},
        risk_level=params.risk_level,
        fallback_attempted=params.fallback_attempted or [],
        escalation_target=params.escalation_target,
        created_at=time.time(),
        reserved=params.reserved or {},
    )


# LLM: build_capability_grant mirrors scoped grant params without bloating SubAgentLifecycleService.
# 函数用途: 根据参数包构造 CapabilityGrant，并保留旧 skills/tools 兼容行为。
def build_capability_grant(run_id: str, params: Any) -> CapabilityGrant:
    return CapabilityGrant(
        id=_new_id("capgrant"),
        request_id=params.request_id,
        grant_to_run_id=run_id,
        grant_type=params.grant_type,
        skills=params.skills or [],
        tools=params.tools or [],
        mcp_tools=params.mcp_tools or [],
        command_allowlist=params.command_allowlist or [],
        capability_cards=params.capability_cards or [],
        reason=params.reason,
        constraints=params.constraints or {},
        path_scope=params.path_scope or [],
        network_scope=params.network_scope or [],
        output_budget=params.output_budget or {},
        risk_level=params.risk_level,
        expires_after_task=params.expires_after_task,
        expires_at=params.expires_at,
        created_at=time.time(),
        reserved=params.reserved or {},
    )


# LLM: BuildCapabilityGapInput bundles route-derived memory facts for gap construction.
# 类用途: 汇总 gap 构造所需的任务、参数和 memory route 命中信息，保持 helper 调用稳定。
@dataclass(frozen=True)
class BuildCapabilityGapInput:
    run_id: str
    task_goal: str
    params: Any
    memory_routes: list[dict[str, str]]
    injected_rule_paths: list[str]


# LLM: build_capability_gap preserves requested scope and memory routes in one construction point.
# 函数用途: 根据参数包和路由命中构造 CapabilityGap，避免字段扩展散落在服务方法中。
def build_capability_gap(request: BuildCapabilityGapInput) -> CapabilityGap:
    params = request.params
    return CapabilityGap(
        id=_new_id("capgap"),
        run_id=request.run_id,
        missing_capability=params.missing_capability,
        source_task=request.task_goal,
        why_failed=params.why_failed,
        gap_type=params.gap_type,
        attempted_skills=params.attempted_skills or [],
        attempted_tools=params.attempted_tools or [],
        needed_outputs=params.needed_outputs or [],
        suggested_skill=params.suggested_skill,
        suggested_tool=params.suggested_tool,
        requested_scope=params.requested_scope or {},
        escalation_chain=params.escalation_chain or [],
        next_record_refs=params.next_record_refs or [],
        memory_routes=request.memory_routes,
        injected_rule_paths=request.injected_rule_paths,
        created_at=time.time(),
        reserved=params.reserved or {},
    )
