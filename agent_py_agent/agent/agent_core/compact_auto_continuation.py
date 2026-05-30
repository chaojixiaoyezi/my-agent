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
    sections = [
        "# Compact Auto Continuation",
        "Status: allowed_to_continue",
        f"Apply ID: {packet.get('apply_id', '')}",
        f"Continue mode: {packet.get('continue_mode', '')}",
        f"Guard status: {guard.get('status', '')}",
        "",
        "## Goal",
        str(work_state.get("goal") or ""),
        "",
        "## Current Phase",
        str(work_state.get("current_phase") or ""),
        "",
        "## Next Step",
        str(work_state.get("next_step") or "继续当前任务目标；先核对推荐引用和最近产物，再从未完成部分推进。"),
        "",
        _items_section("## Acceptance", work_state.get("acceptance")),
        _items_section("## Constraints", work_state.get("constraints")),
        _tests_section(work_state.get("latest_tests")),
        _list_section("## Changed Files", work_state.get("changed_files")),
        _list_section("## Recommended Read Paths", packet.get("recommended_read_paths")),
        "## Continuation Rules",
        "- Continue only from the Next Step above.",
        "- Do not redo completed work.",
        "- Read recommended refs only when needed for the next action.",
        "- If optional notes are missing, continue from the captured goal and refs; only stop when source refs are broken.",
    ]
    return "\n".join(section for section in sections if section is not None).strip()


# LLM: compact_auto_continue_user_prompt keeps the resumed model turn focused on continuation, not a fresh task.
# 函数用途: 生成续跑轮的用户任务文本，避免模型把恢复包当成普通参考资料后重做任务。
def compact_auto_continue_user_prompt() -> str:
    return (
        "继续执行 Compact Auto Continuation 包里的 Next Step。"
        "不要重做已完成内容；如果恢复字段、自检或引用不完整，就停车并报告。"
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
