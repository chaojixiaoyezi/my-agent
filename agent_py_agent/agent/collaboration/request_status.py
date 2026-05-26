# LLM: Request status helpers keep collaboration lifecycle open-world and timer-aware.
# 模块用途: 判断协作请求状态、证据覆盖、缺席响应者和工具展示行。

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .models import CollaborationCase, CollaborationRequest, EvidencePacket
from .store_common import float_value

TargetAliases = Callable[[object], set[str]]


def is_terminal_status(status: str) -> bool:
    text = str(status or "").strip().lower()
    markers = ("close", "closed", "resolved", "done", "completed", "finished", "关闭", "已关闭", "解决", "完成")
    return bool(text and any(marker in text for marker in markers))


def is_completed_request_status(status: str) -> bool:
    text = str(status or "").strip().lower()
    markers = ("completed", "complete", "done", "finished", "responded", "answered", "fulfilled", "完成", "已响应")
    return bool(text and any(marker in text for marker in markers))


def is_blocked_request_status(status: str) -> bool:
    text = str(status or "").strip().lower()
    if is_timed_out_request_status(text):
        return False
    markers = ("blocked", "stuck", "failed", "error", "unavailable", "阻塞", "卡住", "失败", "不可用")
    return bool(text and any(marker in text for marker in markers))


def is_timed_out_request_status(status: str) -> bool:
    text = str(status or "").strip().lower()
    markers = ("timeout", "timed_out", "deadline_expired", "expired", "超时", "过期", "到期")
    return bool(text and any(marker in text for marker in markers))


def is_declined_request_status(status: str) -> bool:
    text = str(status or "").strip().lower()
    markers = ("declined", "rejected", "cancelled", "canceled", "skipped", "refused", "拒绝", "驳回", "取消", "跳过")
    return bool(text and any(marker in text for marker in markers))


def request_is_effectively_timed_out(
    request: CollaborationRequest,
    *,
    now: float,
    has_required_evidence: bool,
) -> bool:
    if has_required_evidence:
        return False
    return is_timed_out_request_status(request.status) or request_deadline_expired(request, now)


def request_deadline_expired(request: CollaborationRequest, now: float) -> bool:
    deadline = float_value(getattr(request, "deadline_at", 0.0))
    return bool(deadline > 0 and deadline <= now)


def evidence_sources_by_request(evidence: list[EvidencePacket], *, aliases: TargetAliases) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for item in evidence:
        request_id = str(getattr(item, "request_id", "") or "")
        source = str(getattr(item, "source_agent_id", "") or "")
        if request_id and source:
            result.setdefault(request_id, set()).update(aliases(source))
    return result


def request_has_required_evidence(
    request: CollaborationRequest,
    evidence_sources: set[str],
    *,
    target_aliases: TargetAliases,
) -> bool:
    targets = [str(item) for item in request.target_agent_ids if str(item or "").strip()]
    if len(targets) <= 1:
        return bool(evidence_sources) or is_completed_request_status(request.status)
    return bool(evidence_sources) and all(
        bool(evidence_sources.intersection(target_aliases(target))) for target in targets
    )


def missing_responder_agent_ids(
    request: CollaborationRequest,
    evidence_sources: set[str],
    *,
    target_aliases: TargetAliases,
) -> list[str]:
    missing: list[str] = []
    for target in [str(item) for item in request.target_agent_ids if str(item or "").strip()]:
        if not evidence_sources.intersection(target_aliases(target)):
            missing.append(target)
    return list(dict.fromkeys(missing))


def missing_responder_agent_ids_by_request(
    requests: list[CollaborationRequest],
    evidence_sources: dict[str, set[str]],
    *,
    target_aliases: TargetAliases,
) -> dict[str, list[str]]:
    rows = (
        (request.request_id, missing_responder_agent_ids(request, evidence_sources.get(request.request_id, set()), target_aliases=target_aliases))
        for request in requests
    )
    return {request_id: missing for request_id, missing in rows if missing}


def unavailable_target_agent_ids_by_request(
    requests: list[CollaborationRequest],
) -> dict[str, list[str]]:
    rows = ((request.request_id, unavailable_target_agent_ids(request)) for request in requests)
    return {request_id: unavailable for request_id, unavailable in rows if unavailable}


def unavailable_target_agent_ids(request: CollaborationRequest) -> list[str]:
    rows = request.metadata.get("unavailable_targets") if isinstance(request.metadata, dict) else None
    if not isinstance(rows, list):
        return []
    return list(dict.fromkeys(_unavailable_agent_id(row) for row in rows if _unavailable_agent_id(row)))


def pending_request_row(case: CollaborationCase, request: CollaborationRequest) -> dict[str, Any]:
    """Small responder-facing handoff shape; heavy context remains referenced."""
    return {
        "case_id": case.case_id,
        "request_id": request.request_id,
        "case_ref": f"collaboration://case/{case.case_id}",
        "request_ref": f"collaboration://request/{request.request_id}",
        "case_title": case.title,
        "case_status": case.status,
        "question": request.question,
        "status": request.status,
        "priority": request.priority or case.priority,
        "target_agent_ids": list(request.target_agent_ids),
        "required_capabilities": list(request.required_capabilities),
        "entities": dict(request.entities or {}),
        "problem_statement": request.problem_statement,
        "observed_facts": list(request.observed_facts),
        "query_intent": dict(request.query_intent or {}),
        "query_hints": list(request.query_hints),
        "routing_requirements": dict(request.routing_requirements or {}),
        "response_contract": dict(request.response_contract or {}),
        "context_refs": list(request.context_refs),
        "created_at": request.created_at,
        "updated_at": request.updated_at,
        "recommended_tools": ["case_status", "submit_evidence", "update_collaboration_request"],
    }


def case_overview_row(status: dict[str, Any]) -> dict[str, Any]:
    case = status.get("case") if isinstance(status.get("case"), dict) else {}
    missing_ids = status.get("missing_evidence_request_ids")
    return {
        "case_id": str(case.get("case_id") or ""),
        "title": str(case.get("title") or ""),
        "status": str(case.get("status") or ""),
        "priority": str(case.get("priority") or ""),
        "thread_id": str(case.get("thread_id") or ""),
        "task_id": str(case.get("task_id") or ""),
        "request_count": int(status.get("request_count") or 0),
        "pending_request_count": int(status.get("pending_request_count") or 0),
        "blocked_request_count": int(status.get("blocked_request_count") or 0),
        "timed_out_request_count": int(status.get("timed_out_request_count") or 0),
        "completed_request_count": int(status.get("completed_request_count") or 0),
        "missing_evidence_request_count": len(missing_ids) if isinstance(missing_ids, list) else 0,
        "evidence_count": int(status.get("evidence_count") or 0),
    }


def case_status_text(status: dict[str, Any]) -> str:
    case = status.get("case") if isinstance(status.get("case"), dict) else {}
    return str(case.get("status") or "")


def _unavailable_agent_id(row: object) -> str:
    if isinstance(row, dict):
        return str(row.get("agent_id") or row.get("run_id") or "").strip()
    return str(row or "").strip()
