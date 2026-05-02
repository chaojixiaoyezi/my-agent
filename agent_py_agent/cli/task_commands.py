from __future__ import annotations

"""LLM: CLI commands for task identity decoupling.

给人看的解释：
这里提供 `my-agent task-lookup` 和 `my-agent task-list` 命令，
让任何终端/会话都能查询全局唯一的 task_id。
"""

import sys

from ..agent.task_registry import get_task_summary, format_task_list
from .common import make_agent, resolve_workspace_root, ROOT
from ..agent.config import load_config
from ..agent.local_store import LocalStore


def cmd_task_lookup(args) -> int:
    """查询单个任务详情。"""

    config = load_config(args.config)
    root = resolve_workspace_root(config, args.config)

    store = LocalStore(root / "data" / "local_store.db")
    summary = get_task_summary(store, args.task_id)

    print(summary)
    return 0


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
