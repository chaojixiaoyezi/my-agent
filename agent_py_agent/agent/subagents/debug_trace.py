# LLM: Subagent debug tracing is opt-in refs-only observability for real E2E runs; keep default level 0 silent.
# 模块用途: 按 0-5 等级写入子代理内部调试追踪，帮助测试多层代理时定位卡点，不污染用户产物目录。

from __future__ import annotations

"""Opt-in subagent debug tracing.

给人看的解释：
这个模块不是普通日志，也不是用户要看的报告。它只在
`subagent_debug_trace_level > 0` 时写内部 JSONL，用来做真实 E2E
排障。默认 0 完全关闭，所以正常使用不会多写调试文件。
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_py_agent.agent.file_io import append_jsonl

_PREVIEW_LIMIT = 240


# LLM: SubAgentDebugTraceRequest is the bundle boundary for trace writes; callers must pass refs, not large bodies.
# 类用途: 子代理调试追踪写入参数包，集中 event、任务快照和附加字段，避免业务入口散落调试参数。
@dataclass(frozen=True)
class SubAgentDebugTraceRequest:
    manager: Any
    level: int
    event_type: str
    task: Any = None
    payload: dict[str, Any] = field(default_factory=dict)


# LLM: SubAgentRunnerTraceRequest keeps runner trace inputs bundled to avoid another broad service-style signature.
# 类用途: runner 调试追踪参数包，集中 manager、task、result 和原始 params，方便后续扩展 trace 字段。
@dataclass(frozen=True)
class SubAgentRunnerTraceRequest:
    manager: Any
    task: Any
    result: Any
    params: Any


# LLM: write_subagent_debug_trace appends one bounded refs-only event when configured level allows it.
# 函数用途: 根据 manager.debug_trace_level 判断是否写调试事件；只写内部 workspace/debug_traces/subagent_trace.jsonl。
def write_subagent_debug_trace(request: SubAgentDebugTraceRequest) -> Path | None:
    configured_level = _configured_trace_level(request.manager)
    event_level = _bounded_level(request.level)
    if configured_level <= 0 or event_level > configured_level:
        return None
    record = _build_trace_record(request, event_level)
    trace_file = Path(request.manager.workspace) / "debug_traces" / "subagent_trace.jsonl"
    append_jsonl(trace_file, record, sort_keys=True)
    return trace_file


# LLM: trace_task_created keeps creation observability near the lifecycle event without exposing prompt bodies.
# 函数用途: 记录任务创建事件，方便真实 E2E 看清 root/child/leaf 层级和角色。
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


# LLM: trace_runner_result records the runner close-out ref and status without copying prompt/response content.
# 函数用途: 记录 runner 收束事件，让测试能追踪 ok/status/verification/结果文件引用。
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


# LLM: _build_trace_record keeps common task fields stable across trace event types.
# 函数用途: 组装单条 JSONL 记录，字段保持短小、可检索、refs-only。
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


# LLM: _task_trace_fields extracts searchable hierarchy/status fields without reading cold artifacts.
# 函数用途: 从任务快照里提取 run/root/parent/depth/role/status 等短字段。
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


# LLM: _safe_payload bounds arbitrary diagnostic values so higher trace levels do not explode files or context.
# 函数用途: 限制调试字段的体积和类型，避免大正文、复杂对象或异常值进入 trace。
def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        safe[str(key)] = _safe_payload_value(value)
    return safe


# LLM: _safe_payload_value keeps value coercion flat so trace events stay easy to audit.
# 函数用途: 把单个调试字段裁剪成 JSON 友好短值，不展开复杂对象。
def _safe_payload_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, int | float):
        return value
    if isinstance(value, str):
        return _preview(value)
    return _preview(str(value))


# LLM: _configured_trace_level accepts manager values defensively because tests may instantiate managers directly.
# 函数用途: 读取并裁剪 manager.debug_trace_level，坏值按 0 关闭处理。
def _configured_trace_level(manager: Any) -> int:
    return _bounded_level(getattr(manager, "debug_trace_level", 0))


# LLM: _bounded_level keeps trace levels in the documented 0-5 range.
# 函数用途: 把输入追踪等级转成 0-5 整数，异常值回退 0。
def _bounded_level(value: Any) -> int:
    try:
        level = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(5, level))


# LLM: _preview keeps trace records searchable while avoiding prompt/response body duplication.
# 函数用途: 截断长文本并去掉换行，确保调试 JSONL 单行短小。
def _preview(value: str) -> str:
    compact = " ".join(str(value).split())
    if len(compact) <= _PREVIEW_LIMIT:
        return compact
    return compact[: _PREVIEW_LIMIT - 3] + "..."
