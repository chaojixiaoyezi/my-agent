from __future__ import annotations

"""Normalize runtime identity reserve fields for subagent task persistence."""

from dataclasses import fields

from ...models import RuntimeIdentity


def normalize_runtime_identity(value: object) -> RuntimeIdentity:
    if isinstance(value, RuntimeIdentity):
        return value
    if not isinstance(value, dict):
        return RuntimeIdentity()
    payload = {key: value[key] for key in _field_names(RuntimeIdentity) if key in value}
    payload["reserved"] = payload.get("reserved") if isinstance(payload.get("reserved"), dict) else {}
    return RuntimeIdentity(**payload)


def _field_names(model: type) -> set[str]:
    return {item.name for item in fields(model)}
