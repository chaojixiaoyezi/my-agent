# LLM: Rework plans are structured suggestions, never task-specific hard gates.
# 模块用途: 根据协作请求状态生成返工目标，帮助模型换路、补派或收口部分结果。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .identity import candidate_target_agent_ids
from .models import CollaborationRequest
from .request_status import TargetAliases, missing_responder_agent_ids


@dataclass(frozen=True)
class ReworkPlanInput:
    blocked_requests: list[CollaborationRequest]
    pending_requests: list[CollaborationRequest]
    timed_out_requests: list[CollaborationRequest]
    evidence_sources_by_request: dict[str, set[str]]
    target_aliases: TargetAliases


def rework_plan(request: ReworkPlanInput) -> dict[str, Any]:
    targets = [
        *_blocked_targets(request.blocked_requests),
        *_timed_out_targets(
            request.timed_out_requests,
            request.evidence_sources_by_request,
            request.target_aliases,
        ),
        *_pending_targets(request.pending_requests),
    ]
    return {"needed": bool(targets), "target_count": len(targets), "targets": targets[-20:]}


def _blocked_targets(requests: list[CollaborationRequest]) -> list[dict[str, Any]]:
    return [
        _request_rework_target(
            request,
            reason="request_blocked",
            suggested_actions=[
                "inspect_request_context",
                "reroute_collaboration_request",
                "try_alternate_source_or_params",
                "ask_requester_for_clarification_if_needed",
                "resubmit_evidence_or_mark_true_blocker",
            ],
        )
        for request in requests
    ]


def _timed_out_targets(
    requests: list[CollaborationRequest],
    evidence_sources_by_request: dict[str, set[str]],
    target_aliases: TargetAliases,
) -> list[dict[str, Any]]:
    return [
        _request_rework_target(
            request,
            reason="request_timed_out_partial_results_allowed",
            suggested_actions=[
                "summarize_partial_evidence",
                "list_missing_responders",
                "decide_continue_or_reroute",
                "update_case_status_with_limitations",
            ],
            missing_responder_agent_ids=missing_responder_agent_ids(
                request,
                evidence_sources_by_request.get(request.request_id, set()),
                target_aliases=target_aliases,
            ),
        )
        for request in requests
    ]


def _pending_targets(requests: list[CollaborationRequest]) -> list[dict[str, Any]]:
    return [
        _request_rework_target(
            request,
            reason="missing_evidence",
            suggested_actions=[
                "inspect_request_context",
                "ask_responder_for_evidence_or_status",
                "reroute_collaboration_request",
                "try_alternate_source_or_params",
                "submit_evidence_or_update_request_status",
            ],
        )
        for request in requests
    ]


def _request_rework_target(
    request: CollaborationRequest,
    *,
    reason: str,
    suggested_actions: list[str],
    missing_responder_agent_ids: list[str] | None = None,
) -> dict[str, Any]:
    candidates = candidate_target_agent_ids(request)
    return {
        "request_id": request.request_id,
        "case_id": request.case_id,
        "reason": reason,
        "status": request.status,
        "target_agent_ids": list(request.target_agent_ids),
        "missing_responder_agent_ids": list(missing_responder_agent_ids or []),
        "candidate_target_agent_ids": candidates,
        "required_capabilities": list(request.required_capabilities),
        "summary": str(request.metadata.get("status_summary") or request.question or ""),
        "primary_tool": "reroute_collaboration_request" if candidates else "update_collaboration_request",
        "suggested_actions": suggested_actions,
    }
