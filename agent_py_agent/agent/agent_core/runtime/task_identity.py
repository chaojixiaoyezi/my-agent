from __future__ import annotations

"""Resolve durable task identity without confusing it with a request attempt."""

import hashlib

# Gateway-backed ordinary conversations are still the main-agent turn.  The
# ``conversation`` label describes where its transcript is persisted; it must
# not make the runtime ignore an exact durable workspace binding.  Child and
# control-plane scopes remain excluded so inherited parent lineage cannot take
# over their own task identity.
_MAIN_SCOPES = frozenset({"", "default", "conversation"})


def _structured_conversation_task_id(params: object) -> str:
    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict):
        return ""
    return str(attrs.get("conversation_task_id") or "").strip()


def _conversation_task_path_key(agent: object, params: object, task_id: str) -> str:
    """会话任务的稳定账本 key:任务目录路径指纹。

    只在「有 conversation_thread_id + store 能按 task_id 解析出 task_path」时生效;
    任何一环缺失都返回空,调用方回落到 conversation_task_id(首轮 id 恰是账本所在)。
    子代理不受影响:其 scope 不在 main 集合,走 scoped 隔离分支。
    """
    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict):
        return ""
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    if not thread_id or not task_id:
        return ""
    store = getattr(agent, "conversation_store", None)
    loader = getattr(store, "load_task_link", None) if store is not None else None
    if not callable(loader):
        return ""
    try:
        link = loader(task_id)
    except Exception:
        return ""
    task_path = str(getattr(link, "task_path", "") or "").strip()
    if not task_path:
        return ""
    digest = hashlib.sha256(task_path.encode("utf-8")).hexdigest()[:16]
    return f"task-path:{digest}"


def durable_task_id(params: object) -> str:
    """Use the bound durable task only for a main-agent context."""
    scope = str(getattr(params, "context_scope", "") or "default").strip().lower()
    bound = _structured_conversation_task_id(params) if scope in _MAIN_SCOPES else ""
    return bound or str(getattr(params, "task_id", "") or "").strip()


def run_scope_task_id(value: object) -> str:
    """Read the durable task identity from a trusted tool-call run scope."""
    if not isinstance(value, dict):
        return ""
    return str(value.get("root_task_id") or value.get("task_id") or "").strip()


def progress_ledger_id(agent: object, params: object, *, scoped_id: str = "") -> str:
    """Return the shared ledger key for tool writes, seeds and closeout reads.

    Gateway 普通会话的账本 key 按「任务目录路径」寻址,不按 conversation_task_id:
    每个 /ask 请求都会派生新任务身份(req_2 → req_2-continue → req_3 → req_0,真机
    实证 2026-08-07 celery 复刻),而账本立在首轮 id 上;按新 id 读=读错位=0 pending
    =续跑不触发=每轮只干一点就收口。task_path 在线程内稳定(同一 goal 的所有请求
    同目录),按它寻址账本跨请求稳定。
    """
    scope = str(getattr(params, "context_scope", "") or "default").strip().lower()
    if scope in _MAIN_SCOPES:
        bound = _structured_conversation_task_id(params)
        if bound:
            stable = _conversation_task_path_key(agent, params, bound)
            return stable or bound
    if str(getattr(params, "source", "") or "").strip() == "background_main_agent":
        task_id = str(getattr(params, "task_id", "") or "").strip()
        if task_id:
            return task_id
    if scoped_id:
        return str(scoped_id).strip()
    for value in (
        getattr(params, "run_id", ""),
        getattr(agent, "_main_agent_run_id", ""),
        getattr(params, "task_id", ""),
        getattr(agent, "_current_request_id", ""),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


__all__ = ["durable_task_id", "progress_ledger_id", "run_scope_task_id"]
