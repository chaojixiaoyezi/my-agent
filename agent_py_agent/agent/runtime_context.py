# LLM: 当前 runner 身份是线程本地临时事实，Tooling、Conversation 与 core 共用此唯一入口；持久身份仍以原任务/会话账为准。
# 模块用途: 隔离同一 Agent 并发执行时的当前任务属性和 runner 身份，退出作用域时清理，避免跨线程串状态。
from __future__ import annotations

"""Thread-local transient runtime state for a shared Agent instance.

LLM: Durable task identity belongs to structured stores; this module contains only scoped in-process state and
must clean it when a scope or agent ends.

模块用途: 为不同运行层提供同一份当前线程的临时身份、任务属性和 runner 参数，不代替持久任务记录。
"""

import threading
import weakref
from pathlib import Path

_LOCAL = threading.local()


# LLM: SimpleAgent is shared by gateway workers, so transient run attributes must be isolated per OS thread.
# 类用途: 让现有 getattr/setattr/delattr 调用继续工作，同时避免前台聊天和后台任务互相覆盖当前运行状态。
class ThreadLocalAgentAttribute:
    """Descriptor storing one transient attribute per agent and worker thread."""

    # LLM: Each descriptor owns an independent thread-local weak map keyed by Agent identity.
    # 函数用途: 初始化一个与原 Agent 属性同名的线程本地描述符。
    def __init__(self, name: str) -> None:
        self.name = str(name)
        self._local = threading.local()

    # LLM: Weak keys prevent destroyed agents from leaking stale values into a later object-id reuse.
    # 函数用途: 取得当前线程的弱引用属性表，对象销毁时自动清理。
    def _values(self) -> weakref.WeakKeyDictionary:
        values = getattr(self._local, "values", None)
        if not isinstance(values, weakref.WeakKeyDictionary):
            values = weakref.WeakKeyDictionary()
            self._local.values = values
        return values

    # LLM: Raising AttributeError preserves hasattr/getattr(default) behavior used by runtime scopes.
    # 函数用途: 读取当前线程中该 agent 的临时值；未设置时表现得像普通属性不存在。
    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        values = self._values()
        if instance not in values:
            raise AttributeError(self.name)
        return values[instance]

    # LLM: Values are keyed by agent identity because one worker may nest calls across scoped agents.
    # 函数用途: 只给当前线程的当前 agent 写临时运行值。
    def __set__(self, instance, value) -> None:
        self._values()[instance] = value

    # LLM: Scope cleanup must not affect another worker currently using the same SimpleAgent.
    # 函数用途: 删除当前线程的值，用于 run/tool scope 的 finally 恢复。
    def __delete__(self, instance) -> None:
        values = self._values()
        if instance not in values:
            raise AttributeError(self.name)
        values.pop(instance, None)


# LLM: Prefer weak identity storage; reads do not create empty entries.
# 函数用途: 读取或创建当前线程里该代理的 runner 上下文。
def _agent_context(agent, *, create: bool = False) -> dict[str, object]:
    weak_contexts = getattr(_LOCAL, "weak_contexts", None)
    if not isinstance(weak_contexts, weakref.WeakKeyDictionary):
        weak_contexts = weakref.WeakKeyDictionary()
        _LOCAL.weak_contexts = weak_contexts
    try:
        context = weak_contexts.get(agent)
    except TypeError:
        return _fallback_agent_context(agent, create=create)
    if isinstance(context, dict):
        return context
    if not create:
        return {}
    context = {}
    weak_contexts[agent] = context
    return context


# LLM: Non-weakrefable adapters use a strong identity row that restore must remove, preventing id reuse.
# 函数用途: 兼容 SimpleNamespace 等不能弱引用的测试或适配对象。
def _fallback_agent_context(agent, *, create: bool) -> dict[str, object]:
    """Support non-weakrefable test/adaptor objects without allowing object-id reuse."""
    contexts = getattr(_LOCAL, "fallback_contexts", None)
    if not isinstance(contexts, dict):
        contexts = {}
        _LOCAL.fallback_contexts = contexts
    key = id(agent)
    row = contexts.get(key)
    if isinstance(row, tuple) and len(row) == 2 and row[0] is agent and isinstance(row[1], dict):
        return row[1]
    if not create:
        return {}
    context: dict[str, object] = {}
    # Keep a strong identity reference until restore clears the scoped context.
    contexts[key] = (agent, context)
    return context


