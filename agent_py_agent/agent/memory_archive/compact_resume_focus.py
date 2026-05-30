# LLM: Compact resume focus helpers keep continuation action-first across apply/resume/runtime prompts.
# 模块用途: 过滤旧式“先读恢复文件”提示，并生成 compact 后续接需要的 next_action 与 captured_refs。

from __future__ import annotations

from typing import Any


# LLM: resume_focus_payload is the shared action-first view for compact continue packets.
# 函数用途: 固定续接时的下一步、已捕获事实和避免重复事项，帮助多轮 compact 后继续推进。
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


# LLM: action_first_actions removes recovery-reader hints from model-visible next actions.
# 函数用途: 过滤“先读 memory-resume/compact 文件”这类旧式提示，避免 compact 后从恢复文件重新开工。
def action_first_actions(next_actions: list[str], work_state: dict[str, Any]) -> list[str]:
    candidates = string_list(next_actions) or string_list(work_state.get("next_actions"))
    filtered = [item for item in candidates if not looks_like_reader_first_recovery_hint(item)]
    if filtered:
        return filtered
    next_step = str(work_state.get("next_step") or "").strip()
    if next_step and not looks_like_reader_first_recovery_hint(next_step):
        return [next_step]
    return []


# LLM: captured_refs_payload carries compact-time refs without embedding large bodies.
# 函数用途: 摘要已读、已写和可分片读取的产物引用，供续跑模型判断哪些工作已经做过。
def captured_refs_payload(work_state: dict[str, Any]) -> dict[str, Any]:
    artifact_refs = work_state.get("artifact_refs") if isinstance(work_state.get("artifact_refs"), list) else []
    return {
        "changed_files": string_list(work_state.get("changed_files")),
        "read_files": string_list(work_state.get("read_files")),
        "artifact_refs": [artifact_ref_payload(item) for item in artifact_refs if isinstance(item, dict)],
    }


# LLM: artifact_ref_payload compresses a tool artifact row into a small resume ref.
# 函数用途: 保留 artifact id、fallback path、source path、tool 和 kind，避免把大内容塞回上下文。
def artifact_ref_payload(item: dict[str, Any]) -> dict[str, Any]:
    fallback = str(item.get("path") or "")
    return {
        "artifact_ref": str(item.get("scoped_call_id") or item.get("call_id") or fallback),
        "fallback_path": fallback,
        "source_path": str(item.get("source_path") or item.get("source_input") or ""),
        "tool": str(item.get("tool") or ""),
        "kind": str(item.get("kind") or ""),
    }


# LLM: looks_like_reader_first_recovery_hint is shared by compact packet builders.
# 函数用途: 识别“先读恢复文件”旧提示，供多处 compact 续接过滤复用。
def looks_like_reader_first_recovery_hint(value: str) -> bool:
    text = value.strip().lower()
    if not text:
        return False
    recovery_markers = ("memory-resume", "localstore", "compact_context", "work_state_snapshot", "restore_refs")
    reader_markers = ("先查看", "先读取", "read ", "inspect ", "查看", "读取")
    return any(marker in text for marker in recovery_markers) and any(marker in text for marker in reader_markers)


# LLM: string_list normalizes user/model/work-state list fields into clean strings.
# 函数用途: 只接受列表或元组，去掉空白项，避免坏类型污染 compact payload。
def string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [text for item in value if (text := str(item).strip())]


__all__ = [
    "action_first_actions",
    "captured_refs_payload",
    "looks_like_reader_first_recovery_hint",
    "resume_focus_payload",
    "string_list",
]
