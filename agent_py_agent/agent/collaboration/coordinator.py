from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import CaseDecision, new_decision_id
from .request_status import (
    CollaborationRequestStatus,
    is_blocked_request_status,
    is_declined_request_status,
    is_timed_out_request_status,
    request_has_required_evidence,
)
from .store_status import CollaborationStore

if TYPE_CHECKING:
    from ..conversation import ConversationStore


@dataclass(frozen=True)
class CollaborationCoordinatorPolicy:
    urgent_min_evidence_packets: int = 1
    normal_min_evidence_packets: int = 2
    deadline_closes_case: bool = True


@dataclass(frozen=True)
class _DecisionSummaryFacts:
    pending_request_count: int
    blocked_request_count: int
    timed_out_request_count: int
    completed_request_count: int
    missing_by_request: dict[str, list[str]]
    unavailable_by_request: dict[str, list[str]]


@dataclass(frozen=True)
class _CloseMetadataRequest:
    case_id: str
    status: dict
    evidence_ids: tuple[str, ...]
    missing: dict
    unavailable: dict


class CollaborationCoordinator:
    def __init__(
        self,
        *,
        store: CollaborationStore,
        conversation_store: ConversationStore,
        policy: CollaborationCoordinatorPolicy | None = None,
    ):
        self.store = store
        self.conversation_store = conversation_store
        self.policy = policy or CollaborationCoordinatorPolicy()

    def tick(self, *, now: float) -> list[CaseDecision]:
        decisions: list[CaseDecision] = []
        for case in self.store.list_cases(status="open"):
            decision = self._tick_case(case, now=now)
            if decision is not None:
                decisions.append(decision)
        return decisions

    def _tick_case(self, case, *, now: float) -> CaseDecision | None:
        if not case.thread_id:
            return None
        _mark_expired_requests(self.store, case.case_id, now=now)
        status = self.store.case_status(case.case_id)
        evidence = self.store.case_evidence(case.case_id)
        if not _should_close_case(case.priority, status, self.policy, now):
            return None
        context = _close_context(case, status, evidence)
        wake_signal_id = self._raise_case_observation(case, context, now=now)
        decision = _case_close_decision(case, context, wake_signal_id, now)
        self.store.append_decision(decision)
        self.store.update_case_status(case.case_id, status="closed", now=now)
        return decision

    def _raise_case_observation(self, case, context: dict, *, now: float) -> str:
        observation = self.conversation_store.append_observation({'thread_id': case.thread_id, 'event_type': "collaboration_case_closed", 'summary': context["summary"], 'urgency': case.priority, 'source_agent_id': case.created_by, 'root_task_id': case.task_id, 'evidence_refs': context["evidence_refs"], 'requires_main_agent': True, 'requires_llm_report': True, 'now': now, 'metadata': context["metadata"]})
        if not _should_wake_main(case, context["status"]):
            return ""
        wake = self.conversation_store.raise_wake_signal({'thread_id': case.thread_id, 'observation': observation, 'urgency': "urgent" if str(case.priority).lower() == "urgent" else "normal", 'reason': "collaboration_case_closed", 'dedupe_key': f"case:{case.case_id}:closed", 'now': now, 'metadata': context["wake_metadata"]})
        return wake.wake_signal_id


def _should_close_case(priority, status, policy, now: float) -> bool:
    if bool(status.get("ready_for_main_agent")):
        return True
    evidence_count = int(status.get("evidence_count") or 0)
    min_evidence = (
        policy.urgent_min_evidence_packets
        if str(priority).lower() == "urgent"
        else policy.normal_min_evidence_packets
    )
    if evidence_count >= max(0, min_evidence):
        return True
    if not policy.deadline_closes_case:
        return False
    requests = [
        item
        for item in status.get("requests", [])
        if isinstance(item, dict)
    ]
    deadlines = [_float(item.get("deadline_at")) for item in requests]
    deadlines = [item for item in deadlines if item > 0]
    return bool(deadlines and min(deadlines) <= now)


def _mark_expired_requests(store: CollaborationStore, case_id: str, *, now: float) -> None:
    evidence_sources_by_request = _evidence_sources_by_request(store, store.case_evidence(case_id))
    for request in store.case_requests(case_id):
        deadline = _float(request.deadline_at)
        if deadline <= 0 or deadline > now:
            continue
        if (
            is_blocked_request_status(request.status)
            or is_declined_request_status(request.status)
            or is_timed_out_request_status(request.status)
        ):
            continue
        evidence_sources = evidence_sources_by_request.get(request.request_id, set())
        if request_has_required_evidence(
            request,
            evidence_sources,
            target_identity_keys=store.agent_identity_keys,
        ):
            continue
        missing_responders = _missing_responder_agent_ids(
            request,
            evidence_sources,
            target_identity_keys=store.agent_identity_keys,
        )
        store.update_request_status({'case_id': case_id, 'request_id': request.request_id, 'status': CollaborationRequestStatus.TIMEOUT.value, 'actor_agent_id': "collaboration_coordinator", 'summary': "协作请求超过 deadline，已标记为 timeout；主代理可带部分结果、未响应名单和限制继续推进。", 'now': now, 'metadata': {
                "timeout": {
                    "deadline_at": deadline,
                    "timed_out_at": now,
                    "missing_responder_agent_ids": missing_responders,
                }
            }})