# LLM: Snapshot-before-set enables nested runner scopes without mutating another worker's context.
# 函数用途: 在当前线程进入一个 runner 上下文，并返回可恢复的旧快照。
def set_current_subagent_context(
    agent,
    *,
    run_id: str,
    attempt_id: str = "",
    task_attributes: dict | None = None,
) -> dict[str, object]:
    """Set the active runner context for the current thread only."""
    context = _agent_context(agent, create=True)
    previous = dict(context)
    context["run_id"] = str(run_id or "").strip()
    context["attempt_id"] = str(attempt_id or "").strip()
    context["task_attributes"] = task_attributes
    return previous


# LLM: Restore removes empty weak/fallback rows so neither memory nor object-id state survives the scope.
# 函数用途: 退出 runner scope 时恢复旧值，无旧值则完整清理身份记录。
def restore_current_subagent_context(agent, previous: dict[str, object]) -> None:
    """Restore a thread-local runner context snapshot."""
    weak_contexts = getattr(_LOCAL, "weak_contexts", None)
    try:
        if isinstance(weak_contexts, weakref.WeakKeyDictionary):
            if previous:
                weak_contexts[agent] = dict(previous)
            else:
                weak_contexts.pop(agent, None)
            return
    except TypeError:
        pass
    contexts = getattr(_LOCAL, "fallback_contexts", None)
    if not isinstance(contexts, dict):
        return
    if previous:
        contexts[id(agent)] = (agent, dict(previous))
    else:
        contexts.pop(id(agent), None)


# LLM: Prefer the scoped runner value; legacy attributes are read-only compatibility for direct adapters.
# 函数用途: 读取当前线程的子代理 run id。
def current_subagent_run_id(agent) -> str:
    context = _agent_context(agent)
    value = context.get("run_id")
    if value is not None:
        return _text_run_id(value)
    return _text_run_id(getattr(agent, "_current_subagent_run_id", ""))


# LLM: Attempt identity follows the same thread-local precedence as the run identity.
# 函数用途: 读取当前线程的 runner attempt id。
def current_subagent_attempt_id(agent) -> str:
    context = _agent_context(agent)
    value = context.get("attempt_id")
    if value is not None:
        return _text_run_id(value)
    return _text_run_id(getattr(agent, "_current_subagent_attempt_id", ""))


# LLM: Structured task attributes are scoped facts; never infer them from the current prompt.
# 函数用途: 读取当前 runner 绑定的结构化任务属性。
def current_task_attributes(agent) -> dict | None:
    context = _agent_context(agent)
    if "task_attributes" in context:
        value = context.get("task_attributes")
        return value if isinstance(value, dict) else None
    value = getattr(agent, "_current_task_attributes", None)
    return value if isinstance(value, dict) else None


# LLM: This is the single in-process seam for the active canonical task root.
# A typed subagent runner context outranks the legacy transient main-turn field.
# 函数用途: 返回当前主代理或任意层级子代理真正绑定的任务目录。
def current_task_root(agent) -> str:
    attrs = current_task_attributes(agent)
    workspace = attrs.get("run_workspace") if isinstance(attrs, dict) else None
    runner_root = workspace.get("task_root") if isinstance(workspace, dict) else ""
    if isinstance(runner_root, str | Path) and str(runner_root).strip():
        return str(runner_root).strip()
    raw = getattr(agent, "_current_run_task_workspace", "")
    return str(raw).strip() if isinstance(raw, str | Path) else ""


# LLM: Runtime IDs accept only explicit strings so arbitrary objects cannot become authority keys.
# 函数用途: 把 runner 身份字段安全收紧为去空白字符串。
def _text_run_id(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


__all__ = [
    "ThreadLocalAgentAttribute",
    "current_subagent_attempt_id",
    "current_subagent_run_id",
    "current_task_attributes",
    "restore_current_subagent_context",
    "set_current_subagent_context",
]
