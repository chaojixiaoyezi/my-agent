# LLM: Compact continue packet freezes the post-resume work state before any caller continues.
# 模块用途: 把 compact resume 的目标、约束、验收、测试、引用和 guard 结果整理成稳定继续工作包。

from __future__ import annotations

"""machine-readable continuation packet for manual, semi-auto, and auto compact resume."""

from dataclasses import dataclass
from typing import Any

from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)

COMPACT_CONTINUE_PACKET_SCHEMA = RuntimeMemorySchemaOptions("compact_continue_packet")


# LLM: CompactContinuePacketRequest bundles all resume outputs needed by downstream continuation policy.
# 类用途: 汇总 handoff、work_state、guard、推荐路径和 owner refs，生成不执行动作的继续工作包。
@dataclass(frozen=True)
class CompactContinuePacketRequest:
    metadata: dict[str, Any]
    work_state: dict[str, Any]
    consistency: dict[str, Any]
    action_guard: dict[str, Any]
    handoff: dict[str, Any]
    recommended_read_paths: list[str]
    next_actions: list[str]
    subagent_owner_refs: dict[str, Any]


# LLM: build_compact_continue_packet is pure packaging; it does not read files or run tools.
# 函数用途: 生成 compact resume 后的继续工作包，供手动恢复、半自动恢复和未来自动恢复共用。
def build_compact_continue_packet(request: CompactContinuePacketRequest) -> dict[str, Any]:
    guard = request.action_guard
    missing = _string_list(guard.get("missing_fields") or request.work_state.get("missing_fields"))
    return {
        "version": COMPACT_CONTINUE_PACKET_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_CONTINUE_PACKET_SCHEMA),
        "event_type": "compact_continue_packet",
        "apply_id": str(request.metadata.get("apply_id", "")),
        "plan_id": str(request.metadata.get("plan_id", "")),
        "owner": dict(guard.get("owner", {})),
        "ready_to_continue": bool(guard.get("allowed_to_continue")),
        "continue_mode": _continue_mode(guard),
        "automatic_tool_execution": "none",
        "work_state_snapshot": _work_state_payload(request.work_state, missing),
        "guard": _guard_payload(guard),
        "recommended_read_paths": list(request.recommended_read_paths),
        "next_actions": list(request.next_actions),
        "semi_auto": _semi_auto_payload(request.handoff, missing),
        "subagent": _subagent_payload(request.subagent_owner_refs),
        "consistency_status": str(request.consistency.get("status", "")),
        "resume_instructions": _resume_instructions(guard),
        "reserved": runtime_memory_reserved_fields(COMPACT_CONTINUE_PACKET_SCHEMA),
    }


# LLM: _work_state_payload keeps the continuation packet focused on task state, not raw artifact bodies.
# 函数用途: 摘要目标、阶段、下一步、验收、约束、测试和变更文件，缺失字段明确列出。
def _work_state_payload(work_state: dict[str, Any], missing: list[str]) -> dict[str, Any]:
    return {
        "goal": str(work_state.get("goal") or ""),
        "current_phase": str(work_state.get("phase") or ""),
        "next_step": str(work_state.get("next_step") or ""),
        "acceptance": _items_payload(work_state.get("acceptance")),
        "constraints": _items_payload(work_state.get("constraints")),
        "latest_tests": _tests_payload(work_state.get("latest_tests")),
        "changed_files": _string_list(work_state.get("changed_files")),
        "read_files": _string_list(work_state.get("read_files")),
        "missing_fields": missing,
    }


# LLM: _guard_payload exposes only continuation decision fields and never executes that decision.
# 函数用途: 固定 action guard 的状态、允许标记、下一步动作和缺失字段，给上层策略判断。
def _guard_payload(guard: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(guard.get("status", "")),
        "mode": str(guard.get("mode", "")),
        "allowed_to_continue": bool(guard.get("allowed_to_continue")),
        "allowed_next_action": str(guard.get("allowed_next_action", "")),
        "automatic_tool_execution": str(guard.get("automatic_tool_execution", "none")),
        "missing_fields": _string_list(guard.get("missing_fields")),
    }


# LLM: _semi_auto_payload points blocked resumes to explicit fact completion rather than guessing.
# 函数用途: 保留 completion prompt 状态和模板，告诉调用方半自动恢复缺什么、下一步怎么补。
def _semi_auto_payload(handoff: dict[str, Any], missing: list[str]) -> dict[str, Any]:
    completion = handoff.get("completion_prompt", {}) if isinstance(handoff.get("completion_prompt"), dict) else {}
    return {
        "status": "needs_fact_completion" if missing else "complete",
        "missing_fields": missing,
        "completion_prompt": completion,
        "automatic_fact_write": False,
    }


# LLM: _subagent_payload keeps subagent compact hooks task-local and refs-only.
# 函数用途: 摘要子代理 owner 引用和预留 hook 状态，明确不写主 memory、不运行工具。
def _subagent_payload(owner_refs: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(owner_refs.get("status", "")),
        "owner": dict(owner_refs.get("owner", {})),
        "memory_scope": str(owner_refs.get("memory_scope", "")),
        "writes_main_memory": bool(owner_refs.get("writes_main_memory", False)),
        "automatic_tool_execution": str(owner_refs.get("automatic_tool_execution", "none")),
        "refs": dict(owner_refs.get("refs", {})),
        "reserved_hooks": dict(owner_refs.get("reserved_hooks", {})),
    }


# LLM: _resume_instructions gives unattended callers a fixed stop/continue checklist.
# 函数用途: 根据 guard 状态返回短规则列表，避免上层解析自然语言继续工作。
def _resume_instructions(guard: dict[str, Any]) -> list[str]:
    if guard.get("allowed_to_continue"):
        return [
            "Read the recommended refs before continuing.",
            "Continue only within the captured goal, constraints, acceptance, and latest test state.",
            "Do not run tools automatically unless a higher-level policy explicitly allows it.",
        ]
    return [
        "Stop automated continuation.",
        "Fill missing work-state facts or inspect consistency_report before continuing.",
        "Rerun memory-compact --apply and memory-resume after facts are complete.",
    ]


# LLM: _continue_mode maps guard status to a stable mode for future scheduler policy.
# 函数用途: 把 action guard 状态转换成 automated_guarded、manual_handoff 或 blocked 三档。
def _continue_mode(guard: dict[str, Any]) -> str:
    if guard.get("allowed_to_continue"):
        return "automated_guarded"
    if guard.get("status") == "requires_user_confirmation":
        return "manual_handoff"
    return "blocked"


# LLM: _items_payload normalizes compact work-state item sections for the continue packet.
# 函数用途: 转换 acceptance/constraints 字段，保留条目、来源状态和来源路径。
def _items_payload(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    return {
        "items": _string_list(payload.get("items")),
        "source_status": str(payload.get("source_status") or "not_recorded"),
        "source_paths": _string_list(payload.get("source_paths")),
    }


# LLM: _tests_payload mirrors latest_tests shape while preserving status.
# 函数用途: 转换 latest_tests 字段，保留测试状态、条目和来源路径。
def _tests_payload(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    return {
        "status": str(payload.get("status") or "not_recorded"),
        "items": _string_list(payload.get("items")),
        "source_paths": _string_list(payload.get("source_paths")),
    }


# LLM: _string_list makes unknown JSON list fields safe for packet rendering.
# 函数用途: 把 list-like 值规整成去空白字符串列表。
def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [text for item in value if (text := str(item).strip())]


__all__ = ["CompactContinuePacketRequest", "build_compact_continue_packet"]
