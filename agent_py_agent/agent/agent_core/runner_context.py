# LLM: Runner context helpers keep active subagent state centralized for nested orchestration.
# 模块用途: 读取当前正在执行的 subagent runner 上下文，避免各工具自行约定字段名。

from __future__ import annotations

import threading

_LOCAL = threading.local()


def _agent_context(agent) -> dict[str, object]:
    contexts = getattr(_LOCAL, "contexts", None)
    if not isinstance(contexts, dict):
        contexts = {}
        _LOCAL.contexts = contexts
    key = id(agent)
    context = contexts.get(key)
    if not isinstance(context, dict):
        context = {}
        contexts[key] = context
    return context


def set_current_subagent_context(
    agent,
    *,
    run_id: str,
    attempt_id: str = "",
    task_attributes: dict | None = None,
) -> dict[str, object]:
    """Set the active runner context for the current thread only."""
    context = _agent_context(agent)
    previous = dict(context)
    context["run_id"] = str(run_id or "").strip()
    context["attempt_id"] = str(attempt_id or "").strip()
    context["task_attributes"] = task_attributes
    return previous


def restore_current_subagent_context(agent, previous: dict[str, object]) -> None:
    """Restore a thread-local runner context snapshot."""
    contexts = getattr(_LOCAL, "contexts", None)
    if not isinstance(contexts, dict):
        return
    key = id(agent)
    if previous:
        contexts[key] = dict(previous)
    else:
        contexts.pop(key, None)


# LLM: current_subagent_run_id returns the active runner id set by subagent_run_flow.
# 函数用途: 给层级调度和 runner 内 dispatch 使用当前节点 ID；空字符串表示当前不是 subagent runner。
def current_subagent_run_id(agent) -> str:
    context = _agent_context(agent)
    value = context.get("run_id")
    if value is not None:
        return _text_run_id(value)
    return _text_run_id(getattr(agent, "_current_subagent_run_id", ""))


def current_subagent_attempt_id(agent) -> str:
    context = _agent_context(agent)
    value = context.get("attempt_id")
    if value is not None:
        return _text_run_id(value)
    return _text_run_id(getattr(agent, "_current_subagent_attempt_id", ""))


def current_task_attributes(agent) -> dict | None:
    context = _agent_context(agent)
    if "task_attributes" in context:
        value = context.get("task_attributes")
        return value if isinstance(value, dict) else None
    value = getattr(agent, "_current_task_attributes", None)
    return value if isinstance(value, dict) else None


# LLM: runner context ids must be real strings, not MagicMock/object reprs.
# 函数用途: 防止测试桩或坏对象被 str() 后误当真实 run_id，进而覆盖显式 scope。
def _text_run_id(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()
