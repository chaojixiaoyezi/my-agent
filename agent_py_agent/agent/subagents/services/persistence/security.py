
from __future__ import annotations

"""Security reserve persistence helpers."""

from dataclasses import fields

from ....common.value_parsing import sequence_strings
from ...models import SecuritySignal


def _field_names(model: type) -> set[str]:
    return {item.name for item in fields(model)}


def normalize_security_signal(value: object) -> SecuritySignal:
    if isinstance(value, SecuritySignal):
        return value
    if not isinstance(value, dict):
        return SecuritySignal()
    payload = {key: value[key] for key in _field_names(SecuritySignal) if key in value}
    for key in ["evidence_refs", "artifact_refs"]:
        payload[key] = sequence_strings(payload.get(key))
    payload["created_at"] = _float_value(payload.get("created_at"))
    payload["reserved"] = payload.get("reserved") if isinstance(payload.get("reserved"), dict) else {}
    return SecuritySignal(**payload)


def _float_value(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
