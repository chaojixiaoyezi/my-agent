# LLM: Runtime guidance renders soft steering notes into the next model turn.
# 模块用途: 从统一 guidance 账本读取补充提示，并注入主代理/子代理下一轮 prompt。

from __future__ import annotations

from typing import Any


# LLM: inject_pending_guidance appends one-shot guidance into main-agent tool context.
# 函数用途: 主代理每次构建工具循环 prompt 前读取未投递 guidance；只提示，不阻断。
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
    thread_id = _thread_id_for_task(store, task_id)
    if thread_id:
        entries.extend(store.pending_guidance("thread", thread_id, limit=20))
    entries = _dedupe_guidance(entries)
    if not entries:
        return False
    context = _render_guidance_entries(entries, title="GUIDANCE_DELIVERED")
    tool_context = getattr(params, "tool_context", None)
    if isinstance(tool_context, list):
        tool_context.append(context)
    runtime_injections = getattr(params, "runtime_injections", None)
    if isinstance(runtime_injections, list):
        runtime_injections.append(context)
    store.mark_guidance_delivered([entry.guidance_id for entry in entries], now=now)
    return True


# LLM: render_subagent_guidance_section is reused by runner prompt construction.
# 函数用途: 子代理 runner 开始前读取点名给该 run 的 guidance，渲染为 Extra Instruction 的补充段。
def render_subagent_guidance_section(store: object, run_id: str, *, now: float | None = None) -> str:
    if store is None or not str(run_id or "").strip():
        return ""
    entries = store.pending_guidance("agent_run", str(run_id), limit=20)
    if not entries:
        return ""
    store.mark_guidance_delivered([entry.guidance_id for entry in entries], now=now)
    return _render_guidance_entries(entries, title="GUIDANCE_DELIVERED")


# LLM: _thread_id_for_task resolves optional thread guidance from a task binding.
# 函数用途: 根据 task_id 反查长期会话 thread_id；失败时保持空值，不影响主循环。
def _thread_id_for_task(store: object, task_id: str) -> str:
    if not task_id:
        return ""
    try:
        thread = store.thread_for_task(task_id)
    except Exception:
        return ""
    return str(getattr(thread, "thread_id", "") or "")


# LLM: _dedupe_guidance keeps multi-target guidance from rendering twice.
# 函数用途: 主代理同时命中 run/task/thread guidance 时按 guidance_id 去重。
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


# LLM: _render_guidance_entries creates a compact model-facing guidance block.
# 函数用途: 把 guidance 账本行渲染成中文软提示，明确不会阻断任务。
def _render_guidance_entries(entries: list[Any], *, title: str) -> str:
    lines = [
        f"[{title}]",
        "以下是运行中补充提示，只用于下一步判断；不要把它当成新的硬门，也不要因为提示本身停止任务。",
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
