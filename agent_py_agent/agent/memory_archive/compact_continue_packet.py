
from __future__ import annotations

"""machine-readable continuation packet for manual, semi-auto, and auto compact resume."""

from dataclasses import dataclass, field
from typing import Any

from ..action_protocol import CompactContinuePacketEnvelope, PathRef, RunScope
from ..common.value_parsing import sequence_strings
from .compact_artifact_read_hints import artifact_read_hints_from_work_state
from .compact_resume.focus import (
    action_first_actions,
    captured_refs_payload,
    resume_focus_payload,
)
from .compact_runtime_handoff import runtime_handoff_payload
from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_schema_payload,
)

COMPACT_CONTINUE_PACKET_SCHEMA = RuntimeMemorySchemaOptions("compact_continue_packet")


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


def build_compact_continue_packet(request: CompactContinuePacketRequest) -> dict[str, Any]:
    guard = request.action_guard
    missing = sequence_strings(guard.get("missing_fields") or request.work_state.get("missing_fields"))
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
        "pending_deferred_tool_calls": _pending_deferred_tool_calls_payload(
            request.work_state.get("pending_deferred_tool_calls")
        ),
        "next_actions": action_first_actions(request.next_actions, request.work_state),
        "resume_focus": resume_focus_payload(request.work_state, request.next_actions),
        "main_context_bundle": _main_context_bundle_payload(request.main_context_bundle),
        "compaction_state": _compaction_state_payload(request.compaction_state),
        "handoff_summary": _handoff_summary_payload(request.compaction_state, request.handoff_summary),
        "semi_auto": _semi_auto_payload(request.handoff, missing, guard),
        "subagent": _subagent_payload(request.subagent_owner_refs),
        "consistency_status": str(request.consistency.get("status", "")),
        "resume_instructions": _resume_instructions(guard),
    }
    payload["typed_envelope"] = _typed_continue_packet_envelope(payload).to_dict()
    return payload


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
        next_actions=sequence_strings(payload.get("next_actions")),
        scope=RunScope(owner_type=owner_type, owner_id=owner_id),
    )


def _compact_continue_packet_id(payload: dict[str, Any]) -> str:
    apply_id = str(payload.get("apply_id") or "").strip()
    plan_id = str(payload.get("plan_id") or "").strip()
    if apply_id:
        return f"compact-continue-{apply_id}"
    if plan_id:
        return f"compact-continue-{plan_id}"
    return "compact-continue-unknown"


def _path_refs_from_recommended(value: Any, *, owner_id: str = "") -> list[PathRef]:
    return [
        PathRef(
            path=path,
            kind="recommended_read",
            owner_run_id=owner_id,
            source="compact_continue_packet.recommended_read_paths",
        )
        for path in sequence_strings(value)
    ]


def _work_state_payload(work_state: dict[str, Any], missing: list[str]) -> dict[str, Any]:
    return {
        "goal": str(work_state.get("goal") or ""),
        "current_phase": str(work_state.get("phase") or ""),
        "next_step": str(work_state.get("next_step") or ""),
        "acceptance": _items_payload(work_state.get("acceptance")),
        "constraints": _items_payload(work_state.get("constraints")),
        "latest_tests": _tests_payload(work_state.get("latest_tests")),
        "changed_files": sequence_strings(work_state.get("changed_files")),
        "read_files": sequence_strings(work_state.get("read_files")),
        "task_progress": _task_progress_payload(work_state.get("task_progress")),
        "read_coverage": _read_coverage_payload(work_state.get("read_coverage")),
        "tool_progress": _tool_progress_payload(work_state.get("tool_progress")),
        "pending_deferred_tool_calls": _pending_deferred_tool_calls_payload(
            work_state.get("pending_deferred_tool_calls")
        ),
        "runtime_handoff": runtime_handoff_payload(work_state.get("runtime_handoff")),
        "captured_refs": captured_refs_payload(work_state),
        "missing_fields": missing,
    }


def _guard_payload(guard: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(guard.get("status", "")),
        "mode": str(guard.get("mode", "")),
        "allowed_to_continue": bool(guard.get("allowed_to_continue")),
        "allowed_next_action": str(guard.get("allowed_next_action", "")),
        "automatic_tool_execution": str(guard.get("automatic_tool_execution", "none")),
        "missing_fields": sequence_strings(guard.get("missing_fields")),
    }


def _semi_auto_payload(handoff: dict[str, Any], missing: list[str], guard: dict[str, Any]) -> dict[str, Any]:
    completion = handoff.get("completion_prompt", {}) if isinstance(handoff.get("completion_prompt"), dict) else {}
    allowed = bool(guard.get("allowed_to_continue"))
    return {
        "status": "optional_notes_missing" if missing and allowed else ("needs_fact_completion" if missing else "complete"),
        "missing_fields": missing,
        "completion_prompt": completion,
        "automatic_fact_write": False,
    }


