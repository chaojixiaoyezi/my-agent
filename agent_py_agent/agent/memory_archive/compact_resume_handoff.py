# LLM: Compact resume handoff renders a stable, readable continuation package without running tools.
# 模块用途: 根据 compact resume 的 work_state、guard 和推荐路径生成可交接的恢复包和上下文块。

from __future__ import annotations

"""handoff package for memory-resume --from-compact."""

import json
from dataclasses import dataclass
from typing import Any

from .compact_artifact_read_hints import (
    artifact_read_hint_lines,
    artifact_read_hints_from_work_state,
)
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
    fail_safe_checkpoints: list[dict[str, Any]]
    completion_prompt: dict[str, Any]
    main_context_bundle: dict[str, Any]


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
        "artifact_read_hints": artifact_read_hints_from_work_state(work_state),
        "recommended_read_paths": list(request.recommended_read_paths),
        "main_context_bundle": dict(request.main_context_bundle),
        "fail_safe_checkpoints": _fail_safe_checkpoint_payloads(request.fail_safe_checkpoints),
        "missing_fields": _string_list(work_state.get("missing_fields")),
        "completion_prompt": dict(request.completion_prompt),
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
    _extend_main_context_bundle(lines, handoff.get("main_context_bundle", {}))
    _extend_section(lines, "Fail Safe Checkpoints", _fail_safe_checkpoint_lines(handoff["fail_safe_checkpoints"]))
    _extend_section(lines, "Artifact Read Hints", artifact_read_hint_lines(handoff["artifact_read_hints"]))
    _extend_section(lines, "Must Read", handoff["recommended_read_paths"][:12])
    _extend_section(lines, "Next Actions", handoff["next_actions"])
    _extend_completion_prompt(lines, handoff.get("completion_prompt", {}))
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


# LLM: _fail_safe_checkpoint_payloads keeps recovery checkpoint refs metadata-only.
# 函数用途: 规范 memory-resume 交接包里的 fail-safe checkpoint 摘要，不展开 artifact 正文。
def _fail_safe_checkpoint_payloads(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "path": str(item.get("path", "") or ""),
            "line_no": int(item.get("line_no", 0) or 0),
            "snapshot_id": str(item.get("snapshot_id", "") or ""),
            "source": str(item.get("source", "") or ""),
            "status": str(item.get("status", "") or ""),
            "tool_calls": _tool_call_refs(item.get("tool_calls")),
            "next_actions": _string_list(item.get("next_actions")),
            "reads_artifact_bodies": False,
        }
        for item in items
    ]


# LLM: _fail_safe_checkpoint_lines renders only refs, hashes, and sizes for the manual context block.
# 函数用途: 把 fail-safe checkpoint 摘要渲染成短行，避免把大工具输出带回恢复 prompt。
def _fail_safe_checkpoint_lines(items: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in items:
        tools = item.get("tool_calls", []) if isinstance(item.get("tool_calls"), list) else []
        first_tool = tools[0] if tools and isinstance(tools[0], dict) else {}
        lines.append(
            f"{item.get('path', '')}:{item.get('line_no', 0)} "
            f"snapshot={item.get('snapshot_id', '')} "
            f"tool={first_tool.get('tool', '')} "
            f"hash={first_tool.get('output_hash', '')} "
            f"size={first_tool.get('output_size_bytes', 0)}"
        )
    return lines


# LLM: _tool_call_refs strips tool calls down to recovery identity fields.
# 函数用途: 保留工具名、调用 id、hash 和尺寸，不复制工具输出内容。
def _tool_call_refs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list | tuple):
        return []
    refs: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        refs.append({
            "tool": str(item.get("tool") or item.get("name") or ""),
            "id": str(item.get("id") or item.get("tool_call_id") or ""),
            "ok": item.get("ok"),
            "output_hash": str(item.get("output_hash", "") or ""),
            "output_size_bytes": int(item.get("output_size_bytes", 0) or 0),
            "output_externalized": str(item.get("output_externalized", "") or ""),
        })
    return refs


# LLM: _extend_section renders compact Markdown lists with stable none output.
# 函数用途: 给 context block 添加列表小节，空列表时明确写 none。
def _extend_section(lines: list[str], title: str, items: list[str]) -> None:
    lines.extend([f"## {title}", ""])
    lines.extend(f"- {item}" for item in items) if items else lines.append("- none")
    lines.append("")


# LLM: _extend_completion_prompt adds the semi-auto missing-field template only when needed.
# 函数用途: 在 context block 里展示可复制补全模板；字段齐全时不输出额外内容。
def _extend_completion_prompt(lines: list[str], completion: dict[str, Any]) -> None:
    if completion.get("status") != "needs_user_input":
        return
    lines.extend(["## Completion Prompt", "", completion.get("prompt_template", ""), ""])


# LLM: _extend_main_context_bundle renders the root run card as refs, not as large task bodies.
# 函数用途: 在 compact resume 上下文块中展示主代理 context bundle 的路径、任务范围和真实工作区。
def _extend_main_context_bundle(lines: list[str], payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict) or not payload.get("ref"):
        return
    scope = payload.get("scope", {}) if isinstance(payload.get("scope"), dict) else {}
    workspace = payload.get("workspace_refs", {}) if isinstance(payload.get("workspace_refs"), dict) else {}
    lines.extend([
        "## Main Context Bundle",
        "",
        f"- ref: {payload.get('ref', '')}",
        f"- loaded: {str(bool(payload.get('loaded'))).lower()}",
        f"- request_id: {scope.get('request_id', '')}",
        f"- run_id: {scope.get('run_id', '')}",
        f"- task_id: {scope.get('task_id', '')}",
        f"- primary_workspace_root: {workspace.get('primary_workspace_root', '')}",
        "",
    ])


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
