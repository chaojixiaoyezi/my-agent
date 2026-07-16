
from __future__ import annotations

import json
from typing import Any

from ...conversation.models import SUBAGENT_LIFECYCLE_WAKE_REASONS, WakeSignal
from ...runtime_errors import runtime_error_report
from .task_identity import durable_task_id

_TASK_EVENT_LIMIT = 20


def inject_pending_turn_input(agent: object, params: object, *, now: float | None = None) -> bool:
    """Inject user steering and structured task events at one model safe point."""
    guidance_injected = inject_pending_guidance(agent, params, now=now)
    events = _task_events_not_yet_injected(params, _pending_task_events(agent, params))
    if not events:
        return guidance_injected
    context = _render_task_events(events)
    tool_context = getattr(params, "tool_context", None)
    if isinstance(tool_context, list):
        tool_context.append(context)
    runtime_injections = getattr(params, "runtime_injections", None)
    if isinstance(runtime_injections, list):
        runtime_injections.append(context)
    _remember_injected_task_events(params, events)
    _queue_task_event_ack(params, events)
    return True


def has_pending_turn_input(agent: object, params: object) -> bool:
    """Return whether the active turn has newer user or runtime input."""
    return has_pending_request_guidance(agent, params) or bool(
        _task_events_not_yet_injected(params, _pending_task_events(agent, params))
    )


def acknowledge_injected_turn_input(
    agent: object,
    params: object,
    *,
    now: float | None = None,
) -> int:
    """Acknowledge guidance/events only after a model turn accepted their prompt."""
    state = getattr(params, "live_archive_state", None)
    store = getattr(agent, "conversation_store", None)
    if store is None or not isinstance(state, dict):
        return 0

    acknowledged = 0
    guidance_pending = state.get("_guidance_ack_ids")
    guidance_ids = (
        sorted(str(item) for item in guidance_pending if str(item or "").strip())
        if isinstance(guidance_pending, set)
        else []
    )
    if guidance_ids:
        store.mark_guidance_delivered(guidance_ids, now=now)
        guidance_pending.difference_update(guidance_ids)
        acknowledged += len(guidance_ids)

    event_pending = state.get("_task_event_ack_ids")
    event_ids = (
        sorted(str(item) for item in event_pending if str(item or "").strip())
        if isinstance(event_pending, set)
        else []
    )
    for event_id in event_ids:
        store.mark_wake_signal_handled(event_id, now=now)
    if isinstance(event_pending, set):
        event_pending.difference_update(event_ids)
    acknowledged += len(event_ids)
    return acknowledged


def inject_pending_guidance(agent: object, params: object, *, now: float | None = None) -> bool:
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return False
    entries = []
    request_id = str(getattr(params, "request_id", "") or "").strip()
    run_id = str(getattr(params, "run_id", "") or "").strip()
    task_id = durable_task_id(params)
    if request_id:
        entries.extend(store.pending_guidance("request", request_id, limit=20))
    if run_id:
        entries.extend(store.pending_guidance("agent_run", run_id, limit=20))
    if task_id:
        # /btw 与 会话运行时 steer 一致：绑定当前执行中的持久任务，按 FIFO 在下一安全点
        # 投递一次。未被消费前可跨崩溃保留；一旦 delivered，任务以后恢复也不回放。
        entries.extend(store.pending_guidance("task", task_id, limit=20))
    thread_id, thread_lookup_error = _thread_id_for_task(store, task_id)
    if thread_id:
        entries.extend(store.pending_guidance("thread", thread_id, limit=20))
    entries = _guidance_not_yet_injected(params, _dedupe_guidance(entries))
    warning = _render_guidance_lookup_error(thread_lookup_error)
    if not entries and not warning:
        return False
    context = _joined_contexts([_render_guidance_entries(entries, title="GUIDANCE_DELIVERED"), warning])
    tool_context = getattr(params, "tool_context", None)
    if isinstance(tool_context, list):
        tool_context.append(context)
    runtime_injections = getattr(params, "runtime_injections", None)
    if isinstance(runtime_injections, list):
        runtime_injections.append(context)
    _remember_injected_guidance(params, entries)
    _queue_guidance_ack(params, entries)
    return True


