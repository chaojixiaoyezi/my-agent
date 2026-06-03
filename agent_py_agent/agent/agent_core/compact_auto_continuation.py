
from __future__ import annotations

"""main-agent auto continuation helpers for compact/resume."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CompactAutoContinuationDecision:
    should_continue: bool
    reason: str
    injection: str = ""
    user_prompt: str = ""


def compact_auto_continuation_decision(result: Any, *, depth: int = 0) -> CompactAutoContinuationDecision:
    packet = _continue_packet(result)
    if not _result_ready(result, packet):
        return CompactAutoContinuationDecision(False, "not_ready")
    return CompactAutoContinuationDecision(
        True,
        "ready",
        build_compact_auto_continue_injection(packet),
        compact_auto_continue_user_prompt(),
    )


def build_compact_auto_continue_injection(packet: dict[str, Any]) -> str:
    work_state = packet.get("work_state_snapshot", {}) if isinstance(packet.get("work_state_snapshot"), dict) else {}
    guard = packet.get("guard", {}) if isinstance(packet.get("guard"), dict) else {}
    resume_focus = _resume_focus(packet, work_state)
    sections = [
        "# Compact Auto Continuation",
        "Status: allowed_to_continue",
        f"Apply ID: {packet.get('apply_id', '')}",
        f"Continue mode: {packet.get('continue_mode', '')}",
        f"Guard status: {guard.get('status', '')}",
        "",
        _resume_focus_section(resume_focus),
        _captured_refs_section(resume_focus.get("captured_refs")),
        "## Goal",
        str(work_state.get("goal") or ""),
        "",
        "## Current Phase",
        str(work_state.get("current_phase") or ""),
        "",
        "## Next Step",
        str(resume_focus.get("next_action") or work_state.get("next_step") or "继续当前任务目标，从未完成部分推进。"),
        "",
        _items_section("## Acceptance", work_state.get("acceptance")),
        _items_section("## Constraints", work_state.get("constraints")),
        _tests_section(work_state.get("latest_tests")),
        _list_section("## Changed Files", work_state.get("changed_files")),
        "## Continuation Rules",
        "- Continue only from the Next Step above.",
        "- Do not redo completed work.",
        "- 不要先重读 compact 文件；只有下一步缺事实、需要校验或引用损坏时才读取恢复引用。",
        "- 如果可选备注缺失，继续从目标、已捕获引用和工作区事实推进；只有关键源引用完全无法定位时才报告阻塞。",
    ]
    return "\n".join(section for section in sections if section is not None).strip()


def compact_auto_continue_user_prompt() -> str:
    return (
        "继续当前任务的未完成部分。"
        "不要重做已完成内容；优先推进未完成部分，必要时才读取恢复引用。"
    )


def mark_compact_auto_continued(result: Any, source_result: Any, *, depth: int) -> Any:
    result.memory_compact_auto_continued = True
    result.memory_compact_auto_continued_from_apply_id = getattr(source_result, "memory_compact_auto_apply_id", "")
    result.memory_compact_auto_continuation_depth = depth
    return result


def _result_ready(result: Any, packet: dict[str, Any]) -> bool:
    return bool(
        getattr(result, "memory_compact_auto_allowed_to_continue", False)
        and getattr(result, "memory_compact_auto_continue_ready", False)
        and getattr(result, "memory_compact_auto_apply_id", "")
        and packet.get("ready_to_continue")
    )


def _continue_packet(result: Any) -> dict[str, Any]:
    packet = getattr(result, "memory_compact_auto_continue_packet", None)
    return packet if isinstance(packet, dict) else {}


def _items_section(title: str, payload: Any) -> str:
    items = _items(payload.get("items")) if isinstance(payload, dict) else []
    return _list_section(title, items or ["<not recorded>"])


def _tests_section(payload: Any) -> str:
    tests = _items(payload.get("items")) if isinstance(payload, dict) else []
    status = str(payload.get("status") or "not_recorded") if isinstance(payload, dict) else "not_recorded"
    return "\n".join(["## Latest Tests", f"Status: {status}", *_bullet_lines(tests or ["<not recorded>"])])


def _resume_focus(packet: dict[str, Any], work_state: dict[str, Any]) -> dict[str, Any]:
    focus = packet.get("resume_focus")
    if isinstance(focus, dict):
        return dict(focus)
    next_actions = _action_first_items(_items(packet.get("next_actions")) or _items(work_state.get("next_actions")))
    next_step = str(work_state.get("next_step") or "").strip()
    if _looks_like_reader_first_recovery_hint(next_step):
        next_step = ""
    return {
        "next_action": next_actions[0] if next_actions else next_step,
        "next_actions": next_actions,
        "captured_refs": {
            "changed_files": _items(work_state.get("changed_files")),
            "read_files": _items(work_state.get("read_files")),
            "artifact_refs": _artifact_ref_items(packet.get("artifact_read_hints")),
        },
        "do_not_repeat": [
            "不要把 compact/恢复文件当成新任务从头阅读；优先按 next_action 推进。",
            "不要重复已经登记的读取、写入或派工；只有验证、修补或缺事实时才重读。",
        ],
    }


def _resume_focus_section(focus: dict[str, Any]) -> str:
    next_action = str(focus.get("next_action") or "").strip() or "继续当前任务的未完成部分。"
    return "\n".join(
        [
            "## Resume Focus",
            f"- next_action: {next_action}",
            *_bullet_lines(_items(focus.get("next_actions"))),
            *_bullet_lines(_items(focus.get("do_not_repeat"))),
        ]
    )


def _captured_refs_section(payload: Any) -> str:
    refs = payload if isinstance(payload, dict) else {}
    lines = ["## Already Captured Refs"]
    lines.extend(_prefixed_lines("changed_files", _items(refs.get("changed_files"))))
    lines.extend(_prefixed_lines("read_files", _items(refs.get("read_files"))))
    artifact_refs = refs.get("artifact_refs") if isinstance(refs.get("artifact_refs"), list) else []
    rendered_artifacts = [
        _render_artifact_ref(item) if isinstance(item, dict) else str(item).strip()
        for item in artifact_refs
        if (isinstance(item, dict) or str(item).strip())
    ]
    lines.extend(_prefixed_lines("artifact_refs", rendered_artifacts))
    if len(lines) == 1:
        lines.append("- <none>")
    return "\n".join(lines)


def _artifact_ref_items(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    refs: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = str(item.get("artifact_ref") or item.get("fallback_path") or "").strip()
        else:
            text = str(item).strip()
        if text:
            refs.append(text)
    return refs


def _render_artifact_ref(item: dict[str, Any]) -> str:
    ref = str(item.get("artifact_ref") or item.get("fallback_path") or "").strip()
    source = str(item.get("source_path") or "").strip()
    if source and ref:
        return f"{ref} (source: {source})"
    return ref or source


def _prefixed_lines(label: str, values: list[str]) -> list[str]:
    return [f"- {label}: {item}" for item in values if item]


def _action_first_items(items: list[str]) -> list[str]:
    return [item for item in items if not _looks_like_reader_first_recovery_hint(item)]


def _looks_like_reader_first_recovery_hint(value: str) -> bool:
    text = value.strip().lower()
    recovery_markers = ("memory-resume", "localstore", "compact_context", "work_state_snapshot", "restore_refs")
    reader_markers = ("先查看", "先读取", "read ", "inspect ", "查看", "读取")
    return any(marker in text for marker in recovery_markers) and any(marker in text for marker in reader_markers)


def _list_section(title: str, values: Any) -> str:
    return "\n".join([title, *_bullet_lines(_items(values) or ["<none>"])])


def _bullet_lines(values: list[str]) -> list[str]:
    return [f"- {item}" for item in values if item]


def _items(value: Any) -> list[str]:
    if isinstance(value, list | tuple):
        return [text for item in value if (text := str(item).strip())]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


__all__ = [
    "CompactAutoContinuationDecision",
    "build_compact_auto_continue_injection",
    "compact_auto_continuation_decision",
    "compact_auto_continue_user_prompt",
    "mark_compact_auto_continued",
]
