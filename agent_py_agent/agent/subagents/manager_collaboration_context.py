# LLM: Subagent runner collaboration context surfaces targeted requests without expanding full case ledgers.
# 模块用途: 给被点名的 responder 注入待处理协作请求摘要，帮助复用已有 case/request。

from __future__ import annotations

from typing import Any


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
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
    if not requests:
        return {}
    return {
        "targeted_request_count": len(requests),
        "targeted_requests": _bounded_targeted_requests(requests),
        "responder_policy": (
            "优先复用已有 case/request，按 case_status -> submit_evidence -> "
            "update_collaboration_request 处理；除非发现全新问题，不要另开 open_case。"
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
