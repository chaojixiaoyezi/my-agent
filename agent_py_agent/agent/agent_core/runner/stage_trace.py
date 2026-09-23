
# LLM: 阶段追踪负责真实 runner 活动与心跳；上下文数值由统一 preflight 写入 thread，不在此重复落盘。
# 模块用途: 记录子代理模型/工具阶段与心跳，不用调试文本推导任务完成或权限。
from __future__ import annotations

"""Refs-only runner stage tracing.

这里不是普通业务日志。普通阶段短事件只在当前 SimpleAgent 正处于 subagent runner
上下文且 `subagent_debug_trace_level` 打开时写入。上下文用量已统一在 model preflight
写入各自 ConversationThread，不再于此重复保存；阶段和心跳路径保持不变。
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ...runtime_context import current_subagent_run_id
from ...runtime_errors import runtime_error_report
from ...subagents.models import TaskStatus, task_has_status
from ...tooling.runtime_contracts import ToolCall, ToolResult

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunnerModelStageTraceRequest:
    agent: Any
    params: Any
    tool_rounds: int
    prompt: str = ""
    response: Any = None
    exc: BaseException | None = None


# LLM: Streaming activity carries counts and transport phase only; model text/reasoning and tool
# JSON are forbidden so the canonical child state stays a safe status projection.
# 类用途: 表示慢模型仍在持续输出哪类数据，让 TUI/Web 能区分“慢”与“卡死”。
@dataclass(frozen=True)
class RunnerModelStreamActivityTraceRequest:
    agent: Any
    params: Any
    tool_rounds: int
    stream_kind: str
    observed_chars: int = 0


# LLM: Retry activity is derived only from the provider attempt observer's typed fields. Error
# bodies and credentials must never enter the child-state projection.
# 类用途: 表示一次模型连接即将按退避策略重试，供子代理状态栏显示尝试次数和等待时间。
@dataclass(frozen=True)
class RunnerProviderRetryTraceRequest:
    agent: Any
    params: Any
    tool_rounds: int
    attempt: int
    total: int
    delay_seconds: float
    error_type: str = ""


@dataclass(frozen=True)
class RunnerToolStageTraceRequest:
    agent: Any
    params: Any
    tool_rounds: int
    idx: int
    call: ToolCall
    result: ToolResult | None = None


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
    # EXEC-31b: native 下工具调用在 tool_use_blocks(text 常为空)——trace 只记 text
    # 会让 debug preview 变成空串, 丢调工具名事实。detail 里并入工具块名。
    text = str(getattr(request.response, "text", "") or "")
    blocks = list(getattr(request.response, "tool_use_blocks", None) or [])
    tool_names = [str(block.get("name") or "") for block in blocks if isinstance(block, dict)]
    detail_text = text
    if tool_names:
        detail_text = (detail_text + "\n" if detail_text else "") + "[tool_use] " + ", ".join(tool_names)
    _trace_runner_stage(
        RunnerStageTraceBundle(
            agent=request.agent,
            event_type="runner_model_response_received",
            params=request.params,
            tool_rounds=request.tool_rounds,
            payload={
                "backend": str(getattr(request.response, "backend", "") or ""),
                "response_chars": len(text),
                "tool_use_names": tool_names,
            },
            detail_payload={"response": detail_text},
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


# LLM: The caller throttles this hot-path event before it reaches persistence. This function
# performs the same exact-run identity check and bounded projection as other runner stages.
# 函数用途: 把模型正文、思考或工具参数仍在流动的事实写进当前子代理状态。
def trace_runner_model_stream_active(
    request: RunnerModelStreamActivityTraceRequest,
) -> None:
    kind = str(request.stream_kind or "output").strip().lower()
    if kind not in {"output", "thinking", "tool_input"}:
        kind = "output"
    _trace_runner_stage(
        RunnerStageTraceBundle(
            agent=request.agent,
            event_type="runner_model_stream_active",
            params=request.params,
            tool_rounds=request.tool_rounds,
            payload={
                "stream_kind": kind,
                "observed_chars": max(0, int(request.observed_chars or 0)),
            },
        )
    )


# LLM: Retry notices update observability only; provider retry authority remains the transport
# observer and its retry_scheduled field. This projection cannot start or suppress a retry.
# 函数用途: 把模型连接退避重试投影为当前子代理的可见运行状态。
def trace_runner_provider_retry_scheduled(
    request: RunnerProviderRetryTraceRequest,
) -> None:
    _trace_runner_stage(
        RunnerStageTraceBundle(
            agent=request.agent,
            event_type="runner_provider_retry_scheduled",
            params=request.params,
            tool_rounds=request.tool_rounds,
            payload={
                "attempt": max(1, int(request.attempt or 1)),
                "total": max(1, int(request.total or 1)),
                "delay_seconds": max(0.0, float(request.delay_seconds or 0.0)),
                "error_type": str(request.error_type or "")[:80],
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
                "tool": request.call.tool_name,
                "payload_keys": sorted(request.call.arguments)[:32],
            },
            detail_payload={"tool_payload": request.call.to_dict()},
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
                "tool": request.result.tool_name if request.result is not None else request.call.tool_name,
                "ok": bool(request.result and request.result.ok),
                "output_chars": len(request.result.output if request.result is not None else ""),
                "failure_stage": request.result.failure_stage if request.result is not None else "",
                "handler_executed": bool(request.result and request.result.handler_executed),
                "duration_ms": request.result.duration_ms if request.result is not None else 0,
            },
            detail_payload={
                "tool_output": request.result.output if request.result is not None else ""
            },
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
    task = _persist_runner_activity(bundle.agent.subagents, task, bundle)
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


# LLM: 这是一份宿主观测到的有界活动投影，不保存模型正文或隐式推理；
# 每个离散模型/工具边界更新一次 canonical child state，供父级状态面和 TUI 判断是否仍在工作。
# 函数用途: 把“正在请求模型/正在用哪个工具/工具刚成功或失败”写进子代理状态，避免只剩空 RUNNING 心跳。
def _persist_runner_activity(manager: Any, task: Any, bundle: RunnerStageTraceBundle) -> Any:

    now = time.time()
    summary, tool = _runner_activity_summary(bundle)
    if not summary:
        return task
    task.current_step = summary
    task.current_tool = tool
    task.heartbeat_at = now
    task.updated_at = now
    attrs = dict(getattr(task, "attributes", {}) or {})
    recent = attrs.get("recent_runtime_activity")
    items = list(recent) if isinstance(recent, list) else []
    entry = {
        "kind": bundle.event_type,
        "summary": summary,
        "tool": tool,
        "tool_round": int(bundle.tool_rounds or 0),
        "at": now,
        **_runner_activity_public_fields(bundle),
    }
    items.append(entry)
    attrs["runtime_activity"] = entry
    attrs["recent_runtime_activity"] = [
        item for item in items if isinstance(item, dict)
    ][-6:]
    task.attributes = attrs
    try:
        manager.save(task)
    except Exception as exc:
        _warn_runner_trace_error(
            exc,
            context="runner_stage_trace.activity.save",
            run_id=str(getattr(task, "id", "") or ""),
        )
    return task


# LLM: Only explicit numeric/enumerated fields may accompany the short activity summary. Prompt,
# response, tool arguments/output and provider error bodies are never copied from detail payloads.
# 函数用途: 为流式状态和重试状态保留可展示数字，不把模型或工具正文写进 canonical state。
def _runner_activity_public_fields(bundle: RunnerStageTraceBundle) -> dict[str, object]:
    payload = bundle.payload if isinstance(bundle.payload, dict) else {}
    if bundle.event_type == "runner_model_stream_active":
        return {
            "stream_kind": str(payload.get("stream_kind") or "output")[:24],
            "observed_chars": max(0, _int_value(payload.get("observed_chars"))),
        }
    if bundle.event_type == "runner_provider_retry_scheduled":
        return {
            "attempt": max(1, _int_value(payload.get("attempt"))),
            "total": max(1, _int_value(payload.get("total"))),
            "delay_seconds": max(0.0, _float_value(payload.get("delay_seconds"))),
            "error_type": str(payload.get("error_type") or "")[:80],
        }
    return {}


# LLM: 活动摘要只由 typed stage 与工具名生成；禁止摘抄 prompt、response、工具输出或模型思考正文。
# 函数用途: 将 runner 的离散阶段翻译成短小、可安全展示的中文状态。
def _runner_activity_summary(bundle: RunnerStageTraceBundle) -> tuple[str, str]:
    payload = bundle.payload if isinstance(bundle.payload, dict) else {}
    if bundle.event_type == "runner_model_request_started":
        return "模型响应中", ""
    if bundle.event_type == "runner_model_response_received":
        tool_names = [
            str(item or "").strip()
            for item in payload.get("tool_use_names", [])
            if str(item or "").strip()
        ] if isinstance(payload.get("tool_use_names"), list) else []
        if tool_names:
            return f"模型已选择工具：{tool_names[0]}", tool_names[0]
        return "模型已生成回复", ""
    if bundle.event_type == "runner_model_request_failed":
        error_type = str(payload.get("error_type") or "模型错误").strip()
        return f"模型请求失败：{error_type}", ""
    if bundle.event_type == "runner_model_stream_active":
        kind = str(payload.get("stream_kind") or "output")
        if kind == "thinking":
            return "模型持续思考中", ""
        if kind == "tool_input":
            return "模型正在生成工具参数", ""
        return "模型持续响应中", ""
    if bundle.event_type == "runner_provider_retry_scheduled":
        attempt = max(1, _int_value(payload.get("attempt")))
        total = max(1, _int_value(payload.get("total")))
        delay = max(0.0, _float_value(payload.get("delay_seconds")))
        wait_text = f"{delay:g} 秒" if delay else "稍后"
        return f"模型连接重试 {attempt}/{total}，等待 {wait_text}", ""
    if bundle.event_type == "runner_tool_call_started":
        tool = str(payload.get("tool") or "").strip()
        return (f"正在使用工具：{tool}" if tool else "正在使用工具"), tool
    if bundle.event_type == "runner_tool_call_finished":
        tool = str(payload.get("tool") or "").strip()
        outcome = "完成" if payload.get("ok") is True else "失败"
        return (f"工具{outcome}：{tool}" if tool else f"工具{outcome}"), tool
    return "", ""


def _touch_active_heartbeat_chain(manager: Any, task: Any) -> Any:

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


# LLM: Status rendering tolerates malformed persisted numeric fields without changing lifecycle.
# 函数用途: 把重试等待时间安全转换为非异常浮点数。
def _float_value(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


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