def _subagent_payload(owner_refs: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(owner_refs.get("status", "")),
        "owner": dict(owner_refs.get("owner", {})),
        "memory_scope": str(owner_refs.get("memory_scope", "")),
        "writes_main_memory": bool(owner_refs.get("writes_main_memory", False)),
        "automatic_tool_execution": str(owner_refs.get("automatic_tool_execution", "none")),
        "refs": dict(owner_refs.get("refs", {})),
        "recommended_read_paths": sequence_strings(owner_refs.get("recommended_read_paths")),
        "continuation_hooks": dict(owner_refs.get("continuation_hooks", {})),
    }


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


def _handoff_summary_payload(state_value: dict[str, Any], summary: str) -> dict[str, Any]:
    state = state_value if isinstance(state_value, dict) else {}
    return {
        "ref": str(state.get("handoff_summary_ref") or ""),
        "previous_ref": str(state.get("previous_handoff_summary_ref") or ""),
        "text": str(summary or ""),
        "authoritative": False,
    }


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


def _resume_instructions(guard: dict[str, Any]) -> list[str]:
    if guard.get("allowed_to_continue"):
        return [
            "Continue from resume_focus.next_action first.",
            "Use captured_refs to avoid repeating finished reads, writes, and dispatches.",
            "Read recommended refs only when the next action lacks facts or needs verification.",
            "If optional notes are missing, continue from the goal instead of stopping.",
            "Do not run tools automatically unless a higher-level policy explicitly allows it.",
        ]
    return [
        "Stop automated continuation.",
        "Fill missing work-state facts or inspect consistency_report before continuing.",
        "Rerun memory-compact --apply and memory-resume after facts are complete.",
    ]


def _continue_mode(guard: dict[str, Any]) -> str:
    if guard.get("allowed_to_continue"):
        return "automated_guarded"
    if guard.get("status") == "requires_user_confirmation":
        return "manual_handoff"
    return "blocked"