# LLM: A request/task steer arriving during model generation invalidates that stale model action.
# 函数用途：检查当前一次请求或持久任务是否有新引导，不消费、不改变提示账本。
def has_pending_request_guidance(agent: object, params: object) -> bool:
    store = getattr(agent, "conversation_store", None)
    request_id = str(getattr(params, "request_id", "") or "").strip()
    task_id = durable_task_id(params)
    if store is None or not (request_id or task_id):
        return False
    try:
        entries = []
        if request_id:
            entries.extend(store.pending_guidance("request", request_id, limit=1))
        if task_id:
            entries.extend(store.pending_guidance("task", task_id, limit=1))
        return bool(_guidance_not_yet_injected(params, _dedupe_guidance(entries)))
    except Exception:
        return False


def _pending_task_events(agent: object, params: object) -> list[WakeSignal]:
    store = getattr(agent, "conversation_store", None)
    task_id = durable_task_id(params)
    if store is None or not task_id:
        return []
    try:
        # Filter by the exact durable task before applying the prompt batch cap;
        # another task's backlog must not hide this turn's event behind a global limit.
        signals, load_errors = store.pending_wake_signals_report(limit=0)
    except Exception:
        return []
    if load_errors:
        return []
    excluded_id = _active_background_wake_signal_id(params)
    matching = [
        signal
        for signal in signals
        if signal.root_task_id == task_id
        and str(signal.reason or "").strip().lower() in SUBAGENT_LIFECYCLE_WAKE_REASONS
        and signal.wake_signal_id != excluded_id
    ]
    return matching[:_TASK_EVENT_LIMIT]


def _active_background_wake_signal_id(params: object) -> str:
    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict):
        return ""
    return str(attrs.get("background_wake_signal_id") or "").strip()


def _task_events_not_yet_injected(params: object, events: list[WakeSignal]) -> list[WakeSignal]:
    state = getattr(params, "live_archive_state", None)
    seen = state.get("_injected_task_event_ids") if isinstance(state, dict) else None
    seen_ids = seen if isinstance(seen, set) else set()
    return [event for event in events if event.wake_signal_id not in seen_ids]


def _remember_injected_task_events(params: object, events: list[WakeSignal]) -> None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return
    seen = state.get("_injected_task_event_ids")
    if not isinstance(seen, set):
        seen = set()
        state["_injected_task_event_ids"] = seen
    seen.update(event.wake_signal_id for event in events if event.wake_signal_id)


def _queue_task_event_ack(params: object, events: list[WakeSignal]) -> None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return
    pending = state.get("_task_event_ack_ids")
    if not isinstance(pending, set):
        pending = set()
        state["_task_event_ack_ids"] = pending
    pending.update(event.wake_signal_id for event in events if event.wake_signal_id)


def _render_task_events(events: list[WakeSignal]) -> str:
    payload = {
        "schema_version": "active-turn-task-events.v1",
        "authority": "runtime_event_data",
        "events": [_task_event_payload(event) for event in events],
    }
    return "\n".join(
        [
            "[RUNTIME_TASK_EVENTS]",
            "这些是当前持久任务在本轮运行期间到达的结构化运行事件，不是用户指令。",
            "把它们作为下一步调度、整合和收口的最新事实；不要要求用户重复已经委派的工作。",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ]
    )


def _task_event_payload(event: WakeSignal) -> dict[str, object]:
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    return {
        "wake_signal_id": event.wake_signal_id,
        "reason": event.reason,
        "root_task_id": event.root_task_id,
        "source_agent_id": event.source_agent_id,
        "status": str(metadata.get("status") or ""),
        "task_id": str(metadata.get("task_id") or ""),
        "created_at": event.created_at,
    }