def _evidence_sources_by_request(store: CollaborationStore, evidence) -> dict[str, set[str]]:
    sources: dict[str, set[str]] = {}
    for item in evidence:
        request_id = str(getattr(item, "request_id", "") or "")
        source = str(getattr(item, "source_agent_id", "") or "")
        if request_id and source:
            sources.setdefault(request_id, set()).update(store.agent_identity_keys(source))
    return sources


def _missing_responder_agent_ids(request, evidence_sources: set[str], *, target_identity_keys) -> list[str]:
    missing: list[str] = []
    for target in [str(item) for item in getattr(request, "target_agent_ids", ()) if str(item or "").strip()]:
        if evidence_sources.intersection(target_identity_keys(target)):
            continue
        if target not in missing:
            missing.append(target)
    return missing


def _decision_summary(
    title: str,
    summary: str,
    evidence_count: int,
    facts: _DecisionSummaryFacts,
) -> str:
    label = title or "未命名协作事件"
    details = summary or "协作事件已达到主代理处理条件。"
    request_bits = (
        f"待响应={facts.pending_request_count}，"
        f"不可达={facts.blocked_request_count}，"
        f"到期未回={facts.timed_out_request_count}，"
        f"已回={facts.completed_request_count}"
    )
    missing_count = sum(len(items) for items in facts.missing_by_request.values())
    unavailable_count = sum(len(items) for items in facts.unavailable_by_request.values())
    responder_bits = f"未回复目标={missing_count}，不可达目标={unavailable_count}"
    return (
        f"协作 case 收集窗口已关闭：{label}。证据包数量={evidence_count}，"
        f"{request_bits}，{responder_bits}。{details}"
    )


def _close_context(case, status: dict, evidence: list) -> dict:
    missing = _dict_of_string_lists(status.get("missing_responder_agent_ids_by_request"))
    unavailable = _dict_of_string_lists(status.get("unavailable_target_agent_ids_by_request"))
    evidence_ids = tuple(item.evidence_id for item in evidence)
    summary = _decision_summary(case.title, case.summary, len(evidence), _summary_facts(status, missing, unavailable))
    metadata = _case_close_metadata(_CloseMetadataRequest(case.case_id, status, evidence_ids, missing, unavailable))
    return {
        "status": status,
        "evidence_ids": evidence_ids,
        "evidence_refs": [ref for item in evidence for ref in item.evidence_refs],
        "missing_by_request": missing,
        "unavailable_by_request": unavailable,
        "summary": summary,
        "metadata": metadata,
        "wake_metadata": _wake_metadata(case.case_id, status, missing, unavailable),
    }


def _case_close_metadata(request: _CloseMetadataRequest) -> dict:
    return {
        "case_id": request.case_id,
        "evidence_ids": list(request.evidence_ids),
        "response_coverage": request.status.get("response_coverage") or {},
        "missing_responder_agent_ids_by_request": request.missing,
        "unavailable_target_agent_ids_by_request": request.unavailable,
        "case_window_status": "closed",
        **_status_counts(request.status),
    }


def _wake_metadata(case_id: str, status: dict, missing: dict, unavailable: dict) -> dict:
    return {
        "case_id": case_id,
        "response_coverage": status.get("response_coverage") or {},
        "missing_responder_agent_ids_by_request": missing,
        "unavailable_target_agent_ids_by_request": unavailable,
        **_status_counts(status),
    }


def _case_close_decision(case, context: dict, wake_signal_id: str, now: float) -> CaseDecision:
    return CaseDecision(
        decision_id=new_decision_id(),
        case_id=case.case_id,
        decision_type="close_collaboration_window",
        summary=context["summary"],
        requires_main_agent=True,
        wake_signal_id=wake_signal_id,
        evidence_ids=context["evidence_ids"],
        created_at=now,
        metadata={"priority": case.priority, "ready_for_main_agent": bool(context["status"].get("ready_for_main_agent")), **context["metadata"]},
    )


def _status_counts(status: dict) -> dict:
    return {
        "pending_request_count": int(status.get("pending_request_count") or 0),
        "blocked_request_count": int(status.get("blocked_request_count") or 0),
        "timed_out_request_count": int(status.get("timed_out_request_count") or 0),
        "completed_request_count": int(status.get("completed_request_count") or 0),
    }


def _should_wake_main(case, status: dict) -> bool:
    return bool(status.get("ready_for_main_agent")) or str(case.priority).lower() == "urgent"


def _summary_facts(
    status: dict,
    missing_by_request: dict[str, list[str]],
    unavailable_by_request: dict[str, list[str]],
) -> _DecisionSummaryFacts:
    return _DecisionSummaryFacts(
        pending_request_count=int(status.get("pending_request_count") or 0),
        blocked_request_count=int(status.get("blocked_request_count") or 0),
        timed_out_request_count=int(status.get("timed_out_request_count") or 0),
        completed_request_count=int(status.get("completed_request_count") or 0),
        missing_by_request=missing_by_request,
        unavailable_by_request=unavailable_by_request,
    )


def _dict_of_string_lists(value) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, rows in value.items():
        request_id = str(key or "").strip()
        if not request_id or not isinstance(rows, list):
            continue
        result[request_id] = [str(item) for item in rows if str(item or "").strip()]
    return result


def _float(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["CollaborationCoordinator", "CollaborationCoordinatorPolicy"]
