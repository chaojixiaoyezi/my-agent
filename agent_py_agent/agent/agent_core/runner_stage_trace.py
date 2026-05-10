# LLM: Runner stage trace bridges core model/tool-loop lifecycle events into subagent debug tracing.
# 模块用途: 在子代理 runner 正在执行时记录模型请求、模型响应和工具调用阶段，帮助真实 E2E 定位长时间 RUNNING 卡点。

from __future__ import annotations

"""Refs-only runner stage tracing.

给人看的解释：
这里不是普通业务日志。它只在当前 SimpleAgent 正处于 subagent runner 上下文，
且 `subagent_debug_trace_level` 打开时写短事件。事件只包含阶段、工具名、
响应长度、字段名等信息，不写 prompt、response 或工具输出正文。
"""

from dataclasses import dataclass, field
from typing import Any


# LLM: RunnerModelStageTraceRequest keeps backend trace inputs bundled for bundle-interface rules.
# 类用途: 模型阶段 trace 参数包，保存当前 agent、运行参数、轮次和可选模型数据；调用方不需要传散乱参数。
@dataclass(frozen=True)
class RunnerModelStageTraceRequest:
    agent: Any
    params: Any
    tool_rounds: int
    prompt: str = ""
    response: Any = None
    exc: BaseException | None = None


# LLM: RunnerToolStageTraceRequest keeps tool trace inputs bundled for bundle-interface rules.
# 类用途: 工具阶段 trace 参数包，保存当前 agent、运行参数、工具序号、payload 和可选结果。
@dataclass(frozen=True)
class RunnerToolStageTraceRequest:
    agent: Any
    params: Any
    tool_rounds: int
    idx: int
    payload: Any
    result: Any = None


# LLM: RunnerStageTraceBundle keeps stage trace data explicit and bounded at the call boundary.
# 类用途: runner 阶段 trace 参数包，集中 agent、事件类型、运行参数和短 payload，避免工具循环签名继续扩张。
@dataclass(frozen=True)
class RunnerStageTraceBundle:
    agent: Any
    event_type: str
    params: Any
    tool_rounds: int
    payload: dict[str, Any] = field(default_factory=dict)
    detail_payload: dict[str, Any] = field(default_factory=dict)


# LLM: trace_runner_model_request_started records the last known point before a backend call can block.
# 函数用途: 写模型请求开始事件；如果模型接口长期不返回，trace 至少能显示卡在请求前后。
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


# LLM: trace_runner_model_response_received records that the backend returned without storing response text.
# 函数用途: 写模型响应返回事件，只记录 backend 和长度，不复制模型正文。
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


# LLM: trace_runner_model_request_failed records backend exceptions without swallowing them.
# 函数用途: 写模型请求失败事件，只记录异常类型和短消息，便于区分 API 异常与长时间无响应。
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


# LLM: trace_runner_tool_call_started records tool execution intent without storing full parameters.
# 函数用途: 写工具调用开始事件，记录工具名和参数键，避免把路径/正文/大参数直接展开到 trace。
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


# LLM: trace_runner_tool_call_finished records the tool result envelope without storing output text.
# 函数用途: 写工具调用结束事件，记录 ok、工具名和输出长度，避免把工具输出正文写入调试 JSONL。
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


# LLM: _trace_runner_stage resolves the active subagent task and delegates to the subagent trace writer.
# 函数用途: 只在当前 agent 有 `_current_subagent_run_id` 时写 trace；没有 runner 上下文时静默返回。
def _trace_runner_stage(bundle: RunnerStageTraceBundle) -> None:
    run_id = str(getattr(bundle.agent, "_current_subagent_run_id", "") or "").strip()
    if not run_id:
        return
    try:
        task = bundle.agent.subagents.load(run_id)
    except Exception:
        return
    from ..subagents.debug_trace import SubAgentDebugTraceRequest, write_subagent_debug_trace

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


# LLM: _detail_trace_payload adds opt-in level 4 previews and level 5 detail refs to stage events.
# 函数用途: 高等级真实 E2E 调试时记录模型/工具交互内容，默认等级不会写正文。
def _detail_trace_payload(manager: Any, task: Any, bundle: RunnerStageTraceBundle) -> dict[str, Any]:
    if not bundle.detail_payload:
        return {}
    from ..subagents.debug_trace import (
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


# LLM: _tool_name extracts a stable tool identifier from the model payload without trusting shape.
# 函数用途: 从工具 payload 中提取 tool/name 字段，异常形状返回 unknown。
def _tool_name(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "unknown"
    return str(payload.get("tool") or payload.get("name") or "unknown")


# LLM: _payload_keys exposes parameter shape for debugging while hiding actual parameter values.
# 函数用途: 返回工具调用 payload 的短键列表，用来判断模型传了哪些字段但不泄露字段内容。
def _payload_keys(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    return sorted(str(key) for key in payload.keys())[:32]
