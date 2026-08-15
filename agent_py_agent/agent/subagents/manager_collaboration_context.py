
from __future__ import annotations

from typing import Any

from ..runtime_errors import runtime_error_report


def collaboration_context_payload(manager: object, task: object) -> dict[str, object]:
    store = getattr(manager, "collaboration_store", None)
    if store is None or not hasattr(store, "pending_requests_for_agent"):
        return {}
    try:
        requests = store.pending_requests_for_agent(
            agent_id=str(getattr(task, "id", "") or ""),
            agent_name=str(getattr(task, "agent_name", "") or ""),
            agent_role=str(getattr(task, "role", "") or ""),
            limit=10,
        )
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return {
            "targeted_request_count": 0,
            "targeted_requests": [],
            "collaboration_load_error": runtime_error_report(
                exc,
                context="subagent_context.collaboration.pending_requests",
            ),
        }
    if not requests:
        return {}
    return {
        "targeted_request_count": len(requests),
        "targeted_requests": _bounded_targeted_requests(requests),
        "responder_policy": (
            "优先复用已有 case/request，按 inspect_collaboration -> submit_collaboration_result -> "
            "update_collaboration 处理；除非发现全新问题，不要另开 raise_collaboration。"
        ),
    }


def _bounded_targeted_requests(requests: list[dict[str, Any]]) -> list[dict[str, object]]:
    return [{key: request[key] for key in _TARGETED_REQUEST_KEYS if key in request} for request in requests[:10]]


_TARGETED_REQUEST_KEYS = (
    "case_id",
    "request_id",
    "case_ref",
    "request_ref",
    "case_title",
    "question",
    "status",
    "priority",
    "target_agent_ids",
    "required_capabilities",
    "entities",
    "problem_statement",
    "observed_facts",
    "query_intent",
    "query_hints",
    "routing_requirements",
    "response_contract",
    "context_refs",
    "recommended_tools",
)
