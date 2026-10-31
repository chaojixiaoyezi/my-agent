"""结构化执行事件的无状态只读投影；不执行工具、不保存业务状态。"""
from __future__ import annotations

import json
from collections.abc import Mapping
from itertools import islice

from ..tooling.runtime_contracts import ToolCall, ToolResult

MAX_EXECUTION_STEPS = 100
MAX_EXECUTION_EVENTS = 2000
MAX_LABEL_BYTES = 240
_TERMINAL_PHASES = frozenset({"finished", "completed", "succeeded", "failed", "cancelled", "interrupted", "denied", "blocked", "skipped"})
_CONFIRMED_RESULT_STATUSES = frozenset({"succeeded", "failed", "cancelled", "skipped"})


# LLM: 仅接受原 ToolCall/ToolResult 或宿主结构化 progress；最多观察2000事件，结果不授予操作身份或写入任何状态。
# 函数用途: 生成按首次观察排序的有界步骤；终态冻结，未知总量不从已观察数反推。
def project_execution_view(events, *, request_id: str, source_revision: int | None = None,
                           total_steps: int | None = None, max_steps: int = 50) -> dict:
    limit = max(1, min(MAX_EXECUTION_STEPS, max_steps))
    steps, seen = {}, set()
    request_phase, events_truncated = "unknown", False
    for index, event in enumerate(islice(events, MAX_EXECUTION_EVENTS + 1)):
        if index == MAX_EXECUTION_EVENTS:
            events_truncated = True
            break
        if isinstance(event, Mapping) and event.get("request_id", request_id) != request_id:
            continue
        if isinstance(event, Mapping) and event.get("kind") in {"request_started", "request_completed", "request_failed"}:
            phase = event.get("phase", event.get("status"))
            if isinstance(phase, str) and request_phase not in _TERMINAL_PHASES:
                request_phase = phase
        facts = _tool_facts(event)
        if facts is None:
            continue
        if facts.get("request_id", request_id) != request_id:
            continue
        call_id = facts.get("call_id")
        display_only = not isinstance(call_id, str) or not call_id
        if display_only:
            round_number, call_index = facts.get("round"), facts.get("call_index")
            if type(round_number) is not int or type(call_index) is not int:
                continue
            step_id = "display:" + json.dumps([request_id, round_number, call_index], ensure_ascii=False)
        else:
            step_id = call_id
        # 内部身份带来源种类，显示键不能与真实 call_id 碰撞而合并操作。
        key = (display_only, step_id)
        seen.add(key)
        if key not in steps and len(steps) >= limit:
            continue
        if key in steps and steps[key]["_terminal"]:
            continue
        previous = steps.get(key, {})
        phase = facts.get("phase")
        phase = phase if isinstance(phase, str) and phase else "unknown"
        duration = facts.get("duration_ms")
        step = {
            "step_id": step_id, "display_only": display_only,
            "tool_name": facts["tool_name"], "phase": phase,
            "ok": facts.get("ok") if type(facts.get("ok")) is bool else previous.get("ok"),
            "handler_executed": (facts["handler_executed"] if type(facts.get("handler_executed")) is bool
                                 else previous.get("handler_executed")),
            "duration_ms": duration if type(duration) is int and duration >= 0 else previous.get("duration_ms"),
            "label": bounded_text(facts.get("label", facts.get("detail", previous.get("label", facts["tool_name"]))),
                                  max_bytes=MAX_LABEL_BYTES),
            "_terminal": bool(facts.get("_terminal")) or phase in _TERMINAL_PHASES,
        }
        if isinstance(facts.get("detail_ref"), str):
            step["detail_ref"] = bounded_text(facts["detail_ref"], max_bytes=240)
        steps[key] = step
    total = total_steps if type(total_steps) is int and total_steps >= len(seen) else None
    return {
        "request_id": request_id, "source_revision": source_revision, "request_phase": request_phase,
        "steps": [{key: value for key, value in step.items() if key != "_terminal"} for step in steps.values()],
        "total_steps": total,
        "truncation": {"observed_steps": len(seen), "displayed_steps": len(steps),
                       "omitted_steps": len(seen) - len(steps), "events_truncated": events_truncated},
    }


# LLM: 不读取 arguments/content_blocks/output；自然语言或 assistant 事件即使带 tool_name 也不能产生步骤。
# 函数用途: 保留真实工具阶段；审批等待/未知结果可接原调用后续核实，只有确认终态冻结。
def _tool_facts(event: object) -> dict | None:
    if isinstance(event, ToolCall):
        return {"call_id": event.call_id, "tool_name": event.tool_name, "phase": "started"}
    if isinstance(event, ToolResult):
        return {"call_id": event.call_id, "tool_name": event.tool_name, "phase": event.status,
                "ok": event.ok, "handler_executed": event.handler_executed,
                "duration_ms": event.duration_ms, "_terminal": event.status in _CONFIRMED_RESULT_STATUSES}
    if not isinstance(event, Mapping):
        return None
    kind = event.get("kind")
    if kind == "tool_progress":
        facts = event.get("progress", event.get("payload", event))
    elif kind in {"tool_call", "tool_result"}:
        facts = event.get("call" if kind == "tool_call" else "result", event)
        if isinstance(facts, (ToolCall, ToolResult)):
            return _tool_facts(facts)
    elif kind is None and "tool" in event and "phase" in event:
        facts = event
    else:
        return None
    if not isinstance(facts, Mapping):
        return None
    name = facts.get("tool_name", facts.get("tool"))
    if not isinstance(name, str) or not name:
        return None
    result = {**facts, "tool_name": name}
    if kind == "tool_call":
        result.setdefault("phase", "started")
    elif kind == "tool_result":
        result.setdefault("phase", facts.get("status", "unknown"))
        result["_terminal"] = facts.get("status") in _CONFIRMED_RESULT_STATUSES
        if "ok" not in result and facts.get("status") in {"succeeded", "failed"}:
            result["ok"] = facts["status"] == "succeeded"
    return result


# LLM: 按 UTF-8 字节预算裁剪并丢弃非法 surrogate；不截断多字节字符，不调用网络、时钟或持久化。
# 函数用途: 为步骤标签和卡片展示字段提供同一安全字符串上限。
def bounded_text(value: object, *, max_bytes: int) -> str:
    text = value if isinstance(value, str) else ""
    # 先按字符取窗口，再编码，避免为超长来源构造整份 UTF-8 副本。
    raw = text[:max_bytes].encode("utf-8", errors="replace")
    return raw[:max_bytes].decode("utf-8", errors="ignore")
