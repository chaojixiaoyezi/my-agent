
from __future__ import annotations

"""Task-local progress snapshots for subagent runner tool calls."""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ....common.json_io import read_json_object_report
from ....common.value_parsing import sequence_strings
from ....runtime_errors import runtime_error_report
from ...model_task import SubAgentTask
from .integrity import (
    artifact_integrity_progress,
    artifact_integrity_summary,
    artifact_next_action,
)

_SCHEMA_VERSION = "subagent_tool_progress.v1"
_MAX_HEADINGS = 16
_WRITE_TOOLS = {"write_file", "apply_patch"}
_EXPLICIT_PROGRESS_PATH_KEYS = (
    "progress_path",
    "product_path",
    "deliverable_path",
    "user_artifact_path",
)
_PATH_KEYS = (
    "artifact_ref",
    "artifact_path",
    "file_path",
    "target_path",
    "path",
    "output_path",
    "ref",
    "uri",
)


@dataclass(frozen=True)
class SubagentToolProgressRequest:
    task: SubAgentTask
    tool: str
    payload: dict[str, object]
    output: str
    ok: bool
    tool_round: int
    tool_index: int
    result_envelope: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _ProgressRefs:
    latest: Path
    ledger: Path


@dataclass(frozen=True)
class _CloseoutSnapshotRequest:
    request: SubagentToolProgressRequest
    previous: dict[str, Any]
    refs: _ProgressRefs
    path: str
    headings: list[str]
    written_paths: list[str]


def record_runtime_subagent_tool_progress(agent: object, record: object) -> dict[str, Any]:
    params = getattr(record, "params", None)
    if str(getattr(params, "context_scope", "") or "").strip().lower() != "task_local":
        return {}
    run_id = str(getattr(params, "run_id", "") or "").strip()
    if not run_id or not hasattr(agent, "subagents"):
        return {}
    try:
        task = agent.subagents.load(run_id)
    except (FileNotFoundError, json.JSONDecodeError, TypeError, AttributeError) as exc:
        return _progress_load_error(exc, run_id)
    result = getattr(record, "result", None)
    payload = getattr(record, "payload", {})
    progress = record_subagent_tool_progress(
        SubagentToolProgressRequest(
            task=task,
            tool=str(getattr(result, "tool", "") or ""),
            payload=payload if isinstance(payload, dict) else {},
            output=str(getattr(result, "output", "") or ""),
            result_envelope=(
                dict(getattr(result, "result_envelope", {}) or {})
                if isinstance(getattr(result, "result_envelope", {}), dict)
                else {}
            ),
            ok=bool(getattr(result, "ok", False)),
            tool_round=int(getattr(record, "tool_rounds", 0) or 0),
            tool_index=int(getattr(record, "idx", 0) or 0),
        )
    )
    _persist_runtime_status(agent, task, result, progress)
    return progress


def record_subagent_tool_progress(request: SubagentToolProgressRequest) -> dict[str, Any]:
    if not _should_record(request):
        return {}
    progress_dir = _progress_dir(request.task)
    if not progress_dir:
        return {}
    progress_dir.mkdir(parents=True, exist_ok=True)
    refs = _ProgressRefs(progress_dir / "latest_tool_progress.json", progress_dir / "tool_progress.jsonl")
    previous_report = read_json_object_report(
        refs.latest,
        parse_nested_string=True,
        context="subagent_tool_progress.previous_progress",
    )
    previous = previous_report.payload
    snapshot = _snapshot_payload(request, previous, refs)
    if previous_report.load_error:
        _append_load_error(snapshot, previous_report.load_error)
    refs.latest.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    with refs.ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=False, sort_keys=True) + "\n")
    request.task.latest_summary = str(snapshot.get("summary") or request.task.latest_summary or "")
    request.task.current_step = str(snapshot.get("next_action") or request.task.current_step or "")
    return snapshot


def _should_record(request: SubagentToolProgressRequest) -> bool:
    return bool(request.ok and isinstance(request.payload, dict) and progress_path(request))


