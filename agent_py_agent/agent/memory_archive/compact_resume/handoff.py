
from __future__ import annotations

"""handoff package for memory-resume --from-compact."""

import json
from dataclasses import dataclass, field
from typing import Any

from ...common.value_parsing import sequence_strings
from ..compact_artifact_read_hints import (
    artifact_read_hint_lines,
    artifact_read_hints_from_work_state,
)
from ..compact_runtime_handoff import render_runtime_handoff_lines, runtime_handoff_payload
from ..schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_schema_payload,
)

COMPACT_RESUME_HANDOFF_SCHEMA = RuntimeMemorySchemaOptions("compact_resume_handoff")


@dataclass(frozen=True)
class CompactResumeHandoffRequest:
    metadata: dict[str, Any]
    work_state: dict[str, Any]
    artifact_load_errors: list[dict[str, object]]
    consistency: dict[str, Any]
    action_guard: dict[str, Any]
    recommended_read_paths: list[str]
    next_actions: list[str]
    fail_safe_checkpoints: list[dict[str, Any]]
    fail_safe_checkpoint_load_errors: list[dict[str, Any]]
    completion_prompt: dict[str, Any]
    main_context_bundle: dict[str, Any]
    compaction_state: dict[str, Any] = field(default_factory=dict)
    handoff_summary: str = ""


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
        "changed_files": sequence_strings(work_state.get("changed_files")),
        "read_files": sequence_strings(work_state.get("read_files")),
        "artifact_read_hints": artifact_read_hints_from_work_state(work_state),
        "source_load_errors": _load_error_payloads(work_state.get("source_load_errors", [])),
        "artifact_load_errors": _load_error_payloads(request.artifact_load_errors),
        "recommended_read_paths": list(request.recommended_read_paths),
        "main_context_bundle": dict(request.main_context_bundle),
        "runtime_handoff": runtime_handoff_payload(work_state.get("runtime_handoff")),
        "compaction_state": _compaction_state_payload(request.compaction_state),
        "handoff_summary": _handoff_summary_payload(request.compaction_state, request.handoff_summary),
        "fail_safe_checkpoints": _fail_safe_checkpoint_payloads(request.fail_safe_checkpoints),
        "fail_safe_checkpoint_load_errors": _load_error_payloads(request.fail_safe_checkpoint_load_errors),
        "missing_fields": sequence_strings(work_state.get("missing_fields")),
        "completion_prompt": dict(request.completion_prompt),
        "consistency_status": str(request.consistency.get("status", "")),
        "action_guard": _action_guard_payload(request.action_guard),
    }


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
    _extend_handoff_summary(lines, handoff.get("handoff_summary", {}))
    _extend_main_context_bundle(lines, handoff.get("main_context_bundle", {}))
    lines.extend(render_runtime_handoff_lines(handoff.get("runtime_handoff", {})))
    _extend_section(lines, "Fail Safe Checkpoints", _fail_safe_checkpoint_lines(handoff["fail_safe_checkpoints"]))
    _extend_section(
        lines,
        "Fail Safe Checkpoint Load Errors",
        _load_error_lines(handoff["fail_safe_checkpoint_load_errors"]),
    )
    _extend_section(lines, "Artifact Read Hints", artifact_read_hint_lines(handoff["artifact_read_hints"]))
    _extend_section(lines, "Source Load Errors", _load_error_lines(handoff["source_load_errors"]))
    _extend_section(lines, "Compact Artifact Load Errors", _load_error_lines(handoff["artifact_load_errors"]))
    _extend_section(lines, "Must Read", handoff["recommended_read_paths"][:12])
    _extend_section(lines, "Next Actions", handoff["next_actions"])
    _extend_completion_prompt(lines, handoff.get("completion_prompt", {}))
    return "\n".join(lines)


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


def _action_guard_payload(action_guard: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(action_guard.get("status", "")),
        "mode": str(action_guard.get("mode", "")),
        "allowed_to_continue": bool(action_guard.get("allowed_to_continue")),
        "allowed_next_action": str(action_guard.get("allowed_next_action", "")),
        "automatic_tool_execution": str(action_guard.get("automatic_tool_execution", "none")),
        "missing_fields": sequence_strings(action_guard.get("missing_fields")),
    }


