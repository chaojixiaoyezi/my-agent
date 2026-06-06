
from __future__ import annotations

from typing import Any

from .models import CollaborationRequest
from .store_common import strings


def agent_identity_keys(values: object = ()) -> set[str]:
    """Return explicit structured identity keys only."""
    keys: set[str] = set()
    for value in _identity_values(values):
        text = str(value or "").strip()
        if not text:
            continue
        keys.add(text)
    return keys


def _identity_values(values: object) -> tuple[object, ...]:
    if isinstance(values, (list, tuple, set)):
        return tuple(values)
    return (values,)


def request_update_targets(*, explicit_targets: object, metadata: dict[str, Any]) -> tuple[str, ...]:
    """Read reroute targets from the current explicit parameter."""
    targets = strings(explicit_targets)
    if targets:
        return targets
    return ()


def candidate_target_agent_ids(request: CollaborationRequest) -> list[str]:
    """Expose current structured alternate routing candidates."""
    candidates: list[str] = []
    for key in ("candidate_target_agent_ids",):
        _append_candidates(candidates, request, key)
    return candidates


def _append_candidates(candidates: list[str], request: CollaborationRequest, key: str) -> None:
    for item in strings(_listish(request.metadata.get(key))):
        if item not in candidates and item not in request.target_agent_ids:
            candidates.append(item)


def _listish(value: object) -> object:
    return value if isinstance(value, (list, tuple)) else [value]