def _persist_runtime_status(agent: object, task: SubAgentTask, result: object, progress: dict[str, Any]) -> None:
    now = time.time()
    tool = _result_tool_name(result)
    ok = bool(getattr(result, "ok", False))
    task.current_tool = tool
    task.heartbeat_at = now
    task.updated_at = now
    _append_recent_tool_trace(task, {"tool": tool, "ok": ok, "at": now, "progress": progress})
    if ok and tool:
        summary = _progress_summary(tool, progress)
        task.last_progress_at = now
        task.last_progress_summary = summary
        task.progress = max(_safe_progress(getattr(task, "progress", 0.0)), 0.25 if progress else 0.05)
        if progress:
            task.latest_summary = str(progress.get("summary") or task.latest_summary or "")
            task.current_step = str(progress.get("next_action") or task.current_step or "")
        else:
            task.latest_summary = task.latest_summary or summary
            task.current_step = task.current_step or "RUNNING"
    try:
        agent.subagents.save(task)
    except Exception as exc:
        progress["status_save_error"] = runtime_error_report(
            exc,
            context="subagent_tool_progress.subagents.save",
        )


# LLM: ToolResult 的权威字段是 tool_name；tool 只兼容少量测试桩和旧调用方，
# 不能因为读错字段让真实 runner 的 current_tool/recent_tool_trace 永远为空。
# 函数用途: 从真实工具结果或旧测试对象中取得统一工具名，供子代理活动状态展示。
def _result_tool_name(result: object) -> str:
    return str(
        getattr(result, "tool_name", "")
        or getattr(result, "tool", "")
        or ""
    ).strip()


def _progress_load_error(exc: Exception, run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "load_error": runtime_error_report(exc, context="subagent_tool_progress.subagents.load"),
    }


def _append_recent_tool_trace(task: SubAgentTask, event: dict[str, Any]) -> None:
    tool = str(event.get("tool") or "")
    if not tool:
        return
    attrs = dict(getattr(task, "attributes", {}) or {})
    trace = attrs.get("recent_tool_trace")
    items = list(trace) if isinstance(trace, list) else []
    progress = event.get("progress") if isinstance(event.get("progress"), dict) else {}
    entry = {
        "tool": tool,
        "ok": bool(event.get("ok", False)),
        "at": event.get("at", 0.0),
        "summary": _progress_summary(tool, progress),
    }
    path = str(progress.get("latest_written_path") or "").strip() if isinstance(progress, dict) else ""
    if path:
        entry["path"] = path
    items.append(entry)
    attrs["recent_tool_trace"] = [item for item in items if isinstance(item, dict)][-5:]
    task.attributes = attrs


def _safe_progress(value: object) -> float:
    try:
        progress = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, progress))


def _progress_summary(tool: str, progress: dict[str, Any]) -> str:
    summary = str(progress.get("summary") or "").strip() if isinstance(progress, dict) else ""
    if summary:
        return summary
    return f"最近成功调用工具: {tool}"


def _progress_dir(task: SubAgentTask) -> Path | None:
    workspace = str(getattr(task, "agent_run_workspace_dir", "") or "").strip()
    return Path(workspace) / "progress" if workspace else None


def progress_path(request: object) -> str:
    tool = str(getattr(request, "tool", "") or "")
    for value in _explicit_progress_path_values(getattr(request, "result_envelope", {})):
        path = _path_text(value)
        if path:
            return path
    output_payload = _json_object_from_text(str(getattr(request, "output", "") or ""))
    for value in _explicit_progress_path_values(output_payload):
        path = _path_text(value)
        if path:
            return path
    if tool not in _WRITE_TOOLS:
        return ""
    for value in _candidate_path_values(getattr(request, "result_envelope", {})):
        path = _path_text(value)
        if path:
            return path
    for value in _candidate_path_values(output_payload):
        path = _path_text(value)
        if path:
            return path
    return _payload_progress_path(request)


def _payload_progress_path(request: object) -> str:
    tool = str(getattr(request, "tool", "") or "")
    payload = getattr(request, "payload", {})
    payload = payload if isinstance(payload, dict) else {}
    if tool in _WRITE_TOOLS:
        return _path_text(payload.get("path"))
    return ""