def _items_payload(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    return {
        "items": sequence_strings(payload.get("items")),
        "source_status": str(payload.get("source_status") or "not_recorded"),
        "source_paths": sequence_strings(payload.get("source_paths")),
    }


def _tests_payload(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    return {
        "status": str(payload.get("status") or "not_recorded"),
        "items": sequence_strings(payload.get("items")),
        "source_paths": sequence_strings(payload.get("source_paths")),
    }


def _task_progress_payload(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    if not payload:
        return {}
    result = {
        "summary": str(payload.get("summary") or ""),
        "next_action": str(payload.get("next_action") or ""),
        "counts": dict(payload.get("counts", {}) if isinstance(payload.get("counts"), dict) else {}),
        "ref": str(payload.get("ref") or ""),
        "updated_at": payload.get("updated_at", 0),
        "active_items": _progress_items(payload.get("active_items"), limit=8),
        "recent_done_items": _progress_items(payload.get("recent_done_items"), limit=24),
    }
    quality = payload.get("quality_hints") if isinstance(payload.get("quality_hints"), dict) else {}
    if quality:
        result["quality_hints"] = {
            "severity": str(quality.get("severity") or ""),
            "messages": sequence_strings(quality.get("messages"))[:4],
            "next_suggestions": sequence_strings(quality.get("next_suggestions"))[:4],
            "result_without_evidence_count": _positive_int(quality.get("result_without_evidence_count")),
            "coverage_incomplete_count": _positive_int(quality.get("coverage_incomplete_count")),
        }
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    if coverage:
        result["coverage"] = {
            "goal": str(coverage.get("goal") or ""),
            "counts": dict(coverage.get("counts", {}) if isinstance(coverage.get("counts"), dict) else {}),
            "active_targets": _progress_items(coverage.get("active_targets"), limit=12),
        }
    return {key: value for key, value in result.items() if value not in ("", [], {}, None)}


def _progress_items(value: Any, *, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in value if isinstance(value, list | tuple) else []:
        if not isinstance(item, dict):
            continue
        row = {
            "id": str(item.get("id") or ""),
            "title": str(item.get("title") or ""),
            "status": str(item.get("status") or ""),
            "notes": str(item.get("notes") or ""),
            "next": str(item.get("next") or ""),
            "evidence": sequence_strings(item.get("evidence"))[:6],
        }
        row = {key: value for key, value in row.items() if value not in ("", [], {}, None)}
        if row:
            rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _tool_progress_payload(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list | tuple):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        source_path = str(item.get("source_path") or item.get("path") or "").strip()
        artifact_ref = str(item.get("artifact_ref") or item.get("scoped_call_id") or item.get("call_id") or "").strip()
        row = {
            "tool": str(item.get("tool") or "").strip(),
            "source_path": source_path,
            "artifact_ref": artifact_ref,
            "size_bytes": _positive_int(item.get("size_bytes")),
            "offset": _optional_int(item.get("offset")),
            "next_offset": _optional_int(item.get("next_offset")),
            "total_chars": _optional_int(item.get("total_chars")),
            "start_line": _optional_int(item.get("start_line")),
            "end_line": _optional_int(item.get("end_line")),
            "next_start_line": _optional_int(item.get("next_start_line")),
            "total_lines": _optional_int(item.get("total_lines")),
        }
        rows.append({key: value for key, value in row.items() if value not in ("", [], {}, None)})
    return rows[-48:]


def _read_coverage_payload(value: Any) -> dict[str, Any]:
    coverage = value if isinstance(value, dict) else {}
    primary = coverage.get("primary") if isinstance(coverage.get("primary"), dict) else {}
    if not primary:
        return {}
    raw_sources = coverage.get("sources") if isinstance(coverage.get("sources"), list) else []
    sources = [
        row
        for item in raw_sources
        if (row := _read_coverage_source_payload(item))
    ]
    raw_incomplete_sources = coverage.get("incomplete_sources") if isinstance(coverage.get("incomplete_sources"), list) else []
    incomplete_sources = [
        row
        for item in raw_incomplete_sources
        if (row := _read_coverage_source_payload(item))
    ] or [row for row in sources if row.get("complete") is not True]
    result = {
        "schema_version": coverage.get("schema_version", 1),
        "source_count": _positive_int(coverage.get("source_count")),
        "omitted_source_count": _positive_int(coverage.get("omitted_source_count")),
        "sources": sources[:24],
        "incomplete_source_count": _positive_int(coverage.get("incomplete_source_count")) or len(incomplete_sources),
        "omitted_incomplete_source_count": _positive_int(coverage.get("omitted_incomplete_source_count")),
        "incomplete_sources": incomplete_sources[:24],
        "primary": {
            "kind": str(primary.get("kind") or "char_window"),
            "source_path": str(primary.get("source_path") or ""),
            "covered_until": _positive_int(primary.get("covered_until")),
            "covered_until_offset": _positive_int(primary.get("covered_until_offset")),
            "covered_until_line": _positive_int(primary.get("covered_until_line")),
            "total": _positive_int(primary.get("total")),
            "total_chars": _positive_int(primary.get("total_chars")),
            "total_lines": _positive_int(primary.get("total_lines")),
            "next_offset": _positive_int(primary.get("next_offset")),
            "next_start_line": _positive_int(primary.get("next_start_line")),
            "complete": bool(primary.get("complete")),
            "range_count": _positive_int(primary.get("range_count")),
            "omitted_range_count": _positive_int(primary.get("omitted_range_count")),
        },
    }
    result["primary"] = {key: item for key, item in result["primary"].items() if item not in ("", 0, [], {}, None)}
    return {key: item for key, item in result.items() if item not in ("", 0, [], {}, None)}


def _read_coverage_source_payload(item: Any) -> dict[str, Any]:
    source = item if isinstance(item, dict) else {}
    if not source:
        return {}
    result = {
        "kind": str(source.get("kind") or "char_window"),
        "source_path": str(source.get("source_path") or ""),
        "covered_until": _positive_int(source.get("covered_until")),
        "covered_until_offset": _positive_int(source.get("covered_until_offset")),
        "covered_until_line": _positive_int(source.get("covered_until_line")),
        "total": _positive_int(source.get("total")),
        "total_chars": _positive_int(source.get("total_chars")),
        "total_lines": _positive_int(source.get("total_lines")),
        "next_offset": _positive_int(source.get("next_offset")),
        "next_start_line": _positive_int(source.get("next_start_line")),
        "complete": bool(source.get("complete")),
        "range_count": _positive_int(source.get("range_count")),
        "omitted_range_count": _positive_int(source.get("omitted_range_count")),
    }
    return {key: value for key, value in result.items() if value not in ("", 0, [], {}, None)}


def _pending_deferred_tool_calls_payload(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in value if isinstance(value, list | tuple) else []:
        if not isinstance(item, dict):
            continue
        params = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
        tool = str(item.get("tool") or params.get("tool") or "").strip()
        if not tool:
            continue
        rows.append(
            {
                "kind": str(item.get("kind") or "tool_call"),
                "tool": tool,
                "call_id": str(item.get("call_id") or ""),
                "scoped_call_id": str(item.get("scoped_call_id") or ""),
                "source_input": str(item.get("source_input") or item.get("source_path") or params.get("path") or ""),
                "source_path": str(item.get("source_path") or item.get("source_input") or params.get("path") or ""),
                "parameters": {**params, "tool": tool},
                "request_id": str(item.get("request_id") or ""),
                "run_id": str(item.get("run_id") or ""),
                "task_id": str(item.get("task_id") or ""),
                "ok": False,
                "status": str(item.get("status") or "error"),
                "error_code": "CONTEXT_COMPACT_DEFERRED",
            }
        )
    return rows[-12:]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _positive_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


__all__ = ["CompactContinuePacketRequest", "build_compact_continue_packet"]
