
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .models import CollaborationRequest
from .request_status import unavailable_target_agent_ids

TargetIdentityKeys = Callable[[object], set[str]]


def case_response_coverage(
    requests: list[CollaborationRequest],
    evidence_sources_by_request: dict[str, set[str]],
    *,
    target_identity_keys: TargetIdentityKeys,
    sample_limit: int = 20,
) -> dict[str, Any]:
    rows = [
        _request_coverage_row(
            request,
            evidence_sources_by_request.get(request.request_id, set()),
            target_identity_keys=target_identity_keys,
            sample_limit=sample_limit,
        )
        for request in requests
    ]
    return {
        "schema_version": "collaboration_response_coverage.v1",
        "request_count": len(rows),
        "target_count": sum(int(row["target_count"]) for row in rows),
        "responded_target_count": sum(int(row["responded_target_count"]) for row in rows),
        "missing_target_count": sum(int(row["missing_target_count"]) for row in rows),
        "unavailable_target_count": sum(int(row["unavailable_target_count"]) for row in rows),
        "sample_limit": sample_limit,
        "truncated": any(bool(row["truncated"]) for row in rows) or len(rows) > sample_limit,
        "requests": rows[-sample_limit:] if sample_limit > 0 else rows,
    }


def _request_coverage_row(
    request: CollaborationRequest,
    evidence_sources: set[str],
    *,
    target_identity_keys: TargetIdentityKeys,
    sample_limit: int,
) -> dict[str, Any]:
    targets = _unique_strings(request.target_agent_ids)
    responded = _responded_targets(targets, evidence_sources, target_identity_keys=target_identity_keys)
    unavailable = _unique_strings(unavailable_target_agent_ids(request))
    missing = [target for target in targets if target not in responded]
    return {
        "request_id": request.request_id,
        "status": request.status,
        "deadline_at": request.deadline_at,
        "target_count": len(targets),
        "responded_target_count": len(responded),
        "missing_target_count": len(missing),
        "unavailable_target_count": len(unavailable),
        "responded_target_sample": _sample(responded, sample_limit),
        "missing_target_sample": _sample(missing, sample_limit),
        "unavailable_target_sample": _sample(unavailable, sample_limit),
        "truncated": _is_truncated((responded, missing, unavailable), sample_limit),
    }


def _responded_targets(
    targets: list[str],
    evidence_sources: set[str],
    *,
    target_identity_keys: TargetIdentityKeys,
) -> list[str]:
    return [
        target
        for target in targets
        if evidence_sources.intersection(target_identity_keys(target))
    ]


def _unique_strings(values: object) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    return list(dict.fromkeys(str(item) for item in values if str(item or "").strip()))


def _sample(values: list[str], limit: int) -> list[str]:
    return values if limit <= 0 else values[:limit]


def _is_truncated(groups: tuple[list[str], ...], limit: int) -> bool:
    return bool(limit > 0 and any(len(group) > limit for group in groups))
