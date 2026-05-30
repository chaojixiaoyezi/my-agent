# LLM: Compact auto continuation bridges a ready continue packet into the next guarded model turn.
# 模块用途: 把自动 compact/resume 生成的继续包渲染成 prompt 注入块；空转续接会返回，有进展时可继续多轮 compact。

from __future__ import annotations

"""main-agent auto continuation helpers for compact/resume."""

from dataclasses import dataclass
from typing import Any


# LLM: CompactAutoContinuationDecision is the small boundary between finalization and the next run.
# 类用途: 描述一次自动 compact 后是否要进入下一轮模型调用，以及要注入什么内容。
@dataclass(frozen=True)
class CompactAutoContinuationDecision:
    should_continue: bool
    reason: str
    injection: str = ""
    user_prompt: str = ""


# LLM: compact_auto_continuation_decision keeps unattended continuation strictly packet-driven.
# 函数用途: 根据 run 结果、continue packet 和续跑深度判断是否允许自动进入下一轮。
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


# LLM: build_compact_auto_continue_injection renders a compact packet for the next model prompt.
# 函数用途: 把 continue packet 转成 LLM 可读的恢复交接块，要求从 next_step 继续且不要重做。
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


# LLM: compact_auto_continue_user_prompt keeps the resumed model turn focused on continuation, not a fresh task.
# 函数用途: 生成续跑轮的用户任务文本，避免模型把恢复包当成普通参考资料后重做任务。
def compact_auto_continue_user_prompt() -> str:
    return (
        "继续当前任务的未完成部分。"
        "不要重做已完成内容；优先推进未完成部分，必要时才读取恢复引用。"
    )


# LLM: mark_compact_auto_continued annotates the returned result without changing the core result contract.
# 函数用途: 在续跑成功后给最终 AgentRunResult 标记来源 apply_id 和续跑深度。
def mark_compact_auto_continued(result: Any, source_result: Any, *, depth: int) -> Any:
    result.memory_compact_auto_continued = True
    result.memory_compact_auto_continued_from_apply_id = getattr(source_result, "memory_compact_auto_apply_id", "")
    result.memory_compact_auto_continuation_depth = depth
    return result


# LLM: _result_ready requires both the finalization flags and the packet-level ready bit.
# 函数用途: 判断 run 结果是否真的已经通过 auto guard，避免只凭单个字段误续跑。
def _result_ready(result: Any, packet: dict[str, Any]) -> bool:
    return bool(
        getattr(result, "memory_compact_auto_allowed_to_continue", False)
        and getattr(result, "memory_compact_auto_continue_ready", False)
        and getattr(result, "memory_compact_auto_apply_id", "")
        and packet.get("ready_to_continue")
    )


# LLM: _continue_packet normalizes optional result packet storage.
# 函数用途: 从 AgentRunResult 中取继续包；不存在或形态异常时返回空对象。
def _continue_packet(result: Any) -> dict[str, Any]:
    packet = getattr(result, "memory_compact_auto_continue_packet", None)
    return packet if isinstance(packet, dict) else {}


# LLM: _items_section renders acceptance/constraint payloads without leaking source metadata into prose.
# 函数用途: 渲染验收或约束条目；条目为空时明确标记 not recorded。
def _items_section(title: str, payload: Any) -> str:
    items = _items(payload.get("items")) if isinstance(payload, dict) else []
    return _list_section(title, items or ["<not recorded>"])


# LLM: _tests_section keeps latest test status visible to the resumed model.
# 函数用途: 渲染最近测试状态和测试条目，让续跑模型知道能否继续或需要先补测。
def _tests_section(payload: Any) -> str:
    tests = _items(payload.get("items")) if isinstance(payload, dict) else []
    status = str(payload.get("status") or "not_recorded") if isinstance(payload, dict) else "not_recorded"
    return "\n".join(["## Latest Tests", f"Status: {status}", *_bullet_lines(tests or ["<not recorded>"])])


# LLM: _resume_focus builds an action-first view even for older continue packets.
# 函数用途: 新包优先使用 resume_focus；旧包从 next_actions/next_step 和 work_state refs 回填，保持兼容。
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


# LLM: _resume_focus_section tells the resumed model what to do before reading backup files.
# 函数用途: 渲染 compact 续接的行动焦点，避免恢复后先重读 compact 文件。
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


# LLM: _captured_refs_section shows already captured refs without embedding large content.
# 函数用途: 渲染已读、已写和归档产物引用，让续接模型知道哪些事实已有记录。
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


# LLM: _artifact_ref_items normalizes mixed artifact hint rows into readable refs.
# 函数用途: 从 compact artifact hints 中提取可展示的 artifact_ref 或 fallback_path。
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


# LLM: _render_artifact_ref preserves both archive id and original source path when available.
# 函数用途: 把单条产物引用渲染成紧凑文本，保留 source_path 帮助模型判断来源。
def _render_artifact_ref(item: dict[str, Any]) -> str:
    ref = str(item.get("artifact_ref") or item.get("fallback_path") or "").strip()
    source = str(item.get("source_path") or "").strip()
    if source and ref:
        return f"{ref} (source: {source})"
    return ref or source


# LLM: _prefixed_lines keeps captured-ref labels stable for compact continuation prompts.
# 函数用途: 给引用列表加统一标签，便于模型扫描已捕获事实。
def _prefixed_lines(label: str, values: list[str]) -> list[str]:
    return [f"- {label}: {item}" for item in values if item]


# LLM: _action_first_items filters out reader-first legacy recovery hints.
# 函数用途: 只保留真正推进任务的下一步动作，避免续跑先翻恢复包。
def _action_first_items(items: list[str]) -> list[str]:
    return [item for item in items if not _looks_like_reader_first_recovery_hint(item)]


# LLM: _looks_like_reader_first_recovery_hint recognizes stale compact-read instructions.
# 函数用途: 判断一句提示是否只是要求读取 compact/localstore 恢复材料。
def _looks_like_reader_first_recovery_hint(value: str) -> bool:
    text = value.strip().lower()
    recovery_markers = ("memory-resume", "localstore", "compact_context", "work_state_snapshot", "restore_refs")
    reader_markers = ("先查看", "先读取", "read ", "inspect ", "查看", "读取")
    return any(marker in text for marker in recovery_markers) and any(marker in text for marker in reader_markers)


# LLM: _list_section renders short string lists with a stable heading.
# 函数用途: 把文件、路径和普通列表渲染成 markdown bullet；空列表显示 none。
def _list_section(title: str, values: Any) -> str:
    return "\n".join([title, *_bullet_lines(_items(values) or ["<none>"])])


# LLM: _bullet_lines makes compact packet lists easy for the model to scan.
# 函数用途: 给字符串列表加 markdown bullet，并去除空白。
def _bullet_lines(values: list[str]) -> list[str]:
    return [f"- {item}" for item in values if item]


# LLM: _items normalizes packet list fields defensively.
# 函数用途: 把未知值转成短字符串列表，避免 injection 渲染时抛错。
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