def _candidate_path_values(payload: object) -> list[object]:
    if not isinstance(payload, dict):
        return []
    values = [payload.get(key) for key in _PATH_KEYS if key in payload]
    for key in ("output", "result", "artifact"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            values.extend(_candidate_path_values(nested))
    return values


def _explicit_progress_path_values(payload: object) -> list[object]:
    if not isinstance(payload, dict):
        return []
    values = [payload.get(key) for key in _EXPLICIT_PROGRESS_PATH_KEYS if key in payload]
    for key in ("output", "result", "artifact"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            values.extend(_explicit_progress_path_values(nested))
    return values


def _path_text(value: object) -> str:
    if isinstance(value, dict):
        return _path_text_from_dict(value)
    text = str(value or "").strip()
    if not text or "://" in text:
        return ""
    return text


def _path_text_from_dict(value: dict[str, object]) -> str:
    for key in ("resolved", "display", "raw", "path", "artifact_ref"):
        text = _path_text(value.get(key))
        if text:
            return text
    return ""


def _json_object_from_text(text: str) -> dict[str, Any]:
    try:
        value = json.loads(str(text or ""))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _snapshot_payload(
    request: SubagentToolProgressRequest,
    previous: dict[str, Any],
    refs: _ProgressRefs,
) -> dict[str, Any]:
    path = progress_path(request)
    headings = _merge_unique(sequence_strings(previous.get("headings")) + _headings_from_payload(request.payload))
    written_paths = _merge_unique(sequence_strings(previous.get("written_paths")) + ([path] if path else []))
    if _is_internal_output_path(request.task, path) and previous:
        return _output_closeout_snapshot(
            _CloseoutSnapshotRequest(request, previous, refs, path, headings, written_paths)
        )
    integrity = artifact_integrity_progress(path)
    summary = _summary(request.tool, path, headings, integrity)
    next_action = artifact_next_action(integrity)
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": "subagent_tool_progress",
        "run_id": request.task.id,
        "root_id": request.task.root_id or request.task.id,
        "tool": request.tool,
        "tool_round": request.tool_round,
        "tool_index": request.tool_index,
        "latest_written_path": path,
        "written_paths": written_paths,
        "headings": headings[:_MAX_HEADINGS],
        "summary": summary,
        "next_action": next_action,
        "artifact_integrity": integrity,
        "latest_tool_progress_ref": str(refs.latest),
        "tool_progress_ledger_ref": str(refs.ledger),
        "output_preview": _clip(request.output, 300),
        "load_errors": [],
    }


def _output_closeout_snapshot(closeout: _CloseoutSnapshotRequest) -> dict[str, Any]:
    request = closeout.request
    snapshot = dict(closeout.previous)
    snapshot.update({
        "tool": request.tool,
        "tool_round": request.tool_round,
        "tool_index": request.tool_index,
        "written_paths": closeout.written_paths,
        "headings": closeout.headings[:_MAX_HEADINGS],
        "latest_tool_progress_ref": str(closeout.refs.latest),
        "tool_progress_ledger_ref": str(closeout.refs.ledger),
        "closeout_written_path": closeout.path,
        "closeout_output_preview": _clip(request.output, 300),
        "load_errors": _load_error_list(closeout.previous.get("load_errors")),
    })
    return snapshot


def _append_load_error(snapshot: dict[str, Any], error: dict[str, object]) -> None:
    items = _load_error_list(snapshot.get("load_errors"))
    items.append(error)
    snapshot["load_errors"] = items


def _load_error_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _is_internal_output_path(task: SubAgentTask, path: str) -> bool:
    if not path:
        return False
    try:
        return Path(path).resolve() == Path(str(getattr(task, "output_json", "") or "")).resolve()
    except OSError:
        return False


def _headings_from_payload(payload: dict[str, object]) -> list[str]:
    content = str(payload.get("content") or "")
    headings: list[str] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line.startswith("#"):
            continue
        heading = line.lstrip("#").strip()
        if heading:
            headings.append(heading)
    return headings[:_MAX_HEADINGS]


def _summary(tool: str, path: str, headings: list[str], integrity: dict[str, Any]) -> str:
    name = Path(path).name if path else "未命名文件"
    integrity_summary = artifact_integrity_summary(integrity)
    if headings:
        base = f"最近 {tool} {name}；已记录标题：{'；'.join(headings[:6])}"
    else:
        base = f"最近 {tool} {name}；尚未识别到 Markdown 标题"
    return f"{base}；{integrity_summary}" if integrity_summary else base


def _merge_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if str(item or "").strip()))


def _clip(text: str, limit: int) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit].rstrip() + "...<truncated>"


__all__ = [
    "SubagentToolProgressRequest",
    "record_runtime_subagent_tool_progress",
    "record_subagent_tool_progress",
]
