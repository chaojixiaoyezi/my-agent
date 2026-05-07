# LLM: Compact resume handoff renders a stable, readable continuation package without running tools.
# 模块用途: 根据 compact resume 的 work_state、guard 和推荐路径生成可交接的恢复包和上下文块。

from __future__ import annotations

"""handoff package for memory-resume --from-compact."""

import json
from dataclasses import dataclass
from typing import Any

from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)

COMPACT_RESUME_HANDOFF_SCHEMA = RuntimeMemorySchemaOptions("compact_resume_handoff")


# LLM: CompactResumeHandoffRequest keeps handoff rendering input explicit and bundle-based.
# 类用途: 汇总 compact resume 交接包所需的 work_state、guard、读取路径和下一步动作。
@dataclass(frozen=True)
class CompactResumeHandoffRequest:
    metadata: dict[str, Any]
    work_state: dict[str, Any]
    consistency: dict[str, Any]
    action_guard: dict[str, Any]
    recommended_read_paths: list[str]
    next_actions: list[str]


# LLM: build_compact_resume_handoff is read-only and makes resume output easy for humans and agents.
# 函数用途: 生成包含目标、阶段、验收、约束、测试、推荐路径和 guard 状态的恢复交接包。
def build_compact_resume_handoff(request: CompactResumeHandoffRequest) -> dict[str, Any]:
    work_state = request.work_state
    return {
        "version": COMPACT_RESUME_HANDOFF_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_RESUME_HANDOFF_SCHEMA),
        "event_type": "compact_resume_handoff",
        "apply_id": str(request.metadata.get("apply_id", "")),
        "plan_id": str(request.metadata.get("plan_id", "")),
        "goal": str(work_state.get("goal") or ""),
        "current_phase": str(work_state.get("phase") or ""),
        "next_step": str(work_state.get("next_step") or ""),
        "next_actions": list(request.next_actions),
        "acceptance": _items_payload(work_state.get("acceptance")),
        "constraints": _items_payload(work_state.get("constraints")),
        "latest_tests": _tests_payload(work_state.get("latest_tests")),
        "changed_files": _string_list(work_state.get("changed_files")),
        "read_files": _string_list(work_state.get("read_files")),
        "recommended_read_paths": list(request.recommended_read_paths),
        "missing_fields": _string_list(work_state.get("missing_fields")),
        "consistency_status": str(request.consistency.get("status", "")),
        "action_guard": _action_guard_payload(request.action_guard),
        "reserved": runtime_memory_reserved_fields(COMPACT_RESUME_HANDOFF_SCHEMA),
    }


# LLM: render_compact_resume_context_block creates the pasteable context for manual or semi-auto resume.
# 函数用途: 把 handoff 渲染成模型和人都容易读的上下文块，不包含原始大文件正文。
def render_compact_resume_context_block(handoff: dict[str, Any]) -> str:
    lines = [
        "# Compact Resume Context",
        "",
        "- authority: compact context is an entrypoint; source refs remain the facts.",
        f"- apply_id: {handoff['apply_id']}",
        f"- plan_id: {handoff['plan_id']}",
        f"- consistency_status: {handoff['consistency_status']}",
        f"- action_guard_status: {handoff['action_guard']['status']}",
        f"- action_guard_next: {handoff['action_guard']['allowed_next_action']}",
        f"- goal: {handoff['goal'] or 'unknown'}",
        f"- current_phase: {handoff['current_phase'] or 'unknown'}",
        f"- next_step: {handoff['next_step'] or 'unknown'}",
        "- missing_fields: " + json.dumps(handoff["missing_fields"], ensure_ascii=False),
        "",
    ]
    _extend_section(lines, "Acceptance", handoff["acceptance"]["items"])
    _extend_section(lines, "Constraints", handoff["constraints"]["items"])
    _extend_section(lines, "Latest Tests", handoff["latest_tests"]["items"])
    _extend_section(lines, "Changed Files", handoff["changed_files"])
    _extend_section(lines, "Must Read", handoff["recommended_read_paths"][:12])
    _extend_section(lines, "Next Actions", handoff["next_actions"])
    return "\n".join(lines)


# LLM: _items_payload normalizes acceptance and constraints while preserving source paths.
# 函数用途: 将 work_state 中的 items/source_status/source_paths 规范成交接包字段。
def _items_payload(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    return {
        "items": _string_list(payload.get("items")),
        "source_status": str(payload.get("source_status") or "not_recorded"),
        "source_paths": _string_list(payload.get("source_paths")),
    }


# LLM: _tests_payload normalizes latest_tests while preserving its status key.
# 函数用途: 将 work_state.latest_tests 规范成交接包字段，方便 CLI 和上下文块展示。
def _tests_payload(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    return {
        "status": str(payload.get("status") or "not_recorded"),
        "items": _string_list(payload.get("items")),
        "source_paths": _string_list(payload.get("source_paths")),
    }


# LLM: _action_guard_payload keeps only the action guard fields needed for handoff decisions.
# 函数用途: 摘要 action guard 状态、是否允许继续、下一步和缺失字段。
def _action_guard_payload(action_guard: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(action_guard.get("status", "")),
        "mode": str(action_guard.get("mode", "")),
        "allowed_to_continue": bool(action_guard.get("allowed_to_continue")),
        "allowed_next_action": str(action_guard.get("allowed_next_action", "")),
        "automatic_tool_execution": str(action_guard.get("automatic_tool_execution", "none")),
        "missing_fields": _string_list(action_guard.get("missing_fields")),
    }


# LLM: _extend_section renders compact Markdown lists with stable none output.
# 函数用途: 给 context block 添加列表小节，空列表时明确写 none。
def _extend_section(lines: list[str], title: str, items: list[str]) -> None:
    lines.extend([f"## {title}", ""])
    lines.extend(f"- {item}" for item in items) if items else lines.append("- none")
    lines.append("")


# LLM: _string_list normalizes unknown JSON values into readable short strings.
# 函数用途: 把任意 list-like 字段转成去空白字符串列表，避免交接包写入复杂对象。
def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [text for item in value if (text := str(item).strip())]


__all__ = [
    "COMPACT_RESUME_HANDOFF_SCHEMA",
    "CompactResumeHandoffRequest",
    "build_compact_resume_handoff",
    "render_compact_resume_context_block",
]
