# LLM: Compact continue packet freezes the post-resume work state before any caller continues.
# 模块用途: 把 compact resume 的目标、约束、验收、测试、引用和 guard 结果整理成稳定继续工作包。

from __future__ import annotations

"""machine-readable continuation packet for manual, semi-auto, and auto compact resume."""

from dataclasses import dataclass, field
from typing import Any

from ..action_protocol import CompactContinuePacketEnvelope, PathRef, RunScope
from .compact_artifact_read_hints import artifact_read_hints_from_work_state
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
    main_context_bundle: dict[str, Any]
    compaction_state: dict[str, Any] = field(default_factory=dict)
    handoff_summary: str = ""


# LLM: build_compact_continue_packet is pure packaging; it does not read files or run tools.
# 函数用途: 生成 compact resume 后的继续工作包，供手动恢复、半自动恢复和未来自动恢复共用。
def build_compact_continue_packet(request: CompactContinuePacketRequest) -> dict[str, Any]:
    guard = request.action_guard
    missing = _string_list(guard.get("missing_fields") or request.work_state.get("missing_fields"))
    payload = {
        "version": COMPACT_CONTINUE_PACKET_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_CONTINUE_PACKET_SCHEMA),
        "event_type": "compact_continue_packet",
        "apply_id": str(request.metadata.get("apply_id", "")),
        "plan_id": str(request.metadata.get("plan_id", "")),
        "lineage": _lineage_payload(request.metadata.get("lineage")),
        "owner": dict(guard.get("owner", {})),
        "ready_to_continue": bool(guard.get("allowed_to_continue")),
        "continue_mode": _continue_mode(guard),
        "automatic_tool_execution": "none",
        "work_state_snapshot": _work_state_payload(request.work_state, missing),
        "guard": _guard_payload(guard),
        "recommended_read_paths": list(request.recommended_read_paths),
        "artifact_read_hints": artifact_read_hints_from_work_state(request.work_state),
        "next_actions": list(request.next_actions),
        "main_context_bundle": _main_context_bundle_payload(request.main_context_bundle),
        "compaction_state": _compaction_state_payload(request.compaction_state),
        "handoff_summary": _handoff_summary_payload(request.compaction_state, request.handoff_summary),
        "semi_auto": _semi_auto_payload(request.handoff, missing, guard),
        "subagent": _subagent_payload(request.subagent_owner_refs),
        "consistency_status": str(request.consistency.get("status", "")),
        "resume_instructions": _resume_instructions(guard),
        "reserved": runtime_memory_reserved_fields(COMPACT_CONTINUE_PACKET_SCHEMA),
    }
    payload["typed_envelope"] = _typed_continue_packet_envelope(payload).to_dict()
    return payload


# LLM: _typed_continue_packet_envelope mirrors the legacy dict as an executable-safe recovery envelope.
# 函数用途: 从 continue packet 字典生成 typed envelope，供后续自动恢复按字段读取而不是解析自然语言。
def _typed_continue_packet_envelope(payload: dict[str, Any]) -> CompactContinuePacketEnvelope:
    owner = payload.get("owner", {}) if isinstance(payload.get("owner"), dict) else {}
    owner_type = str(owner.get("owner_type") or "")
    owner_id = str(owner.get("owner_id") or "")
    return CompactContinuePacketEnvelope(
        packet_id=_compact_continue_packet_id(payload),
        apply_id=str(payload.get("apply_id") or ""),
        plan_id=str(payload.get("plan_id") or ""),
        ready_to_continue=bool(payload.get("ready_to_continue")),
        continue_mode=str(payload.get("continue_mode") or ""),
        owner=dict(owner),
        work_state=dict(payload.get("work_state_snapshot", {})),
        guard=dict(payload.get("guard", {})),
        path_refs=_path_refs_from_recommended(payload.get("recommended_read_paths"), owner_id=owner_id),
        next_actions=_string_list(payload.get("next_actions")),
        scope=RunScope(owner_type=owner_type, owner_id=owner_id),
        reserved={"source": "compact_continue_packet"},
    )


# LLM: _compact_continue_packet_id produces a stable human-readable packet id.
# 函数用途: 根据 apply_id/plan_id 生成恢复包 id，缺失时使用 compact-continue-unknown。
def _compact_continue_packet_id(payload: dict[str, Any]) -> str:
    apply_id = str(payload.get("apply_id") or "").strip()
    plan_id = str(payload.get("plan_id") or "").strip()
    if apply_id:
        return f"compact-continue-{apply_id}"
    if plan_id:
        return f"compact-continue-{plan_id}"
    return "compact-continue-unknown"


