# LLM: Home runtime CLI commands expose ~/.my-agent daily memory and task workspaces without writing data.
# 模块用途: 提供 memory-daily-list、task-workspace-list、home-status 这类只读调试入口。

from __future__ import annotations

import json
from typing import Any

from ..agent.user_space.home_runtime_query import (
    DailyMemoryQuery,
    TaskWorkspaceQuery,
    home_runtime_status,
    list_task_workspaces,
    read_daily_memory_records,
)
from .common import make_agent


# LLM: cmd_memory_daily_list is a read-only CLI bridge from argparse to DailyMemoryQuery.
# 函数用途: 按日期、角色、类型和关键词列出 home daily memory 记录。
def cmd_memory_daily_list(args) -> int:
    agent = make_agent(args)
    request = DailyMemoryQuery(
        query=getattr(args, "query", "") or "",
        date_key=getattr(args, "date", None),
        role=getattr(args, "role", "") or "",
        kind=getattr(args, "kind", "") or "",
        limit=_limit_from_args(agent, args),
    )
    payload = {
        "ok": True,
        "home": str(agent.home_paths.root),
        "query": request.query,
        "date": request.date_key or "",
        "role": request.role,
        "kind": request.kind,
        "limit": request.limit,
        "records": read_daily_memory_records(agent.home_paths, request),
    }
    _print_memory_daily_list(payload, json_output=getattr(args, "json", False))
    return 0


# LLM: cmd_task_workspace_list is the read-only CLI bridge for home task workspace discovery.
# 函数用途: 列出 ~/.my-agent/workspace/tasks 下的任务工作区，帮助恢复、调试和前端展示。
def cmd_task_workspace_list(args) -> int:
    agent = make_agent(args)
    request = TaskWorkspaceQuery(
        query=getattr(args, "query", "") or "",
        date_key=getattr(args, "date", None),
        limit=_limit_from_args(agent, args),
    )
    payload = {
        "ok": True,
        "home": str(agent.home_paths.root),
        "query": request.query,
        "date": request.date_key or "",
        "limit": request.limit,
        "tasks": list_task_workspaces(agent.home_paths, request),
    }
    _print_task_workspace_list(payload, json_output=getattr(args, "json", False))
    return 0


# LLM: cmd_home_status surfaces the same payload doctor embeds, without route/archive checks.
# 函数用途: 查看 my-agent 家目录入口文件、关键目录和轻量计数状态。
def cmd_home_status(args) -> int:
    agent = make_agent(args)
    payload = {"ok": True, "home": home_runtime_status(agent.home_paths)}
    _print_home_status(payload, json_output=getattr(args, "json", False))
    return 0


# LLM: _limit_from_args keeps home runtime CLI defaults tied to AgentConfig.
# 函数用途: 读取命令行 limit；未传时使用 cli_task_list_limit 配置。
def _limit_from_args(agent, args) -> int:
    value = getattr(args, "limit", None)
    if value is not None:
        return int(value)
    return int(getattr(agent.config, "cli_task_list_limit", 50) or 0)


# LLM: _print_memory_daily_list keeps JSON stable and text output compact.
# 函数用途: 输出 daily memory 查询结果，支持机器可读 JSON 和人工文本。
def _print_memory_daily_list(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("MY-AGENT MEMORY DAILY LIST")
    print(f"home={payload['home']}")
    print(f"date={payload['date'] or '-'} query={payload['query'] or '-'} records={len(payload['records'])}")
    for record in payload["records"]:
        print(f"- {record.get('date', '-')}: {record.get('role', '-')} {record.get('kind', '-')} :: {record.get('content', '')}")


# LLM: _print_task_workspace_list keeps task workspace output path-oriented and refs-only.
# 函数用途: 输出 task workspace 摘要，不展开产物正文。
def _print_task_workspace_list(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("MY-AGENT TASK WORKSPACE LIST")
    print(f"home={payload['home']}")
    print(f"date={payload['date'] or '-'} query={payload['query'] or '-'} tasks={len(payload['tasks'])}")
    for task in payload["tasks"]:
        state = task.get("state", {}) if isinstance(task.get("state"), dict) else {}
        print(f"- {state.get('task_id') or task['slug']} run={state.get('run_id') or '-'} dir={task['root']}")


# LLM: _print_home_status mirrors memory doctor home payload for quick human checks.
# 函数用途: 输出 home runtime 状态，JSON 用于前端，文本用于终端快速查看。
def _print_home_status(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    home = payload["home"]
    print("MY-AGENT HOME STATUS")
    print(f"root={home['root']}")
    print("Entry Files")
    for name, state in home["entry_files"].items():
        print(f"- {name}: exists={state['exists']} path={state['path']}")
    print("Directories")
    for name, state in home["directories"].items():
        print(f"- {name}: exists={state['exists']} path={state['path']}")
    print("Counts")
    print(json.dumps(home["counts"], ensure_ascii=False, sort_keys=True))


__all__ = ["cmd_home_status", "cmd_memory_daily_list", "cmd_task_workspace_list"]
