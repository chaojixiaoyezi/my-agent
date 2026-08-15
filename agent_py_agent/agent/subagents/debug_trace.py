
from __future__ import annotations

"""Opt-in subagent debug tracing.

这个模块不是普通日志，也不是用户要看的报告。它只在
`subagent_debug_trace_level > 0` 时写内部 JSONL，用来做真实 E2E
排障。默认 0 完全关闭，所以正常使用不会多写调试文件。
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_py_agent.agent.io import append_jsonl

_PREVIEW_LIMIT = 240


@dataclass(frozen=True)
class SubAgentDebugTraceRequest:
    manager: Any
    level: int
    event_type: str
    task: Any = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SubAgentDebugDetailRequest:
    manager: Any
    task: Any
    event_type: str
    label: str
    value: Any


@dataclass(frozen=True)
class SubAgentRunnerTraceRequest:
    manager: Any
    task: Any
    result: Any
    params: Any


@dataclass(frozen=True)
class SubAgentHierarchyTraceRequest:
    manager: Any
    parent_task: Any
    result: Any


def write_subagent_debug_trace(request: SubAgentDebugTraceRequest) -> Path | None:
    configured_level = _configured_trace_level(request.manager)
    event_level = _bounded_level(request.level)
    if configured_level <= 0 or event_level > configured_level:
        return None
    record = _build_trace_record(request, event_level)
    trace_file = Path(request.manager.workspace) / "debug_traces" / "subagent_trace.jsonl"
    append_jsonl(trace_file, record, sort_keys=True)
    return trace_file


def configured_subagent_debug_trace_level(manager: Any) -> int:
    return _configured_trace_level(manager)


def preview_debug_trace_text(value: Any) -> str:
    return _preview(_detail_text(value))


def write_subagent_debug_detail(request: SubAgentDebugDetailRequest) -> str:
    if configured_subagent_debug_trace_level(request.manager) < 5:
        return ""
    run_id = str(getattr(request.task, "id", "") or "unknown")
    detail_dir = Path(request.manager.workspace) / "debug_traces" / "details" / _safe_name(run_id)
    detail_dir.mkdir(parents=True, exist_ok=True)
    detail_file = (
        detail_dir
        / f"{int(time.time() * 1000)}-{_safe_name(request.event_type)}-{_safe_name(request.label)}.txt"
    )
    detail_file.write_text(_detail_text(request.value), encoding="utf-8")
    return str(detail_file)


def trace_task_created(manager: Any, task: Any) -> Path | None:
    return write_subagent_debug_trace(
        SubAgentDebugTraceRequest(
            manager=manager,
            level=1,
            event_type="task_created",
            task=task,
            payload={"goal_preview": _preview(getattr(task, "goal", ""))},
        )
    )


def trace_runner_result(request: SubAgentRunnerTraceRequest) -> Path | None:
    task = request.task
    result = request.result
    params = request.params
    return write_subagent_debug_trace(
        SubAgentDebugTraceRequest(
            manager=request.manager,
            level=2,
            event_type="runner_result_recorded",
            task=task,
            payload={
                "ok": bool(getattr(result, "ok", False)),
                "dry_run": bool(getattr(params, "dry_run", False)),
                "backend": str(getattr(params, "backend", "") or ""),
                "tool_rounds": int(getattr(params, "tool_rounds", 0) or 0),
                "runner_result_ref": str(getattr(task, "runner_result_file", "") or ""),
            },
        )
    )


def trace_hierarchy_schedule_result(request: SubAgentHierarchyTraceRequest) -> Path | None:
    result = request.result
    return write_subagent_debug_trace(
        SubAgentDebugTraceRequest(
            manager=request.manager,
            level=2,
            event_type="hierarchy_schedule_result",
            task=request.parent_task,
            payload={
                "dry_run": bool(getattr(result, "dry_run", True)),
                "blocked": bool(getattr(result, "blocked", False)),
                "reason": str(getattr(result, "reason", "") or ""),
                "requested_by": str(getattr(result, "requested_by", "") or ""),
                "planned_count": int(getattr(result, "planned_count", 0) or 0),
                "created_count": len(getattr(result, "created_run_ids", []) or []),
                "created_run_ids": list(getattr(result, "created_run_ids", []) or [])[:32],
            },
        )
    )


def trace_hierarchy_schedule(manager: Any, parent_task: Any, result: Any) -> Any:
    trace_hierarchy_schedule_result(SubAgentHierarchyTraceRequest(manager, parent_task, result))
    return result


def _build_trace_record(request: SubAgentDebugTraceRequest, event_level: int) -> dict[str, Any]:
    task = request.task
    record: dict[str, Any] = {
        "created_at": time.time(),
        "event_type": str(request.event_type),
        "level": event_level,
    }
    if task is not None:
        record.update(_task_trace_fields(task))
    record.update(_safe_payload(request.payload))
    return record


def _task_trace_fields(task: Any) -> dict[str, Any]:
    return {
        "run_id": str(getattr(task, "id", "") or ""),
        "root_id": str(getattr(task, "root_id", "") or ""),
        "parent_id": str(getattr(task, "parent_id", "") or ""),
        "depth": int(getattr(task, "depth", 0) or 0),
        "role": str(getattr(task, "role", "") or ""),
        "status": str(getattr(task, "status", "") or ""),
        "verification_status": str(getattr(task, "verification_status", "") or ""),
    }


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        safe[str(key)] = _safe_payload_value(value)
    return safe


def _safe_payload_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, int | float):
        return value
    if isinstance(value, str):
        return _preview(value)
    if isinstance(value, list):
        return [_safe_payload_value(item) for item in value[:32]]
    if isinstance(value, dict):
        return {str(key): _safe_payload_value(item) for key, item in list(value.items())[:32]}
    return _preview(str(value))


def _configured_trace_level(manager: Any) -> int:
    return _bounded_level(getattr(manager, "debug_trace_level", 0))


def _bounded_level(value: Any) -> int:
    try:
        level = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(5, level))


def _preview(value: str) -> str:
    compact = " ".join(str(value).split())
    if len(compact) <= _PREVIEW_LIMIT:
        return compact
    return compact[: _PREVIEW_LIMIT - 3] + "..."


def _detail_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        return str(value)


def _safe_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in str(value))
    return cleaned.strip("-")[:80] or "item"
