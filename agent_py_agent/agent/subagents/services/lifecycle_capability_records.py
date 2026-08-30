
from __future__ import annotations

"""Builder helpers for capability lifecycle records."""

import time
from dataclasses import dataclass
from typing import Any

from ..models import CapabilityGap, CapabilityGrant, CapabilityRequest
from ..utils import _new_id


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
        alternatives_attempted=params.alternatives_attempted or [],
        escalation_target=params.escalation_target,
        created_at=time.time(),
    )


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
        request_scope=params.request_scope or {},
        expires_after_task=params.expires_after_task,
        expires_at=params.expires_at,
        created_at=time.time(),
    )


@dataclass(frozen=True)
class BuildCapabilityGapInput:
    run_id: str
    task_goal: str
    params: Any
    memory_routes: list[dict[str, str]]
    injected_rule_paths: list[str]


def build_capability_gap(request: BuildCapabilityGapInput) -> CapabilityGap:
    params = request.params
    return CapabilityGap(
        id=_new_id("capgap"),
        run_id=request.run_id,
        missing_capability=params.missing_capability,
        source_task=request.task_goal,
        why_failed=params.why_failed,
        request_id=str(getattr(params, "request_id", "") or ""),
        gap_type=params.gap_type,
        attempted_skills=params.attempted_skills or [],
        attempted_tools=params.attempted_tools or [],
        needed_outputs=params.needed_outputs or [],
        suggested_skill=params.suggested_skill,
        suggested_tool=params.suggested_tool,
        requested_scope=params.requested_scope or {},
        constraints=params.constraints or {},
        escalation_chain=params.escalation_chain or [],
        next_record_refs=params.next_record_refs or [],
        memory_routes=request.memory_routes,
        injected_rule_paths=request.injected_rule_paths,
        created_at=time.time(),
    )