def _compaction_state_payload(value: Any) -> dict[str, Any]:
    state = value if isinstance(value, dict) else {}
    work = state.get("work", {}) if isinstance(state.get("work"), dict) else {}
    return {
        "compact_id": str(state.get("compact_id") or ""),
        "compact_index": int(state.get("compact_index", 0) or 0),
        "previous_compact_id": str(state.get("previous_compact_id") or ""),
        "handoff_summary_ref": str(state.get("handoff_summary_ref") or ""),
        "previous_handoff_summary_ref": str(state.get("previous_handoff_summary_ref") or ""),
        "goal": str(work.get("goal") or ""),
        "next_step": str(work.get("next_step") or ""),
    }


def _handoff_summary_payload(state_value: Any, summary: str) -> dict[str, Any]:
    state = state_value if isinstance(state_value, dict) else {}
    return {
        "ref": str(state.get("handoff_summary_ref") or ""),
        "previous_ref": str(state.get("previous_handoff_summary_ref") or ""),
        "text": str(summary or ""),
        "authoritative": False,
    }


def _fail_safe_checkpoint_payloads(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "path": str(item.get("path", "") or ""),
            "line_no": int(item.get("line_no", 0) or 0),
            "snapshot_id": str(item.get("snapshot_id", "") or ""),
            "source": str(item.get("source", "") or ""),
            "status": str(item.get("status", "") or ""),
            "tool_calls": _tool_call_refs(item.get("tool_calls")),
            "next_actions": sequence_strings(item.get("next_actions")),
            "reads_artifact_bodies": False,
        }
        for item in items
    ]


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


def _load_error_payloads(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        payload = {
            "context": str(item.get("context") or ""),
            "category": str(item.get("category") or ""),
            "error_type": str(item.get("error_type") or ""),
            "message": str(item.get("message") or ""),
            "path": str(item.get("path") or ""),
            "model_message": str(item.get("model_message") or ""),
        }
        if item.get("line_no"):
            payload["line_no"] = int(item.get("line_no", 0) or 0)
        payloads.append(payload)
    return payloads


def _load_error_lines(items: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in items:
        location = str(item.get("path") or "")
        if item.get("line_no"):
            location += f":{item.get('line_no')}"
        lines.append(
            f"{location} context={item.get('context', '')} "
            f"category={item.get('category', '')} error={item.get('error_type', '')}"
        )
    return lines


def _tool_call_refs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list | tuple):
        return []
    refs: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        refs.append({
            "tool": str(item.get("tool") or ""),
            "id": str(item.get("id") or item.get("tool_call_id") or ""),
            "ok": item.get("ok"),
            "output_hash": str(item.get("output_hash", "") or ""),
            "output_size_bytes": int(item.get("output_size_bytes", 0) or 0),
            "output_externalized": str(item.get("output_externalized", "") or ""),
        })
    return refs


def _extend_section(lines: list[str], title: str, items: list[str]) -> None:
    lines.extend([f"## {title}", ""])
    lines.extend(f"- {item}" for item in items) if items else lines.append("- none")
    lines.append("")


def _extend_completion_prompt(lines: list[str], completion: dict[str, Any]) -> None:
    if completion.get("status") != "needs_user_input":
        return
    lines.extend(["## Completion Prompt", "", completion.get("prompt_template", ""), ""])


def _extend_handoff_summary(lines: list[str], payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict) or not payload.get("text"):
        return
    lines.extend([
        "## Compaction Handoff Summary",
        "",
        f"- ref: {payload.get('ref', '')}",
        f"- previous_ref: {payload.get('previous_ref', '')}",
        "",
        str(payload.get("text") or "").strip(),
        "",
    ])


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


__all__ = [
    "COMPACT_RESUME_HANDOFF_SCHEMA",
    "CompactResumeHandoffRequest",
    "build_compact_resume_handoff",
    "render_compact_resume_context_block",
]
