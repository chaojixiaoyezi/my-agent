
from __future__ import annotations

from typing import Any

from ..common.value_parsing import sequence_strings


def desired_outputs_payload(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    return {
        "items": sequence_strings(payload.get("items")),
        "source_status": str(payload.get("source_status") or "not_recorded"),
        "source_paths": sequence_strings(payload.get("source_paths")),
    }


def desired_outputs_line(value: Any) -> str:
    payload = desired_outputs_payload(value)
    items = payload["items"]
    if not items:
        return "未记录"
    return "；".join(items[:3]) + ("；..." if len(items) > 3 else "")


def run_intent_payload(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    return {
        "reference_roots": desired_outputs_payload(payload.get("reference_roots")),
        "desired_outputs": desired_outputs_payload(payload.get("desired_outputs")),
        "soft_only": bool(payload.get("soft_only", True)),
    }


def run_intent_line(value: Any) -> str:
    payload = run_intent_payload(value)
    references = payload["reference_roots"]["items"]
    outputs = payload["desired_outputs"]["items"]
    parts: list[str] = []
    if references:
        parts.append("参考目录=" + "；".join(references[:2]))
    if outputs:
        parts.append("目标输出=" + "；".join(outputs[:2]))
    return "；".join(parts) if parts else "未记录"


__all__ = [
    "desired_outputs_line",
    "desired_outputs_payload",
    "run_intent_line",
    "run_intent_payload",
]
