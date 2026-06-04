
from __future__ import annotations

"""Capability-request extraction for runner structured output."""
from .values import _dict_list


def capability_requests_from_payload(payload: dict[str, object]) -> list[dict[str, object]]:
    return _dict_list(payload.get("capability_requests", []))
