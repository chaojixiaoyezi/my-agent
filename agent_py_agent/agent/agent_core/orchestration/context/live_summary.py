
from __future__ import annotations

import json
from typing import Any

_ORCHESTRATION_TOOLS = {
    "inspect_collaboration",
    "create_subagents",
    "dispatch_subagents",
    "inspect_agent_tree",
    "raise_collaboration",
    "update_collaboration",
    "schedule_child_subagents",
}
_MAX_INLINE_JSON = 900
_MAX_INLINE_TEXT = 500
_TOP_LEVEL_ACTION_KEYS = (
    "scope",
    "root_id",
    "status_buckets",
    "blocked",
    "reason",
    "created",
    "case_id",
    "request_id",
    "case_ref",
    "request_ref",
    "target_agent_ids",
    "required_capabilities",
    "request_count",
    "request_history_count",
    "evidence_count",
    "participant_count",
    "decision_count",
    "case_window",
    "collection_result",
    "requires_main_agent",
    "allowed_tools",
    "subagent_workspace",
    "completion_status",
    "completion_risk",
    "blocking_run_ids",
    "repair_advice",
    "created_run_ids",
    "planned_count",
    "runner_selection_recovery",
    "quality_advice",
    "current_turn_run_state",
    "next_action",
)


def orchestration_live_summary(result, archive_record: dict[str, object]) -> str:
    tool = str(getattr(result, "tool_name", "") or "")
    payload = _json_object(str(getattr(result, "output", "") or ""))
    if tool in _ORCHESTRATION_TOOLS and payload is not None:
        return orchestration_payload_summary(tool, payload, archive_record)
    if tool == "read_artifact" and payload is not None:
        return _render_artifact_read_summary(payload, archive_record)
    return ""


def orchestration_payload_summary(
    tool: str,
    payload: dict[str, Any],
    archive_record: dict[str, object] | None = None,
) -> str:
    """Project a full typed orchestration payload before generic output truncation."""

    return _render_orchestration_summary(
        "orchestration_summary",
        str(tool or ""),
        payload,
        archive_record or {},
    )


def _render_artifact_read_summary(
    payload: dict[str, Any],
    archive_record: dict[str, object],
) -> str:
    inner_tool = str(payload.get("tool") or "")
    content = str(payload.get("content") or "")
    inner = _json_object(content)
    if inner_tool in _ORCHESTRATION_TOOLS and inner is not None:
        return _render_orchestration_summary("artifact_read_summary", inner_tool, inner, _source_artifact_record(payload))
    lines = [
        "[tool=read_artifact; status=ok]",
        "artifact_read_summary:",
        "- policy: artifact body was requested explicitly; live prompt keeps only a bounded preview.",
        f"- source_artifact_ref: {payload.get('artifact_ref', '')}",
        f"- source_artifact_tool: {inner_tool}",
        f"- source_call_id: {payload.get('call_id', '')}",
        f"- content_offset: {payload.get('content_offset', 0)}",
        f"- content_chars: {payload.get('content_chars', len(content))}",
        f"- truncated: {payload.get('truncated', False)}",
        f"- content_preview: {_clip(content)}",
        "- continue_read_policy: read source_artifact_ref with offset/mode if more evidence is needed; do not read a read_artifact wrapper path.",
    ]
    return "\n".join(lines)


def _render_orchestration_summary(
    heading: str,
    tool: str,
    payload: dict[str, Any],
    archive_record: dict[str, object],
) -> str:
    ok = archive_record.get("ok") is True
    lines = [
        f"[tool={tool}; status={'ok' if ok else 'error'}]",
        f"{heading}:",
        "- policy: refs-first orchestration output; do not read artifact/file bodies unless a specific evidence ref requires it.",
    ]
    lines.extend(_direct_children_lines(payload.get("direct_children")))
    lines.extend(top_level_action_lines(payload))
    result_index = payload.get("result_refs_by_run")
    if not isinstance(result_index, list):
        result_index = payload.get("child_result_index")
    lines.extend(_result_refs_by_run_lines(result_index))
    lines.extend(_ref_lines(payload))
    lines.extend(_summary_lines(payload))
    lines.extend(_archive_pointer_lines(archive_record, include_read_hint=False))
    return "\n".join(lines)


