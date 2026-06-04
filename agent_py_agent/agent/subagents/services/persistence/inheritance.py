
from __future__ import annotations

"""Persistence helpers for subagent inheritance manifests."""

import json
from dataclasses import asdict, fields
from pathlib import Path

from ...models import InheritanceManifest, SubAgentTask


def normalize_inheritance_manifest(value: object) -> InheritanceManifest:
    if isinstance(value, InheritanceManifest):
        return value
    if not isinstance(value, dict):
        return InheritanceManifest()
    payload = {key: value[key] for key in _field_names(InheritanceManifest) if key in value}
    for key in ["inherited", "overridden", "dropped", "policy"]:
        payload[key] = _dict_value(payload.get(key))
    payload["created_at"] = _float_value(payload.get("created_at"))
    return InheritanceManifest(**payload)


def write_inheritance_manifest(task: SubAgentTask) -> None:
    if not task.inheritance_manifest_json:
        return
    Path(task.inheritance_manifest_json).write_text(
        json.dumps(asdict(task.inheritance_manifest), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _field_names(model: type) -> set[str]:
    return {item.name for item in fields(model)}


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _float_value(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