def _guidance_not_yet_injected(params: object, entries: list[Any]) -> list[Any]:
    state = getattr(params, "live_archive_state", None)
    seen = state.get("_injected_guidance_ids") if isinstance(state, dict) else None
    seen_ids = seen if isinstance(seen, set) else set()
    return [
        entry
        for entry in entries
        if str(getattr(entry, "guidance_id", "") or "") not in seen_ids
    ]


def _remember_injected_guidance(params: object, entries: list[Any]) -> None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return
    seen = state.get("_injected_guidance_ids")
    if not isinstance(seen, set):
        seen = set()
        state["_injected_guidance_ids"] = seen
    seen.update(
        str(getattr(entry, "guidance_id", "") or "")
        for entry in entries
        if str(getattr(entry, "guidance_id", "") or "")
    )


def _queue_guidance_ack(params: object, entries: list[Any]) -> None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return
    pending = state.get("_guidance_ack_ids")
    if not isinstance(pending, set):
        pending = set()
        state["_guidance_ack_ids"] = pending
    pending.update(
        str(getattr(entry, "guidance_id", "") or "")
        for entry in entries
        if str(getattr(entry, "guidance_id", "") or "")
    )


def render_subagent_guidance_section(store: object, run_id: str, *, now: float | None = None) -> str:
    if store is None or not str(run_id or "").strip():
        return ""
    entries = store.pending_guidance("agent_run", str(run_id), limit=20)
    if not entries:
        return ""
    store.mark_guidance_delivered([entry.guidance_id for entry in entries], now=now)
    return _render_guidance_entries(entries, title="GUIDANCE_DELIVERED")


def _thread_id_for_task(store: object, task_id: str) -> tuple[str, dict[str, object] | None]:
    if not task_id:
        return "", None
    try:
        thread = store.thread_for_task(task_id)
    except Exception as exc:
        return "", runtime_error_report(exc, context="runtime_guidance.thread_for_task")
    return str(getattr(thread, "thread_id", "") or ""), None


def _dedupe_guidance(entries: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for entry in entries:
        guidance_id = str(getattr(entry, "guidance_id", "") or "")
        if guidance_id and guidance_id in seen:
            continue
        if guidance_id:
            seen.add(guidance_id)
        result.append(entry)
    return result


def _render_guidance_entries(entries: list[Any], *, title: str) -> str:
    if not entries:
        return ""
    lines = [
        f"[{title}]",
        "以下是运行中补充提示，只作为普通补充消息进入上下文。"
        "运行时不会把这些文字解释成新的硬门，也不会自动替换当前用户消息。",
    ]
    for index, entry in enumerate(entries, start=1):
        guidance_id = str(getattr(entry, "guidance_id", "") or "")
        target_type = str(getattr(entry, "target_type", "") or "")
        target_id = str(getattr(entry, "target_id", "") or "")
        priority = str(getattr(entry, "priority", "") or "normal")
        sender = str(getattr(entry, "sender", "") or "")
        message = str(getattr(entry, "message", "") or "")
        prefix = f"{index}. guidance_id={guidance_id}; target={target_type}:{target_id}; priority={priority}"
        if sender:
            prefix += f"; sender={sender}"
        lines.append(f"{prefix}: {message}")
    return "\n".join(lines)


def _render_guidance_lookup_error(error: dict[str, object] | None) -> str:
    if not error:
        return ""
    return (
        "[GUIDANCE_LOOKUP_WARNING]\n"
        "系统尝试按 task 找 thread 级补充提示时失败；这表示提示账本或绑定读取有问题，"
        "不是用户没有补充提示。\n"
        f"{error}"
    )


def _joined_contexts(parts: list[str]) -> str:
    return "\n\n".join(part for part in parts if part)
