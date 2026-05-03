from __future__ import annotations

"""LLM: CLI commands for task lifecycle management.

给人看的解释：
这里提供 `my-agent task list/show/abandon/pause/resume/search` 命令，
让用户能控制任务生命周期（ABANDONED/PAUSED/RESUMED）。
"""

import time

from ..agent.task_registry import get_task_summary, format_task_list, TaskRegistry
from .common import resolve_workspace_root, ROOT
from ..agent.config import load_config
from ..agent.local_store import LocalStore
from ..agent.subagents.models import TaskStatus


def cmd_task_list(args) -> int:
    """列出任务列表。"""

    config = load_config(args.config)
    root = resolve_workspace_root(config, args.config)

    store = LocalStore(root / "data" / "local_store.db")

    tasks = store.task_registry.query_tasks(
        user_id=args.user_id,
        status=args.status,
        limit=args.limit,
    )

    result = format_task_list(tasks)
    print(result)
    return 0


def cmd_task_show(args) -> int:
    """显示任务详情。"""

    config = load_config(args.config)
    root = resolve_workspace_root(config, args.config)

    store = LocalStore(root / "data" / "local_store.db")
    summary = get_task_summary(store, args.task_id)

    print(summary)
    return 0


def cmd_task_abandon(args) -> int:
    """标记任务为 ABANDONED。"""

    config = load_config(args.config)
    root = resolve_workspace_root(config, args.config)

    store = LocalStore(root / "data" / "local_store.db")

    task_info = store.task_registry.lookup_task(args.task_id)
    if not task_info:
        print(f"任务 {args.task_id} 不存在。")
        return 1

    current_status = task_info["status"]
    if current_status == TaskStatus.ABANDONED.value:
        print(f"任务 {args.task_id} 已经是 ABANDONED 状态。")
        return 0

    ok = store.task_registry.update_task_status(args.task_id, TaskStatus.ABANDONED.value)
    if ok:
        print(f"任务 {args.task_id} 已标记为 ABANDONED，Dispatch 不会再调度。")
        return 0
    else:
        print(f"任务 {args.task_id} 更新失败。")
        return 1


def cmd_task_pause(args) -> int:
    """标记任务为 PAUSED。"""

    config = load_config(args.config)
    root = resolve_workspace_root(config, args.config)

    store = LocalStore(root / "data" / "local_store.db")

    task_info = store.task_registry.lookup_task(args.task_id)
    if not task_info:
        print(f"任务 {args.task_id} 不存在。")
        return 1

    current_status = task_info["status"]
    if current_status == TaskStatus.PAUSED.value:
        print(f"任务 {args.task_id} 已经是 PAUSED 状态。")
        return 0

    ok = store.task_registry.update_task_status(args.task_id, TaskStatus.PAUSED.value)
    if ok:
        print(f"任务 {args.task_id} 已暂停，可用 task resume 恢复。")
        return 0
    else:
        print(f"任务 {args.task_id} 更新失败。")
        return 1


def cmd_task_resume(args) -> int:
    """将 PAUSED 任务恢复为 RUNNING。"""

    config = load_config(args.config)
    root = resolve_workspace_root(config, args.config)

    store = LocalStore(root / "data" / "local_store.db")

    task_info = store.task_registry.lookup_task(args.task_id)
    if not task_info:
        print(f"任务 {args.task_id} 不存在。")
        return 1

    current_status = task_info["status"]
    if current_status != TaskStatus.PAUSED.value:
        print(f"任务 {args.task_id} 当前状态是 {current_status}，只有 PAUSED 状态才能 resume。")
        return 1

    ok = store.task_registry.update_task_status(args.task_id, TaskStatus.RUNNING.value)
    if ok:
        print(f"任务 {args.task_id} 已恢复为 RUNNING。")
        return 0
    else:
        print(f"任务 {args.task_id} 更新失败。")
        return 1


def cmd_task_search(args) -> int:
    """LLM 模糊搜索匹配的任务。"""

    config = load_config(args.config)
    root = resolve_workspace_root(config, args.config)

    store = LocalStore(root / "data" / "local_store.db")

    # 获取所有任务，用 LLM 搜索（简单实现：关键词匹配）
    all_tasks = store.task_registry.query_tasks(limit=200)
    query = args.query.lower()

    matched = []
    for task in all_tasks:
        goal = task.get("goal", "").lower()
        task_id = task.get("task_id", "").lower()
        status = task.get("status", "").lower()
        # 简单模糊匹配：query 是 goal 的子串，或 query 长度 >= 3 时 goal 包含 query
        if query in goal or query in task_id:
            matched.append(task)
        elif len(query) >= 3 and any(word in goal for word in query.split()):
            matched.append(task)

    if not matched:
        print(f"没有找到匹配 \"{args.query}\" 的任务。")
        return 0

    result = format_task_list(matched)
    print(result)
    return 0