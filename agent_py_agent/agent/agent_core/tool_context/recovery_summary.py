
from __future__ import annotations


def strategy_preview(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    preview: list[dict[str, object]] = []
    for item in value[:3]:
        if not isinstance(item, dict):
            continue
        preview.append({
            "run_id": item.get("run_id", ""),
            "recommended_action": item.get("recommended_action", ""),
            "packet_status": item.get("packet_status", ""),
            "uses_continue_packet": bool(item.get("uses_continue_packet", False)),
            "runner_instruction": _clip(item.get("runner_instruction", ""), limit=220),
        })
    return preview


def _clip(value: object, *, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit].rstrip() + f"...[truncated {len(text) - limit} chars]"