def top_level_action_lines(payload: dict[str, Any]) -> list[str]:
    return [
        f"- {key}: {_json_inline(payload.get(key))}"
        for key in _TOP_LEVEL_ACTION_KEYS
        if key in payload and payload.get(key) not in (None, "", [], {})
    ]


def _direct_children_lines(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    lines = [
        f"- parent_run_id: {value.get('parent_run_id', '')}",
        f"- direct_children_total: {value.get('total', 0)}",
        f"- direct_children_status: {_json_inline(value.get('by_status', {}))}",
        f"- next_action: {value.get('next_action', '')}",
    ]
    for key in (
        "unfinished_run_ids",
        "recovery_run_ids",
        "rejected_acceptance_run_ids",
        "running_run_ids",
        "planning_run_ids",
    ):
        if value.get(key):
            lines.append(f"- {key}: {_json_inline(value.get(key))}")
    if value.get("quality_advice"):
        lines.append(f"- quality_advice: {_json_inline(value.get('quality_advice'))}")
    lines.extend(_direct_children_recovery_lines(value))
    lines.extend(_direct_children_repair_lines(value))
    lines.extend(_direct_children_suggested_tool_lines(value))
    return lines


def _direct_children_recovery_lines(value: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if value.get("recovery_action_counts"):
        lines.append(f"- recovery_action_counts: {_json_inline(value.get('recovery_action_counts'))}")
    if value.get("recovery_mode_counts"):
        lines.append(f"- recovery_mode_counts: {_json_inline(value.get('recovery_mode_counts'))}")
    if value.get("recovery_batches"):
        lines.append(f"- recovery_batches: {_json_inline(value.get('recovery_batches'))}")
    if value.get("recovery_strategies"):
        lines.append(f"- recovery_strategy_preview: {_json_inline(_strategy_preview(value.get('recovery_strategies')))}")
    return lines


def _strategy_preview(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    preview: list[dict[str, object]] = []
    for item in value[:3]:
        if not isinstance(item, dict):
            continue
        preview.append(
            {
                "run_id": item.get("run_id", ""),
                "recommended_action": item.get("recommended_action", ""),
                "runner_instruction": _clip(item.get("runner_instruction", ""), limit=220),
            }
        )
    return preview


def _direct_children_repair_lines(value: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if value.get("qa_repair_advice"):
        lines.append(f"- qa_repair_advice: {_json_inline(value.get('qa_repair_advice'))}")
    if value.get("repair_wave_deferred_by_recovery"):
        lines.append("- repair_wave_deferred_by_recovery: true")
    return lines


def _direct_children_suggested_tool_lines(value: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if value.get("suggested_tool_call"):
        lines.append(f"- suggested_tool_call: {_json_inline(value.get('suggested_tool_call'))}")
    if value.get("suggested_recovery_child_tool_call"):
        lines.append(
            f"- suggested_recovery_child_tool_call: {_json_inline(value.get('suggested_recovery_child_tool_call'))}"
        )
    return lines


def _result_refs_by_run_lines(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        return []
    lines = ["- result_refs_by_run:"]
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        run_id = item.get("run_id", "")
        status = f"{item.get('status', '')}/{item.get('verification_status', '')}"
        artifact_ids = _json_inline(item.get("primary_artifact_ids") or [])
        artifacts = _json_inline(item.get("primary_artifact_refs") or [])
        expected_outputs = _json_inline(item.get("expected_outputs") or [])
        read_order = _json_inline(item.get("read_order") or [])
        artifact_summaries = item.get("primary_artifact_summaries") or []
        run_closeout_ref = item.get("run_closeout_ref") or item.get("output_json", "")
        summary = _clip(item.get("summary", ""), limit=220)
        lines.append(
            f"  - run_id={run_id} status={status} "
            f"primary_artifact_ids={artifact_ids} primary_artifact_refs={artifacts} "
            f"expected_outputs={expected_outputs} read_order={read_order}"
        )
        if artifact_summaries:
            lines.append(f"    artifact_summaries={_json_inline(artifact_summaries)}")
        if run_closeout_ref:
            lines.append(f"    run_closeout_ref={run_closeout_ref}")
        if summary:
            lines.append(f"    summary={summary}")
    lines.append(
        "- result_ref_policy: read read_order first, then primary_artifact_refs; "
        "expected_outputs are declared targets, not readable results until they exist or are registered; "
        "do not guess child filenames; run_closeout_ref and internal final reports are diagnostic progress refs, "
        "not the first child result."
    )
    return lines


def _summary_lines(payload: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if payload.get("summary"):
        lines.append(f"- summary: {_json_inline(payload.get('summary'))}")
    bulky = payload.get("records")
    if isinstance(bulky, list):
        lines.append(f"- record_count: {len(bulky)}")
    if payload.get("dispatch_json"):
        lines.append(f"- dispatch_json: {payload.get('dispatch_json')}")
    if payload.get("dispatch_md"):
        lines.append(f"- dispatch_md: {payload.get('dispatch_md')}")
    return lines


def _ref_lines(payload: dict[str, Any]) -> list[str]:
    artifact_ids = _refs_from_payload(payload, "deliverable_artifact_ids", item_key="artifact_ids")
    artifact_refs = _refs_from_payload(payload, "deliverable_artifact_refs", item_key="artifact_refs")
    evidence_refs = _refs_from_payload(payload, "deliverable_evidence_refs", item_key="evidence_refs")
    lines: list[str] = []
    if artifact_ids:
        lines.append(f"- deliverable_artifact_ids: {_json_inline(artifact_ids)}")
    if artifact_refs:
        lines.append(f"- deliverable_artifact_refs: {_json_inline(artifact_refs)}")
        lines.append(
            "- refs_policy: use deliverable_artifact_ids/artifact_refs from registry first; "
            "do not guess task_dir child paths."
        )
    if evidence_refs:
        lines.append(f"- deliverable_evidence_refs: {_json_inline(evidence_refs)}")
    return lines


def _refs_from_payload(payload: dict[str, Any], top_key: str, *, item_key: str, limit: int = 12) -> list[str]:
    refs = _string_refs(payload.get(top_key), limit=limit)
    if refs:
        return refs
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    collected: list[str] = []
    seen: set[str] = set()
    for item in items:
        if _collect_item_refs((collected, seen), item, item_key, limit):
            return collected
    return collected


def _collect_item_refs(collection: tuple[list[str], set[str]], item: object, item_key: str, limit: int) -> bool:
    if not isinstance(item, dict):
        return False
    target, seen = collection
    for ref in _string_refs(item.get(item_key), limit=limit):
        if ref in seen:
            continue
        seen.add(ref)
        target.append(ref)
        if len(target) >= limit:
            return True
    return False


def _string_refs(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    refs: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        refs.append(text)
        if len(refs) >= limit:
            break
    return refs


def _archive_pointer_lines(archive_record: dict[str, object], *, include_read_hint: bool) -> list[str]:
    if not any(
        archive_record.get(key)
        for key in ("call_id", "id", "scoped_call_id", "output_hash", "output_size_bytes")
    ):
        return []
    lines = [
        f"- output_call_id: {archive_record.get('call_id') or archive_record.get('id', '')}",
        f"- output_scoped_call_id: {archive_record.get('scoped_call_id', '')}",
        "- artifact_ref_policy: use output_scoped_call_id for read_artifact; absolute blob paths are archival only.",
        f"- output_hash: {archive_record.get('output_hash', '')}",
        f"- output_size_bytes: {archive_record.get('output_size_bytes', 0)}",
    ]
    if include_read_hint:
        lines.append("- read_artifact_hint: read only a narrow evidence slice when the compact summary is insufficient.")
    return lines


def _source_artifact_record(payload: dict[str, Any]) -> dict[str, object]:
    return {
        "output_path": "",
        "artifact_ref": str(payload.get("artifact_ref") or ""),
        "call_id": str(payload.get("call_id") or ""),
        "scoped_call_id": str(payload.get("scoped_call_id") or ""),
        "output_hash": str(payload.get("sha256") or ""),
        "output_size_bytes": int(payload.get("size_bytes") or payload.get("content_chars") or 0),
        "ok": payload.get("ok") is True,
    }


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _json_inline(value: object, *, limit: int = _MAX_INLINE_JSON) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = repr(value)
    return _clip(text, limit=limit)


def _clip(value: object, *, limit: int = _MAX_INLINE_TEXT) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit].rstrip() + f"...[truncated {len(text) - limit} chars]"
