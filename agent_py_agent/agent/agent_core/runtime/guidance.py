
from __future__ import annotations

from typing import Any

from ...runtime_errors import runtime_error_report


def inject_pending_guidance(agent: object, params: object, *, now: float | None = None) -> bool:
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return False
    entries = []
    run_id = str(getattr(params, "run_id", "") or "").strip()
    task_id = str(getattr(params, "task_id", "") or "").strip()
    if run_id:
        entries.extend(store.pending_guidance("agent_run", run_id, limit=20))
    if task_id:
        entries.extend(store.pending_guidance("task", task_id, limit=20))
    thread_id, thread_lookup_error = _thread_id_for_task(store, task_id)
    if thread_id:
        entries.extend(store.pending_guidance("thread", thread_id, limit=20))
    entries = _dedupe_guidance(entries)
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
    store.mark_guidance_delivered([entry.guidance_id for entry in entries], now=now)
    return True


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
        "以下是运行中补充提示，代表最新用户/上级上下文；如果它和较早任务合同、旧工具记录冲突，以这里为准。"
        "它不是新的硬门；没有明确停止、改目标或收口要求时，按原任务继续。",
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
