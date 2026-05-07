from __future__ import annotations

"""LLM: CLI commands for task lifecycle management.

给人看的解释：
这里提供 `my-agent task list/show/abandon/pause/resume/search` 命令，
让用户能控制任务生命周期（ABANDONED/PAUSED/RESUMED）。
"""

import time

from ..agent.config import load_config
from ..agent.local_store import LocalStore
from ..agent.subagents.models import TaskStatus
from ..agent.task_registry import TaskRegistry, format_task_list, get_task_summary
from .common import ROOT, resolve_workspace_root
from .models import TaskIdOptions, TaskListOptions, TaskSearchOptions


def _task_store(config_path: str):
    config = load_config(config_path)
    root = resolve_workspace_root(config, config_path)
    return LocalStore(root / "data" / "local_store.db")


def cmd_task_list(args) -> int:

    options = _task_list_options(args)
    store = _task_store(options.config)

    tasks = store.task_registry.query_tasks(
        user_id=options.user_id,
        status=options.status,
        limit=options.limit,
    )

    result = format_task_list(tasks)
    print(result)
    return 0


def cmd_task_show(args) -> int:

    options = _task_id_options(args)
    summary = get_task_summary(_task_store(options.config), options.task_id)

    print(summary)
    return 0


def cmd_task_abandon(args) -> int:

    options = _task_id_options(args)
    store = _task_store(options.config)

    task_info = store.task_registry.lookup_task(options.task_id)
    if not task_info:
        print(f"任务 {options.task_id} 不存在。")
        return 1

    current_status = task_info["status"]
    if current_status == TaskStatus.ABANDONED.value:
        print(f"任务 {options.task_id} 已经是 ABANDONED 状态。")
        return 0

    ok = store.task_registry.update_task_status(options.task_id, TaskStatus.ABANDONED.value)
    if ok:
        print(f"任务 {options.task_id} 已标记为 ABANDONED，Dispatch 不会再调度。")
        return 0
    else:
        print(f"任务 {options.task_id} 更新失败。")
        return 1


def cmd_task_pause(args) -> int:

    options = _task_id_options(args)
    store = _task_store(options.config)

    task_info = store.task_registry.lookup_task(options.task_id)
    if not task_info:
        print(f"任务 {options.task_id} 不存在。")
        return 1

    current_status = task_info["status"]
    if current_status == TaskStatus.PAUSED.value:
        print(f"任务 {options.task_id} 已经是 PAUSED 状态。")
        return 0

    ok = store.task_registry.update_task_status(options.task_id, TaskStatus.PAUSED.value)
    if ok:
        print(f"任务 {options.task_id} 已暂停，可用 task resume 恢复。")
        return 0
    else:
        print(f"任务 {options.task_id} 更新失败。")
        return 1


def cmd_task_resume(args) -> int:

    options = _task_id_options(args)
    store = _task_store(options.config)

    task_info = store.task_registry.lookup_task(options.task_id)
    if not task_info:
        print(f"任务 {options.task_id} 不存在。")
        return 1

    current_status = task_info["status"]
    if current_status != TaskStatus.PAUSED.value:
        print(f"任务 {options.task_id} 当前状态是 {current_status}，只有 PAUSED 状态才能 resume。")
        return 1

    ok = store.task_registry.update_task_status(options.task_id, TaskStatus.RUNNING.value)
    if ok:
        print(f"任务 {options.task_id} 已恢复为 RUNNING。")
        return 0
    else:
        print(f"任务 {options.task_id} 更新失败。")
        return 1


def cmd_task_search(args) -> int:

    options = _task_search_options(args)
    store = _task_store(options.config)

    # 获取所有任务，用 LLM 搜索（简单实现：关键词匹配）
    all_tasks = store.task_registry.query_tasks(limit=200)
    query = options.query.lower()

    matched = []
    for task in all_tasks:
        goal = task.get("goal", "").lower()
        task_id = task.get("task_id", "").lower()
        status = task.get("status", "").lower()
        # 简单模糊匹配：query 是 goal 的子串，或 query 长度 >= 3 时 goal 包含 query
        if query in goal or query in task_id or len(query) >= 3 and any(word in goal for word in query.split()):
            matched.append(task)

    if not matched:
        print(f"没有找到匹配 \"{options.query}\" 的任务。")
        return 0

    result = format_task_list(matched)
    print(result)
    return 0


def _task_list_options(args) -> TaskListOptions:
    # LLM: task commands convert CLI args to tiny bundles before touching the registry.
    return TaskListOptions(
        config=args.config,
        user_id=args.user_id,
        status=args.status,
        limit=int(args.limit or 0),
    )


def _task_id_options(args) -> TaskIdOptions:
    return TaskIdOptions(config=args.config, task_id=args.task_id)


def _task_search_options(args) -> TaskSearchOptions:
    return TaskSearchOptions(config=args.config, query=args.query)
