# LLM: create_subagents idempotency keeps repeated model calls from growing duplicate child trees.
# 模块用途: 根据同父级命名子代理的结构化事实复用已有 run，并生成下一步调度 run ids。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..subagents.services.base import CreateRunParams

_REUSABLE_STATUSES = {"PLANNING", "PENDING", "RUNNING", "DONE", "COMPLETED", "BLOCKED", "PAUSED"}
_DISPATCHABLE_STATUSES = {"PLANNING", "PENDING"}
_GENERIC_AGENT_NAMES = {"", "general", "worker", "subagent", "agent", "小傻妞"}


# LLM: CreateTaskResolution records whether a task was newly created or reused.
# 类用途: 让 create_subagents payload 同时表达 created/reused/dispatch 三种事实，避免父级靠自然语言猜。
@dataclass(frozen=True)
class CreateTaskResolution:
    task: Any
    reused: bool = False


# LLM: resolve_create_run returns an existing same-parent named child when safe.
# 函数用途: 同一 parent/root 下同名、同角色且仍可复用的 child 已存在时直接返回它；否则调用 manager 创建新 run。
def resolve_create_run(manager: Any, params: CreateRunParams) -> CreateTaskResolution:
    existing = find_reusable_named_child(manager, params)
    if existing is not None:
        return CreateTaskResolution(task=existing, reused=True)
    return CreateTaskResolution(task=manager.create_run(params=params), reused=False)


# LLM: find_reusable_named_child makes idempotency a structured state lookup, not a prompt instruction.
# 函数用途: 查询已有同名 sibling run；只复用未失败/未废弃的明确命名子代理，避免无限重复创建。
def find_reusable_named_child(manager: Any, params: CreateRunParams):
    name = _normalized_name(params.agent_name)
    if name in _GENERIC_AGENT_NAMES:
        return None
    for task in reversed(_safe_list_runs(manager)):
        if _same_create_scope(task, params, name):
            return task
    return None


# LLM: created_tasks filters resolution records for schedule envelopes and audits.
# 函数用途: 返回本次真正新建的 run，供 payload.created_run_ids 和 typed envelope 使用。
def created_tasks(resolutions: list[CreateTaskResolution]) -> list[Any]:
    return [item.task for item in resolutions if not item.reused]


# LLM: reused_tasks filters resolution records for parent recovery hints.
# 函数用途: 返回本次复用的已有 run，帮助 root 下一步直接 dispatch 而不是再 create。
def reused_tasks(resolutions: list[CreateTaskResolution]) -> list[Any]:
    return [item.task for item in resolutions if item.reused]


# LLM: dispatchable_tasks returns only children that should be started now.
# 函数用途: 已 DONE/RUNNING/BLOCKED 的复用 run 仍展示给 root，但不会被建议重复 dispatch。
def dispatchable_tasks(tasks: list[Any]) -> list[Any]:
    return [task for task in tasks if _status(task) in _DISPATCHABLE_STATUSES]


# LLM: _same_create_scope checks parent/root/name/role without comparing fragile natural-language goal text.
# 函数用途: 判断一个已有 task 是否就是这次 create_subagents 想创建的同一位小傻妞。
def _same_create_scope(task: Any, params: CreateRunParams, name: str) -> bool:
    if _normalized_name(getattr(task, "agent_name", "")) != name:
        return False
    if _status(task) not in _REUSABLE_STATUSES:
        return False
    if _text(getattr(task, "parent_id", "")) != _text(params.parent_id):
        return False
    if _requested_root_id(params) and _text(getattr(task, "root_id", "")) != _requested_root_id(params):
        return False
    return _compatible_role(getattr(task, "role", ""), params.role)


# LLM: _requested_root_id treats an omitted root as top-level create scope, not a literal empty root_id.
# 函数用途: 顶层 create_run 会把 root_id 写成自己的 run id；模型没传 root_id 时按 parent/name 去重即可。
def _requested_root_id(params: CreateRunParams) -> str:
    return _text(params.root_id)


# LLM: _compatible_role avoids reusing an explicit checker as a worker with the same display name.
# 函数用途: 两边都有明确 role 且不同就不复用；空值或通用 worker 保持宽容。
def _compatible_role(existing: object, requested: object) -> bool:
    existing_text = _text(existing)
    requested_text = _text(requested)
    if not existing_text or not requested_text:
        return True
    return existing_text == requested_text


# LLM: _safe_list_runs keeps mocked managers and old adapters from crashing create_subagents.
# 函数用途: manager 没有 list_runs 或读取失败时关闭复用逻辑，回到原创建行为。
def _safe_list_runs(manager: Any) -> list[Any]:
    try:
        runs = manager.list_runs()
    except (AttributeError, OSError, TypeError, ValueError):
        return []
    return list(runs or [])


# LLM: _normalized_name keeps Chinese role names stable while trimming model whitespace.
# 函数用途: 规范化 agent_name；不做 aggressive 语义聚类，避免误合并不同子任务。
def _normalized_name(value: object) -> str:
    return _text(value).casefold()


# LLM: _status normalizes task lifecycle values from dataclasses or mocks.
# 函数用途: 读取状态并转成大写字符串，供复用和调度过滤。
def _status(task: Any) -> str:
    return _text(getattr(task, "status", "")).upper()


# LLM: _text guards against MagicMock truthiness and non-string fields.
# 函数用途: 把字段安全转换为去空格字符串。
def _text(value: object) -> str:
    return str(value or "").strip()