# LLM: _path_refs_from_recommended keeps resume read hints as structured refs.
# 函数用途: 把 recommended_read_paths 转成 PathRef，不读取文件正文。
def _path_refs_from_recommended(value: Any, *, owner_id: str = "") -> list[PathRef]:
    return [
        PathRef(
            path=path,
            kind="recommended_read",
            owner_run_id=owner_id,
            source="compact_continue_packet.recommended_read_paths",
        )
        for path in _string_list(value)
    ]


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
# LLM: _semi_auto_payload keeps manual fact-fill hints without making missing notes look fatal.
# 函数用途: 缺少可选备注时保留补充提示；自动 guard 已放行时不再标成必须补事实。
def _semi_auto_payload(handoff: dict[str, Any], missing: list[str], guard: dict[str, Any]) -> dict[str, Any]:
    completion = handoff.get("completion_prompt", {}) if isinstance(handoff.get("completion_prompt"), dict) else {}
    allowed = bool(guard.get("allowed_to_continue"))
    return {
        "status": "optional_notes_missing" if missing and allowed else ("needs_fact_completion" if missing else "complete"),
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
        # LLM: subagent owner packets expose task-local read hints without copying main memory.
        "recommended_read_paths": _string_list(owner_refs.get("recommended_read_paths")),
        "reserved_hooks": dict(owner_refs.get("reserved_hooks", {})),
    }


# LLM: _main_context_bundle_payload carries the root run card through resume without reading its body.
# 函数用途: 摘要主代理 context bundle 路径和任务范围，供自动恢复按机器字段定位任务。
def _main_context_bundle_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"ref": "", "loaded": False, "scope": {}, "workspace_refs": {}, "error": ""}
    return {
        "ref": str(payload.get("ref", "") or ""),
        "loaded": bool(payload.get("loaded")),
        "scope": dict(payload.get("scope", {}) if isinstance(payload.get("scope"), dict) else {}),
        "workspace_refs": dict(
            payload.get("workspace_refs", {}) if isinstance(payload.get("workspace_refs"), dict) else {}
        ),
        "error": str(payload.get("error", "") or ""),
    }


# LLM: _compaction_state_payload carries compact chain identity through automatic continuation.
# 函数用途: 给继续包提供 compact id、上一轮 id、摘要路径和任务下一步，不复制大正文。
def _compaction_state_payload(value: dict[str, Any]) -> dict[str, Any]:
    state = value if isinstance(value, dict) else {}
    work = state.get("work", {}) if isinstance(state.get("work"), dict) else {}
    return {
        "compact_id": str(state.get("compact_id") or ""),
        "compact_index": _positive_int(state.get("compact_index")),
        "previous_compact_id": str(state.get("previous_compact_id") or ""),
        "handoff_summary_ref": str(state.get("handoff_summary_ref") or ""),
        "previous_handoff_summary_ref": str(state.get("previous_handoff_summary_ref") or ""),
        "goal": str(work.get("goal") or ""),
        "next_step": str(work.get("next_step") or ""),
    }


# LLM: _handoff_summary_payload separates model-readable context from machine continuation fields.
# 函数用途: 把交接摘要和引用放入继续包，明确它不是事实来源。
def _handoff_summary_payload(state_value: dict[str, Any], summary: str) -> dict[str, Any]:
    state = state_value if isinstance(state_value, dict) else {}
    return {
        "ref": str(state.get("handoff_summary_ref") or ""),
        "previous_ref": str(state.get("previous_handoff_summary_ref") or ""),
        "text": str(summary or ""),
        "authoritative": False,
    }


# LLM: _lineage_payload keeps repeated compact cycle state machine-readable in continue packets.
# 函数用途: 摘要当前 compact 是第几轮、上一轮 apply 是谁，避免自动恢复只能靠自然语言猜。
def _lineage_payload(value: Any) -> dict[str, Any]:
    lineage = value if isinstance(value, dict) else {}
    return {
        "status": str(lineage.get("status") or ""),
        "cycle_index": _positive_int(lineage.get("cycle_index")),
        "current_apply_id": str(lineage.get("current_apply_id") or ""),
        "previous_apply_id": str(lineage.get("previous_apply_id") or ""),
        "previous_metadata_ref": str(lineage.get("previous_metadata_ref") or ""),
        "previous_apply_bundle_ref": str(lineage.get("previous_apply_bundle_ref") or ""),
        "content_preserved": bool(lineage.get("content_preserved", False)),
    }


# LLM: _resume_instructions gives unattended callers a fixed stop/continue checklist.
# 函数用途: 根据 guard 状态返回短规则列表，避免上层解析自然语言继续工作。
def _resume_instructions(guard: dict[str, Any]) -> list[str]:
    if guard.get("allowed_to_continue"):
        return [
            "Read the recommended refs before continuing.",
            "Continue within the captured goal and any recorded constraints, acceptance, and latest test state.",
            "If optional notes are missing, continue from the goal instead of stopping.",
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


# LLM: _positive_int normalizes compact lineage counters for packet payloads.
# 函数用途: 将未知 JSON 值转换为非负整数，避免坏 lineage 影响 resume 包生成。
def _positive_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


__all__ = ["CompactContinuePacketRequest", "build_compact_continue_packet"]
