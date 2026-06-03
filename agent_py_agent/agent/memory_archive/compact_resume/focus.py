
from __future__ import annotations

from typing import Any

from ...common.value_parsing import sequence_strings


def resume_focus_payload(work_state: dict[str, Any], next_actions: list[str]) -> dict[str, Any]:
    actions = action_first_actions(next_actions, work_state)
    next_step = str(work_state.get("next_step") or "").strip()
    next_action = actions[0] if actions else next_step
    return {
        "next_action": next_action,
        "next_actions": actions,
        "captured_refs": captured_refs_payload(work_state),
        "do_not_repeat": [
            "不要把 compact/恢复文件当成新任务从头阅读；优先按 next_action 推进。",
            "不要重复已经登记的读取、写入或派工；只有验证、修补或缺事实时才重读。",
        ],
    }


def action_first_actions(next_actions: list[str], work_state: dict[str, Any]) -> list[str]:
    candidates = sequence_strings(next_actions) or sequence_strings(work_state.get("next_actions"))
    filtered = [item for item in candidates if not looks_like_reader_first_recovery_hint(item)]
    if filtered:
        return filtered
    next_step = str(work_state.get("next_step") or "").strip()
    if next_step and not looks_like_reader_first_recovery_hint(next_step):
        return [next_step]
    return []


def captured_refs_payload(work_state: dict[str, Any]) -> dict[str, Any]:
    artifact_refs = work_state.get("artifact_refs") if isinstance(work_state.get("artifact_refs"), list) else []
    return {
        "changed_files": sequence_strings(work_state.get("changed_files")),
        "read_files": sequence_strings(work_state.get("read_files")),
        "artifact_refs": [artifact_ref_payload(item) for item in artifact_refs if isinstance(item, dict)],
    }


def artifact_ref_payload(item: dict[str, Any]) -> dict[str, Any]:
    fallback = str(item.get("path") or "")
    return {
        "artifact_ref": str(item.get("scoped_call_id") or item.get("call_id") or fallback),
        "fallback_path": fallback,
        "source_path": str(item.get("source_path") or item.get("source_input") or ""),
        "tool": str(item.get("tool") or ""),
        "kind": str(item.get("kind") or ""),
    }


def looks_like_reader_first_recovery_hint(value: str) -> bool:
    text = value.strip().lower()
    if not text:
        return False
    recovery_markers = ("memory-resume", "localstore", "compact_context", "work_state_snapshot", "restore_refs")
    reader_markers = ("先查看", "先读取", "read ", "inspect ", "查看", "读取")
    return any(marker in text for marker in recovery_markers) and any(marker in text for marker in reader_markers)


__all__ = [
    "action_first_actions",
    "captured_refs_payload",
    "looks_like_reader_first_recovery_hint",
    "resume_focus_payload",
]
