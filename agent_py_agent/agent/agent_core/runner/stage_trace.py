
from __future__ import annotations

"""Refs-only runner stage tracing.

这里不是普通业务日志。它只在当前 SimpleAgent 正处于 subagent runner 上下文，
且 `subagent_debug_trace_level` 打开时写短事件。事件只包含阶段、工具名、
响应长度、字段名等信息，不写 prompt、response 或工具输出正文。
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from ...runtime_errors import runtime_error_report
from ...subagents.models import TaskStatus, task_has_status
from .context import current_subagent_run_id

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunnerModelStageTraceRequest:
    agent: Any
    params: Any
    tool_rounds: int
    prompt: str = ""
    response: Any = None
    exc: BaseException | None = None


@dataclass(frozen=True)
class RunnerToolStageTraceRequest:
    agent: Any
    params: Any
    tool_rounds: int
    idx: int
    payload: Any
    result: Any = None


@dataclass(frozen=True)
class RunnerStageTraceBundle:
    agent: Any
    event_type: str
    params: Any
    tool_rounds: int
    payload: dict[str, Any] = field(default_factory=dict)
    detail_payload: dict[str, Any] = field(default_factory=dict)


def trace_runner_model_request_started(request: RunnerModelStageTraceRequest) -> None:
    _trace_runner_stage(
        RunnerStageTraceBundle(
            agent=request.agent,
            event_type="runner_model_request_started",
            params=request.params,
            tool_rounds=request.tool_rounds,
            payload={"prompt_chars": len(request.prompt or "")},
            detail_payload={"prompt": request.prompt or ""},
        )
    )


def trace_runner_model_response_received(request: RunnerModelStageTraceRequest) -> None:
    _trace_runner_stage(
        RunnerStageTraceBundle(
            agent=request.agent,
            event_type="runner_model_response_received",
            params=request.params,
            tool_rounds=request.tool_rounds,
            payload={
                "backend": str(getattr(request.response, "backend", "") or ""),
                "response_chars": len(str(getattr(request.response, "text", "") or "")),
            },
            detail_payload={"response": str(getattr(request.response, "text", "") or "")},
        )
    )


def trace_runner_model_request_failed(request: RunnerModelStageTraceRequest) -> None:
    exc = request.exc or RuntimeError("unknown backend error")
    _trace_runner_stage(
        RunnerStageTraceBundle(
            agent=request.agent,
            event_type="runner_model_request_failed",
            params=request.params,
            tool_rounds=request.tool_rounds,
            payload={
                "error_type": exc.__class__.__name__,
                "error_preview": str(exc),
            },
        )
    )


def trace_runner_tool_call_started(request: RunnerToolStageTraceRequest) -> None:
    _trace_runner_stage(
        RunnerStageTraceBundle(
            agent=request.agent,
            event_type="runner_tool_call_started",
            params=request.params,
            tool_rounds=request.tool_rounds,
            payload={
                "idx": request.idx,
                "tool": _tool_name(request.payload),
                "payload_keys": _payload_keys(request.payload),
            },
            detail_payload={"tool_payload": request.payload},
        )
    )


def trace_runner_tool_call_finished(request: RunnerToolStageTraceRequest) -> None:
    _trace_runner_stage(
        RunnerStageTraceBundle(
            agent=request.agent,
            event_type="runner_tool_call_finished",
            params=request.params,
            tool_rounds=request.tool_rounds,
            payload={
                "idx": request.idx,
                "tool": str(getattr(request.result, "tool", "") or _tool_name(request.payload)),
                "ok": bool(getattr(request.result, "ok", False)),
                "output_chars": len(str(getattr(request.result, "output", "") or "")),
            },
            detail_payload={"tool_output": str(getattr(request.result, "output", "") or "")},
        )
    )


def _trace_runner_stage(bundle: RunnerStageTraceBundle) -> None:
    run_id = current_subagent_run_id(bundle.agent)
    if not run_id:
        return
    try:
        task = bundle.agent.subagents.load(run_id)
    except Exception as exc:
        _warn_runner_trace_error(exc, context="runner_stage_trace.subagents.load", run_id=run_id)
        return
    task = _touch_active_heartbeat_chain(bundle.agent.subagents, task)
    from ...subagents.debug_trace import SubAgentDebugTraceRequest, write_subagent_debug_trace

    write_subagent_debug_trace(
        SubAgentDebugTraceRequest(
            manager=bundle.agent.subagents,
            level=3,
            event_type=bundle.event_type,
            task=task,
            payload={
                "source": str(getattr(bundle.params, "source", "") or ""),
                "request_id": str(getattr(bundle.params, "request_id", "") or ""),
                "runtime_run_id": str(getattr(bundle.params, "run_id", "") or ""),
                "runtime_task_id": str(getattr(bundle.params, "task_id", "") or ""),
                "tool_rounds": int(bundle.tool_rounds or 0),
                **bundle.payload,
                **_detail_trace_payload(bundle.agent.subagents, task, bundle),
            },
        )
    )


def _touch_active_heartbeat_chain(manager: Any, task: Any) -> Any:
    import time

    now = time.time()
    current = task
    seen: set[str] = set()
    latest_current = task
    while current is not None:
        run_id = str(getattr(current, "id", "") or "").strip()
        if not run_id or run_id in seen:
            break
        seen.add(run_id)
        if not _heartbeat_active_task(current):
            break
        current.heartbeat_at = now
        current.updated_at = now
        try:
            manager.save(current)
        except Exception as exc:
            _warn_runner_trace_error(exc, context="runner_stage_trace.heartbeat.save", run_id=run_id)
            break
        if run_id == str(getattr(task, "id", "") or "").strip():
            latest_current = current
        parent_id = str(getattr(current, "parent_id", "") or "").strip()
        if not parent_id:
            break
        current = _next_heartbeat_parent(manager, current, parent_id)
        if current is None:
            break
    return latest_current


def _next_heartbeat_parent(manager: Any, current: Any, parent_id: str) -> Any | None:
    try:
        return manager.load(parent_id)
    except Exception as exc:
        if not _is_external_parent_anchor(current, parent_id):
            _warn_runner_trace_error(exc, context="runner_stage_trace.heartbeat.parent_load", run_id=parent_id)
        return None


def _warn_runner_trace_error(exc: Exception, *, context: str, run_id: str) -> None:
    report = runtime_error_report(exc, context=context)
    report["run_id"] = run_id
    _LOGGER.warning("runner stage trace failed: %s", report)


def _heartbeat_active_task(task: Any) -> bool:
    active_attempt = str(getattr(task, "runner_active_attempt_id", "") or "").strip()
    return task_has_status(task, TaskStatus.RUNNING) or bool(active_attempt)


def _is_external_parent_anchor(task: Any, parent_id: str) -> bool:
    root_id = str(getattr(task, "root_id", "") or "").strip()
    depth = _int_value(getattr(task, "depth", 0))
    task_id = str(getattr(task, "id", "") or "").strip()
    return bool(parent_id and parent_id == root_id and parent_id != task_id and depth <= 1)


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _detail_trace_payload(manager: Any, task: Any, bundle: RunnerStageTraceBundle) -> dict[str, Any]:
    if not bundle.detail_payload:
        return {}
    from ...subagents.debug_trace import (
        SubAgentDebugDetailRequest,
        configured_subagent_debug_trace_level,
        preview_debug_trace_text,
        write_subagent_debug_detail,
    )

    level = configured_subagent_debug_trace_level(manager)
    if level < 4:
        return {}
    payload: dict[str, Any] = {
        "task_goal_preview": preview_debug_trace_text(str(getattr(task, "goal", "") or "")),
    }
    for label, value in bundle.detail_payload.items():
        payload[f"{label}_preview"] = preview_debug_trace_text(value)
        detail_ref = write_subagent_debug_detail(
            SubAgentDebugDetailRequest(
                manager,
                task,
                event_type=bundle.event_type,
                label=label,
                value=value,
            )
        )
        if detail_ref:
            payload[f"{label}_detail_ref"] = detail_ref
    return payload


def _tool_name(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "unknown"
    return str(payload.get("tool") or "unknown")


def _payload_keys(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    return sorted(str(key) for key in payload.keys())[:32]
