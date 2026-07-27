
from __future__ import annotations

"""work-state snapshot helpers for compact apply."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...common.value_parsing import dedupe_strings
from ..compact_tool_output_refs import tool_call_refs, tool_output_artifact_refs
from ..schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_schema_payload,
)

COMPACT_WORK_STATE_SNAPSHOT_SCHEMA = RuntimeMemorySchemaOptions("compact_work_state_snapshot")


@dataclass(frozen=True)
class WorkStateSnapshotRequest:
    plan: dict[str, Any]
    restore_refs: dict[str, Any]
    paths: dict[str, Path]
    now: str
    apply_id: str
    plan_id: str


@dataclass(frozen=True)
class WorkStateToolState:
    call_refs: list[dict[str, Any]]
    artifact_refs: list[dict[str, Any]]
    all_progress: list[dict[str, Any]]
    read_coverage: dict[str, Any]
    tool_progress: list[dict[str, Any]]


@dataclass(frozen=True)
class WorkStateSnapshotPayloadRequest:
    snapshot: WorkStateSnapshotRequest
    source_state: dict[str, Any]
    field_sources: Any
    tool_state: WorkStateToolState
    goal: str
    next_actions: list[str]
    read_files: list[str]


def build_work_state_snapshot(request: WorkStateSnapshotRequest) -> dict[str, Any]:
    from ..compact_work_state.archive import source_work_state

    source_state = source_work_state(request.restore_refs, request.plan["scope"])
    snapshot = _base_snapshot(request, source_state)
    snapshot["completeness"] = _work_state_completeness(snapshot, request.restore_refs)
    snapshot["missing_fields"] = _missing_fields(snapshot["completeness"])
    snapshot["source_quality"] = _source_quality(snapshot, request.restore_refs)
    return snapshot


def work_state_summary(work_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "goal_present": bool(work_state["goal"]),
        "next_actions_count": len(work_state["next_actions"]),
        "missing_fields": list(work_state["missing_fields"]),
        "source_quality": dict(work_state["source_quality"]),
    }


def restore_refs_summary(restore_refs: dict[str, Any]) -> dict[str, int]:
    refs = restore_refs["source_refs"]
    return {
        "archive_files": len(refs["archive_files"]),
        "snapshot_files": len(refs["snapshot_files"]),
        "token_ledgers": len(refs["token_ledgers"]),
    }


def _base_snapshot(request: WorkStateSnapshotRequest, source_state: dict[str, Any]) -> dict[str, Any]:
    from ..compact_work_state.sources import (
        WorkStateFieldSourceRequest,
        build_work_state_field_sources,
    )

    field_sources = build_work_state_field_sources(WorkStateFieldSourceRequest(request.plan, source_state))
    # Current task/run facts are authoritative. Archive text is only a
    # fallback because it may contain a runner wrapper or an older turn.
    goal = field_sources.goal or source_state["goal"]
    tool_state = _work_state_tool_state(request.restore_refs)
    next_actions = _snapshot_next_actions(source_state, field_sources)
    read_files = _snapshot_read_files(field_sources, tool_state)
    return _work_state_snapshot_payload(
        WorkStateSnapshotPayloadRequest(request, source_state, field_sources, tool_state, goal, next_actions, read_files)
    )


def _work_state_tool_state(restore_refs: dict[str, Any]) -> WorkStateToolState:
    call_refs = tool_call_refs(restore_refs)
    artifact_refs = tool_output_artifact_refs(restore_refs)
    all_progress = _dedupe_tool_progress([
        *_tool_progress_from_call_refs(call_refs),
        *_tool_progress_from_artifact_refs(artifact_refs),
    ], limit=0)
    return WorkStateToolState(
        call_refs=call_refs,
        artifact_refs=artifact_refs,
        all_progress=all_progress,
        read_coverage=_read_coverage_payload(all_progress),
        tool_progress=_limited_tool_progress(all_progress),
    )


def _snapshot_next_actions(
    source_state: dict[str, Any],
    field_sources: Any,
) -> list[str]:
    guidance_next = _runtime_guidance_next_action(field_sources.runtime_handoff)
    progress_next = _task_progress_next_action(field_sources.task_progress)
    current_actions = (
        ([guidance_next] if guidance_next else [])
        or ([progress_next] if progress_next else [])
        or field_sources.next_actions
    )
    if current_actions or field_sources.goal:
        return current_actions
    return source_state["next_actions"]


def _snapshot_read_files(field_sources: Any, tool_state: WorkStateToolState) -> list[str]:
    return dedupe_strings([
        *field_sources.read_files,
        *_tool_read_files_from_progress(tool_state.tool_progress),
    ])


def _work_state_snapshot_payload(request: WorkStateSnapshotPayloadRequest) -> dict[str, Any]:
    snapshot = request.snapshot
    source_state = request.source_state
    field_sources = request.field_sources
    tool_state = request.tool_state
    return {
        "version": COMPACT_WORK_STATE_SNAPSHOT_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_WORK_STATE_SNAPSHOT_SCHEMA),
        "event_type": "compact_work_state_snapshot",
        "apply_id": snapshot.apply_id,
        "plan_id": snapshot.plan_id,
        "workspace_root": snapshot.plan["workspace_root"],
        "scope": snapshot.plan["scope"],
        "created_at": snapshot.now,
        "goal": request.goal,
        "phase": "compact_apply",
        "next_step": request.next_actions[0] if request.next_actions else "",
        "next_actions": request.next_actions,
        "acceptance": field_sources.acceptance,
        "constraints": field_sources.constraints,
        "changed_files": [],
        "read_files": request.read_files,
        "read_coverage": tool_state.read_coverage,
        "tool_progress": tool_state.tool_progress,
        "pending_deferred_tool_calls": _pending_deferred_tool_calls(tool_state.call_refs),
        "artifact_refs": tool_state.artifact_refs,
        "restore_refs": _work_state_restore_refs(snapshot.paths, snapshot.restore_refs),
        "refs": _work_state_refs(snapshot.paths, snapshot.restore_refs),
        "git_state": {"status": "not_captured", "changed_files": []},
        "latest_tests": field_sources.latest_tests,
        "task_progress": field_sources.task_progress,
        "desired_outputs": field_sources.desired_outputs,
        "run_intent": field_sources.run_intent,
        "target_coverage": field_sources.target_coverage,
        "runtime_handoff": field_sources.runtime_handoff,
        "source_load_errors": _source_load_errors(source_state),
    }


def _task_progress_next_action(progress: dict[str, Any]) -> str:
    return str(progress.get("next_action") or "").strip() if isinstance(progress, dict) else ""


def _runtime_guidance_next_action(handoff: dict[str, Any]) -> str:
    if not isinstance(handoff, dict):
        return ""
    rows = handoff.get("recent_guidance")
    if not isinstance(rows, list) or not rows:
        return ""
    first = rows[0] if isinstance(rows[0], dict) else {}
    message = str(first.get("message") or "").strip()
    return f"按最近运行中提示继续：{message}" if message else ""


def _tool_progress_from_artifact_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    progress: list[dict[str, Any]] = []
    for ref in refs:
        if item := _tool_progress_item(ref):
            # 来自已外置 artifact_refs → 这条 tool output 真有可读 blob。打标记供渲染层(Fix B,
            # 阶段4)决定是否把 scoped_call_id 当 read_artifact 入口喂模型;未外置的(只在 call_refs
            # 里、无此标记)渲染层不喂,免得模型对读不到的 ref 瞎试(read_artifact 会报 not_externalized)。
            item["externalized"] = True
            progress.append(item)
    return progress


def _tool_progress_from_call_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    progress: list[dict[str, Any]] = []
    for ref in refs:
        if item := _tool_progress_item(ref):
            progress.append(item)
    return progress


def _dedupe_tool_progress(progress: list[dict[str, Any]], *, limit: int = 48) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    positions: dict[tuple[str, str, str], int] = {}
    for item in progress:
        tool = str(item.get("tool") or "").strip()
        source_path = str(item.get("source_path") or "").strip()
        if not tool or not source_path:
            continue
        key = (tool, source_path, _tool_progress_dedupe_scope(item))
        if key in positions:
            _merge_tool_progress_item(deduped[positions[key]], item)
            continue
        positions[key] = len(deduped)
        deduped.append(dict(item))
    return _limited_tool_progress(deduped, limit=limit)


def _merge_tool_progress_item(existing: dict[str, Any], item: dict[str, Any]) -> None:
    if not str(existing.get("scoped_call_id") or "").strip():
        existing["scoped_call_id"] = str(item.get("scoped_call_id") or "").strip()
    if not int(existing.get("size_bytes", 0) or 0):
        existing["size_bytes"] = int(item.get("size_bytes", 0) or 0)
    if item.get("externalized"):
        # call_ref(无标记,排前先入)与 artifact_ref(已外置,排后)同 key 合并时,
        # 把"已外置"传播到留存项 → 渲染层据此放心喂 artifact_ref。
        existing["externalized"] = True
    _merge_cursor_fields(existing, item)


def _limited_tool_progress(progress: list[dict[str, Any]], *, limit: int = 48) -> list[dict[str, Any]]:
    return progress[-limit:] if limit > 0 else progress


def _tool_progress_dedupe_scope(item: dict[str, Any]) -> str:
    tool = str(item.get("tool") or "").strip()
    if tool in {"read_file", "read_artifact"}:
        if item.get("offset") is not None:
            return f"offset:{item.get('offset')}:{item.get('next_offset') or item.get('max_chars') or ''}"
        if item.get("start_line") is not None or item.get("end_line") is not None:
            return f"lines:{item.get('start_line') or ''}:{item.get('end_line') or ''}"
        scoped_call_id = str(item.get("scoped_call_id") or "").strip()
        if scoped_call_id:
            return f"call:{scoped_call_id}"
    if tool in {"list_files", "find_files", "search_text"}:
        return (
            f"page:{item.get('offset') if item.get('offset') is not None else ''}:"
            f"{item.get('next_offset') if item.get('next_offset') is not None else ''}:"
            f"{item.get('query') or item.get('pattern') or item.get('file_glob') or ''}:"
            f"{item.get('output_mode') or ''}"
        )
    return "source"


def _merge_cursor_fields(existing: dict[str, Any], item: dict[str, Any]) -> None:
    for key in (
        "offset",
        "max_chars",
        "chars",
        "next_offset",
        "total_chars",
        "start_line",
        "end_line",
        "next_start_line",
        "total_lines",
        "limit",
        "returned",
    ):
        if existing.get(key) is None and item.get(key) is not None:
            existing[key] = item[key]
    if "complete" not in existing and "complete" in item:
        existing["complete"] = item["complete"]


def _tool_progress_item(ref: dict[str, Any]) -> dict[str, Any]:
    tool = str(ref.get("tool") or "").strip()
    if tool not in {"read_file", "list_files", "find_files", "search_text", "run_command", "read_artifact", "write_file"}:
        return {}
    parameters = ref.get("parameters", {}) if isinstance(ref.get("parameters"), dict) else {}
    source_path = str(ref.get("source_path") or parameters.get("path") or parameters.get("command") or "").strip()
    if not source_path:
        return {}
    item = {
        "tool": tool,
        "source_path": source_path,
        "scoped_call_id": str(ref.get("scoped_call_id") or ref.get("call_id") or "").strip(),
        "size_bytes": int(ref.get("size_bytes", 0) or 0),
    }
    if tool in {"read_file", "read_artifact"}:
        if not _successful_read_ref(ref):
            return {}
        item.update(_read_cursor_fields(ref, parameters))
    if tool in {"list_files", "find_files", "search_text"}:
        if not _successful_read_ref(ref):
            return {}
        item.update(_page_cursor_fields(ref, parameters))
    return item


def _successful_read_ref(ref: dict[str, Any]) -> bool:
    if ref.get("ok") is not True:
        return False
    if str(ref.get("error_code") or "").strip():
        return False
    status = str(ref.get("status") or "").strip()
    return status in {"", "ok"}


def _read_cursor_fields(ref: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "offset": _optional_int(parameters.get("offset")),
        "max_chars": _optional_int(parameters.get("max_chars")),
        "start_line": _optional_int(parameters.get("start_line")),
        "end_line": _optional_int(parameters.get("end_line")),
    }
    window = _read_window_from_ref(ref)
    if window.get("kind") == "char_window":
        offset = int(window["offset"])
        next_offset = int(window["next_offset"])
        total_chars = int(window["total_chars"])
        fields["offset"] = offset
        fields["chars"] = int(window.get("chars") or max(0, next_offset - offset))
        fields["next_offset"] = next_offset
        fields["total_chars"] = total_chars
        fields["max_chars"] = fields["max_chars"] or next_offset - offset
    elif window.get("kind") == "line_window":
        fields["start_line"] = int(window["start_line"])
        fields["end_line"] = int(window["end_line"])
        fields["next_start_line"] = int(window.get("next_start_line") or 0)
        fields["total_lines"] = int(window["total_lines"])
    if not fields.get("next_offset") and fields["max_chars"]:
        if fields["offset"] is None and fields["start_line"] is None and fields["end_line"] is None:
            fields["offset"] = 0
        if fields["offset"] is not None:
            fields["next_offset"] = int(fields["offset"]) + int(fields["max_chars"])
    return {key: value for key, value in fields.items() if value is not None}


def _read_window_from_ref(ref: dict[str, Any]) -> dict[str, Any]:
    window = ref.get("read_window")
    if not isinstance(window, dict):
        envelope = ref.get("tool_result_envelope")
        window = envelope.get("read_window") if isinstance(envelope, dict) else {}
    if not isinstance(window, dict):
        return {}
    kind = str(window.get("kind") or "").strip()
    if kind == "char_window":
        offset = _optional_int(window.get("offset"))
        next_offset = _optional_int(window.get("next_offset"))
        total_chars = _optional_int(window.get("total_chars"))
        if offset is None or next_offset is None or total_chars is None:
            return {}
        return {
            "kind": kind,
            "offset": offset,
            "chars": _optional_int(window.get("chars")) or max(0, next_offset - offset),
            "next_offset": next_offset,
            "total_chars": total_chars,
        }
    if kind == "line_window":
        start_line = _optional_int(window.get("start_line"))
        end_line = _optional_int(window.get("end_line"))
        total_lines = _optional_int(window.get("total_lines"))
        if start_line is None or end_line is None or total_lines is None:
            return {}
        return {
            "kind": kind,
            "start_line": start_line,
            "end_line": end_line,
            "next_start_line": _optional_int(window.get("next_start_line")) or 0,
            "total_lines": total_lines,
        }
    return {}


def _page_cursor_fields(ref: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "offset": _optional_int(parameters.get("offset")),
        "limit": _optional_int(parameters.get("limit")),
    }
    window = _page_window_from_ref(ref)
    if window:
        fields.update({
            "offset": int(window["offset"]),
            "limit": int(window["limit"]),
            "returned": int(window["returned"]),
            "next_offset": int(window["next_offset"]),
            "complete": bool(window["complete"]),
        })
        if window.get("output_mode"):
            fields["output_mode"] = str(window.get("output_mode") or "")
    for key in ("query", "pattern", "file_glob", "output_mode"):
        value = str(parameters.get(key) or "").strip()
        if value:
            fields[key] = value
    for key in ("recursive", "max_depth", "include_dirs", "include_files", "include_ignored", "literal", "ignore_case", "context"):
        if key in parameters:
            fields[key] = parameters[key]
    return {key: value for key, value in fields.items() if value is not None}


def _page_window_from_ref(ref: dict[str, Any]) -> dict[str, Any]:
    window = ref.get("page_window")
    if not isinstance(window, dict):
        envelope = ref.get("tool_result_envelope")
        window = envelope.get("page_window") if isinstance(envelope, dict) else {}
    if not isinstance(window, dict) or str(window.get("kind") or "").strip() != "offset_page":
        return {}
    offset = _optional_int(window.get("offset"))
    limit = _optional_int(window.get("limit"))
    returned = _optional_int(window.get("returned"))
    next_offset = _optional_int(window.get("next_offset"))
    if offset is None or limit is None or returned is None:
        return {}
    if next_offset is None:
        next_offset = 0 if bool(window.get("complete")) else offset + returned
    payload: dict[str, Any] = {
        "kind": "offset_page",
        "offset": offset,
        "limit": limit,
        "returned": returned,
        "next_offset": next_offset,
        "complete": bool(window.get("complete")) or next_offset <= 0,
    }
    output_mode = str(window.get("output_mode") or "").strip()
    if output_mode:
        payload["output_mode"] = output_mode
    return payload


def _tool_read_files_from_progress(progress: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("source_path") or "")
        for item in progress
        if str(item.get("tool") or "") in {"read_file", "read_artifact"}
    ]


def _read_coverage_payload(progress: list[dict[str, Any]]) -> dict[str, Any]:
    read_items = [item for item in progress if item.get("tool") in {"read_file", "read_artifact"}]
    cursor = _best_read_cursor(read_items)
    if not cursor:
        return {}
    sources = _source_read_coverage(read_items)
    incomplete_sources = _incomplete_read_sources(sources)
    primary = _read_cursor_payload(cursor)
    return {
        "schema_version": 1,
        "primary": primary,
        "sources": sources[:24],
        "source_count": len(sources),
        "omitted_source_count": max(0, len(sources) - 24),
        "incomplete_sources": incomplete_sources[:24],
        "incomplete_source_count": len(incomplete_sources),
        "omitted_incomplete_source_count": max(0, len(incomplete_sources) - 24),
    }


def _read_cursor_payload(cursor: dict[str, Any]) -> dict[str, Any]:
    ranges = list(cursor.get("ranges") or [])
    covered_until = int(cursor.get("covered_until") or 0)
    total = int(cursor.get("total") or 0)
    payload = {
        "kind": str(cursor.get("kind") or "char_window"),
        "source_path": str(cursor.get("source_path") or ""),
        "covered_until": covered_until,
        "total": total,
        "complete": bool(total and covered_until >= total),
        "ranges": [
            {"start": int(start), "end": int(end)}
            for start, end, _total in ranges[:12]
        ],
        "range_count": len(ranges),
        "omitted_range_count": max(0, len(ranges) - 12),
    }
    if payload["kind"] == "line_window":
        payload["covered_until_line"] = covered_until
        payload["total_lines"] = total
        payload["next_start_line"] = covered_until + 1 if not payload["complete"] else 0
    else:
        payload["covered_until_offset"] = covered_until
        payload["total_chars"] = total
        payload["next_offset"] = covered_until if not payload["complete"] else 0
    return {key: value for key, value in payload.items() if value not in ("", [], {}, None)}


def _source_read_coverage(read_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for item in read_items:
        source = str(item.get("source_path") or "").strip()
        if not source:
            continue
        if source not in grouped:
            order.append(source)
            grouped[source] = []
        grouped[source].append(item)
    rows: list[dict[str, Any]] = []
    for source in order:
        cursor = _best_read_cursor(grouped[source])
        if cursor:
            rows.append(_read_cursor_payload(cursor))
    return rows


def _incomplete_read_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [source for source in sources if source and source.get("complete") is not True]


def _best_read_cursor(read_items: list[dict[str, Any]]) -> dict[str, Any]:
    char_cursor = _best_char_cursor(read_items)
    line_cursor = _best_line_cursor(read_items)
    if not char_cursor:
        return line_cursor
    if not line_cursor:
        return char_cursor
    char_total = int(char_cursor.get("total") or 0)
    line_total = int(line_cursor.get("total") or 0)
    char_ratio = (int(char_cursor.get("covered_until") or 0) / char_total) if char_total else 0
    line_ratio = (int(line_cursor.get("covered_until") or 0) / line_total) if line_total else 0
    return line_cursor if line_ratio >= char_ratio else char_cursor


def _best_char_cursor(read_items: list[dict[str, Any]]) -> dict[str, Any]:
    ranges_by_source: dict[str, list[tuple[int, int, int]]] = {}
    for item in read_items:
        source = str(item.get("source_path") or "").strip()
        offset = item.get("offset")
        next_offset = item.get("next_offset")
        if not source or offset is None or next_offset is None:
            continue
        ranges_by_source.setdefault(source, []).append(
            (int(offset), int(next_offset), int(item.get("total_chars") or 0))
        )
    best: dict[str, Any] = {}
    for source, ranges in ranges_by_source.items():
        ranges = sorted(set(ranges), key=lambda item: (item[0], item[1]))
        covered = _covered_prefix_end([(start, end) for start, end, _total in ranges])
        total = max((total for _start, _end, total in ranges), default=0)
        if not best or int(best.get("covered_until") or 0) < covered:
            best = {"kind": "char_window", "source_path": source, "covered_until": covered, "total": total, "ranges": ranges}
    return best


def _best_line_cursor(read_items: list[dict[str, Any]]) -> dict[str, Any]:
    ranges_by_source: dict[str, list[tuple[int, int, int]]] = {}
    for item in read_items:
        source = str(item.get("source_path") or "").strip()
        start_line = item.get("start_line")
        end_line = item.get("end_line")
        if not source or start_line is None or end_line is None:
            continue
        ranges_by_source.setdefault(source, []).append(
            (int(start_line), int(end_line), int(item.get("total_lines") or 0))
        )
    best: dict[str, Any] = {}
    for source, ranges in ranges_by_source.items():
        ranges = sorted(set(ranges), key=lambda item: (item[0], item[1]))
        covered = _covered_line_prefix_end([(start, end) for start, end, _total in ranges])
        total = max((total for _start, _end, total in ranges), default=0)
        if covered and (not best or int(best.get("covered_until") or 0) < covered):
            best = {"kind": "line_window", "source_path": source, "covered_until": covered, "total": total, "ranges": ranges}
    return best


def _covered_prefix_end(ranges: list[tuple[int, int]]) -> int:
    cursor = 0
    for start, end in sorted(ranges):
        if start > cursor:
            break
        if end > cursor:
            cursor = end
    return cursor


def _covered_line_prefix_end(ranges: list[tuple[int, int]]) -> int:
    cursor = 0
    for start, end in sorted(ranges):
        if start > cursor + 1:
            break
        if end > cursor:
            cursor = end
    return cursor


def _pending_deferred_tool_calls(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pending: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        key = _tool_call_key(record)
        if not key:
            continue
        if str(record.get("error_code") or "").strip() == "CONTEXT_COMPACT_DEFERRED":
            pending[key] = dict(record)
            continue
        if record.get("ok") is True and key in pending:
            pending.pop(key, None)
    return list(pending.values())


def _tool_call_key(record: dict[str, Any]) -> str:
    params = record.get("parameters")
    if not isinstance(params, dict):
        return ""
    tool = str(params.get("tool") or record.get("tool") or "").strip()
    if not tool:
        return ""
    try:
        return json.dumps({**params, "tool": tool}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return ""


def _optional_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _work_state_restore_refs(paths: dict[str, Path], restore_refs: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": str(paths["restore_refs_json"]),
        "source_counts": restore_refs_summary(restore_refs),
        "all_source_paths_exist": _all_source_paths_exist(restore_refs),
    }


def _work_state_refs(paths: dict[str, Path], restore_refs: dict[str, Any]) -> dict[str, Any]:
    return {
        "compact_context": str(paths["context_md"]),
        "apply_bundle": str(paths["apply_bundle_json"]),
        "restore_refs": str(paths["restore_refs_json"]),
        "source_counts": restore_refs_summary(restore_refs),
    }


def _work_state_completeness(snapshot: dict[str, Any], restore_refs: dict[str, Any]) -> dict[str, bool]:
    return {
        "goal_present": bool(snapshot["goal"]),
        "next_actions_present": bool(snapshot["next_actions"]),
        "source_refs_present": any(restore_refs_summary(restore_refs).values()),
        "restore_refs_exist": bool(snapshot["restore_refs"]["all_source_paths_exist"]),
        "acceptance_present": bool(snapshot["acceptance"]["items"]),
        "constraints_present": bool(snapshot["constraints"]["items"]),
        "test_state_present": bool(snapshot["latest_tests"]["items"]),
    }


def _missing_fields(completeness: dict[str, bool]) -> list[str]:
    names = {
        "goal_present": "goal",
        "next_actions_present": "next_step",
        "source_refs_present": "restore_refs",
        "acceptance_present": "acceptance",
        "constraints_present": "constraints",
        "test_state_present": "latest_tests",
    }
    return [field for key, field in names.items() if not completeness[key]]


def _source_quality(snapshot: dict[str, Any], restore_refs: dict[str, Any]) -> dict[str, Any]:
    missing = _missing_fields(snapshot["completeness"])
    return {
        "status": "complete" if not missing else "partial",
        "missing_fields": missing,
        "source_counts": restore_refs_summary(restore_refs),
    }


def _source_load_errors(source_state: dict[str, Any]) -> list[dict[str, object]]:
    value = source_state.get("source_load_errors")
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _all_source_paths_exist(restore_refs: dict[str, Any]) -> bool:
    paths = [
        str(item.get("path") or "")
        for group in restore_refs["source_refs"].values()
        for item in group
        if isinstance(item, dict)
    ]
    return all(Path(path).exists() for path in paths if path)


__all__ = [
    "COMPACT_WORK_STATE_SNAPSHOT_SCHEMA",
    "WorkStateSnapshotRequest",
    "build_work_state_snapshot",
    "restore_refs_summary",
    "work_state_summary",
]
