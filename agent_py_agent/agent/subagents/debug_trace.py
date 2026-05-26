# LLM: Subagent debug tracing is opt-in refs-only observability for real E2E runs; keep default level 0 silent.
# 模块用途: 按 0-5 等级写入子代理内部调试追踪，帮助测试多层代理时定位卡点，不污染用户产物目录。

from __future__ import annotations

"""Opt-in subagent debug tracing.

给人看的解释：
这个模块不是普通日志，也不是用户要看的报告。它只在
`subagent_debug_trace_level > 0` 时写内部 JSONL，用来做真实 E2E
排障。默认 0 完全关闭，所以正常使用不会多写调试文件。
"""

import json
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


# LLM: SubAgentDebugDetailRequest bundles opt-in full-detail trace writes behind one parameter.
# 类用途: 保存等级 5 详情日志写入所需的 manager、task、事件名、标签和值，避免调试接口参数继续增长。
@dataclass(frozen=True)
class SubAgentDebugDetailRequest:
    manager: Any
    task: Any
    event_type: str
    label: str
    value: Any


# LLM: SubAgentRunnerTraceRequest keeps runner trace inputs bundled to avoid another broad service-style signature.
# 类用途: runner 调试追踪参数包，集中 manager、task、result 和原始 params，方便后续扩展 trace 字段。
@dataclass(frozen=True)
class SubAgentRunnerTraceRequest:
    manager: Any
    task: Any
    result: Any
    params: Any


# LLM: SubAgentHierarchyTraceRequest keeps hierarchy observability tied to schedule result refs.
# 类用途: 层级调度 trace 参数包，记录 parent task 和调度摘要，不展开子任务正文。
@dataclass(frozen=True)
class SubAgentHierarchyTraceRequest:
    manager: Any
    parent_task: Any
    result: Any


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


# LLM: configured_subagent_debug_trace_level exposes the normalized level for trace detail writers.
# 函数用途: 给 runner stage trace 判断是否写短预览或完整 detail 文件；坏值按 0 处理。
def configured_subagent_debug_trace_level(manager: Any) -> int:
    return _configured_trace_level(manager)


# LLM: preview_debug_trace_text reuses the trace truncation policy outside this module.
# 函数用途: 生成可 tail 的短预览，避免高等级调试把大 prompt 直接塞进 JSONL。
def preview_debug_trace_text(value: Any) -> str:
    return _preview(_detail_text(value))


# LLM: write_subagent_debug_detail stores full opt-in test logs outside the main JSONL trace.
# 函数用途: 等级 5 时把完整 prompt、response、工具参数或工具输出写到内部 detail 文件，并返回路径引用。
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


# LLM: trace_hierarchy_schedule_result records fan-out decisions without reading child prompts or outputs.
# 函数用途: 记录一次层级调度的 dry-run/apply、阻断原因、创建数量和子任务引用。
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


# LLM: trace_hierarchy_schedule is a thin caller-friendly bridge for scheduler lifecycle points.
# 函数用途: 让调度器一行写 trace 并返回原 result，避免服务文件增长。
def trace_hierarchy_schedule(manager: Any, parent_task: Any, result: Any) -> Any:
    trace_hierarchy_schedule_result(SubAgentHierarchyTraceRequest(manager, parent_task, result))
    return result


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
    if isinstance(value, list):
        return [_safe_payload_value(item) for item in value[:32]]
    # LLM: report summaries may be dicts; keep keys/values bounded instead of stringifying the whole map.
    if isinstance(value, dict):
        return {str(key): _safe_payload_value(item) for key, item in list(value.items())[:32]}
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


# LLM: _detail_text serializes debug detail values for level-5 files only.
# 函数用途: 把 prompt/response/工具参数转成可读文本，复杂对象用 JSON，字符串保持原文。
def _detail_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        return str(value)


# LLM: _safe_name keeps trace detail filenames portable and independent from user/task text.
# 函数用途: 清理 run id、事件名和 label，只保留适合文件名的短字符。
def _safe_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in str(value))
    return cleaned.strip("-")[:80] or "item"
