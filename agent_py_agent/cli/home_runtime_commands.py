
from __future__ import annotations

import argparse
import json
from typing import Any

from ..agent.memory_api import DAILY_ACTORS, DAILY_EVENT_TYPES
from ..agent.user_space.home_index_rebuild import rebuild_home_indexes
from ..agent.user_space.home_retention import apply_owner_retention, plan_owner_retention
from ..agent.user_space.home_runtime_query import (
    DailyMemoryQuery,
    TaskWorkspaceQuery,
    home_runtime_status,
    list_task_workspaces,
    read_daily_memory_records_report,
)
from .common import make_agent


# LLM: Home runtime commands expose only canonical owner paths; Daily filters follow v2 actor/
# event_type fields and never revive the legacy role/kind mirror schema.
# 函数用途: 注册 owner home 状态、保留期、索引、Daily v2 与任务工作区管理员命令。
def add_home_runtime_subcommands(sub: argparse._SubParsersAction) -> None:
    home_status = sub.add_parser("home-status", help="查看 my-agent 家目录入口文件和关键目录")
    home_status.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    home_status.set_defaults(func=cmd_home_status)

    home_retention = sub.add_parser("home-retention", help="预览或执行当前 owner home 的 retention 清理")
    home_retention.add_argument("--apply", action="store_true", help="实际删除过期文件；不传时只预览")
    home_retention.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    home_retention.set_defaults(func=cmd_home_retention)

    home_index_rebuild = sub.add_parser("home-index-rebuild", help="预览或重建 owner/task/run/agent 全局索引")
    home_index_rebuild.add_argument(
        "--apply",
        action="store_true",
        help="用当前权威状态原子替换并压紧索引；不传时只预览",
    )
    home_index_rebuild.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    home_index_rebuild.set_defaults(func=cmd_home_index_rebuild)

    memory_daily_list = sub.add_parser("memory-daily-list", help="列出 owner 的 Daily v2 经历摘要")
    memory_daily_list.add_argument("query", nargs="?", default="", help="搜索关键词；为空时列出匹配日期的记录")
    memory_daily_list.add_argument("--date", help="只查看某一天，格式 YYYY-MM-DD")
    memory_daily_list.add_argument(
        "--actor",
        choices=sorted(DAILY_ACTORS),
        default="",
        help="按经历主体过滤：user/main_agent/subagent/tool/system",
    )
    memory_daily_list.add_argument(
        "--event-type",
        choices=sorted(DAILY_EVENT_TYPES),
        default="",
        help="按经历类型过滤：conversation/decision/task_progress/tool_result/lesson/todo/summary/warning/error",
    )
    memory_daily_list.add_argument("--limit", type=int, default=None, help="最多显示多少条记录；默认读配置")
    memory_daily_list.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    memory_daily_list.set_defaults(func=cmd_memory_daily_list)

    task_workspace_list = sub.add_parser("task-workspace-list", help="列出 home tasks 任务目录")
    task_workspace_list.add_argument("query", nargs="?", default="", help="搜索 task_id/run_id/request_id/任务名")
    task_workspace_list.add_argument("--date", help="只查看某一天，格式 YYYY-MM-DD")
    task_workspace_list.add_argument("--limit", type=int, default=None, help="最多显示多少个任务；默认读配置")
    task_workspace_list.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    task_workspace_list.set_defaults(func=cmd_task_workspace_list)


# LLM: Daily CLI 是只读经历检查器，不得把旧 role/kind 镜像解释成正式记忆或召回来源。
# 函数用途: 查询当前 owner 的 Daily v2 账本并呈现摘要、顺序和引用字段。
def cmd_memory_daily_list(args) -> int:
    agent = make_agent(args)
    request = DailyMemoryQuery(
        query=getattr(args, "query", "") or "",
        date_key=getattr(args, "date", None),
        actor=getattr(args, "actor", "") or "",
        event_type=getattr(args, "event_type", "") or "",
        limit=_limit_from_args(agent, args),
    )
    report = read_daily_memory_records_report(agent.home_paths, request)
    payload = {
        "ok": True,
        "home": str(agent.home_paths.root),
        "query": request.query,
        "date": request.date_key or "",
        "actor": request.actor,
        "event_type": request.event_type,
        "limit": request.limit,
        "records": report.records,
        "load_errors": report.load_errors,
    }
    _print_memory_daily_list(payload, json_output=getattr(args, "json", False))
    return 0


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


def cmd_home_status(args) -> int:
    agent = make_agent(args)
    payload = {"ok": True, "home": home_runtime_status(agent.home_paths)}
    _print_home_status(payload, json_output=getattr(args, "json", False))
    return 0


def cmd_home_retention(args) -> int:
    agent = make_agent(args)
    result = apply_owner_retention(agent.home_paths) if bool(getattr(args, "apply", False)) else plan_owner_retention(agent.home_paths)
    payload = {"ok": True, "home": str(agent.home_paths.owner_home_dir), "retention": result.to_dict()}
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_home_retention(payload)
    return 0


def cmd_home_index_rebuild(args) -> int:
    agent = make_agent(args)
    result = rebuild_home_indexes(agent.home_paths, apply=bool(getattr(args, "apply", False)))
    payload = {"ok": True, "home": str(agent.home_paths.root), "index_rebuild": result.to_dict()}
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_home_index_rebuild(payload)
    return 0


def _limit_from_args(agent, args) -> int:
    value = getattr(args, "limit", None)
    if value is not None:
        return int(value)
    return int(getattr(agent.config, "cli_task_list_limit", 50) or 0)


# LLM: Human output renders only bounded Daily v2 summaries and ordering IDs, never referenced
# message/tool/artifact bodies.
# 函数用途: 输出 Daily v2 查询结果或稳定 JSON。
def _print_memory_daily_list(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("MY-AGENT MEMORY DAILY LIST")
    print(f"home={payload['home']}")
    print(f"date={payload['date'] or '-'} query={payload['query'] or '-'} records={len(payload['records'])}")
    if payload.get("load_errors"):
        print(f"load_errors={len(payload['load_errors'])}")
    for record in payload["records"]:
        print(
            f"- {record.get('date', '-')} #{record.get('sequence', '-')}: "
            f"{record.get('actor', '-')} {record.get('event_type', '-')} :: "
            f"{record.get('summary', '')}"
        )


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
    print("Identity")
    print(json.dumps(home.get("identity", {}), ensure_ascii=False, sort_keys=True))
    print("Counts")
    print(json.dumps(home["counts"], ensure_ascii=False, sort_keys=True))


def _print_home_retention(payload: dict[str, Any]) -> None:
    retention = payload["retention"]
    print("MY-AGENT HOME RETENTION")
    print(f"home={payload['home']} applied={retention['applied']} actions={len(retention['actions'])}")
    for action in retention["actions"]:
        print(f"- {action['status']} {action['category']}: {action['path']} ({action['reason']})")


def _print_home_index_rebuild(payload: dict[str, Any]) -> None:
    rebuild = payload["index_rebuild"]
    print("MY-AGENT HOME INDEX REBUILD")
    print(
        f"home={payload['home']} applied={rebuild['applied']} "
        f"owners={rebuild['owner_count']} tasks={rebuild['task_count']} "
        f"runs={rebuild['run_count']} agents={rebuild['agent_count']}"
    )


__all__ = [
    "add_home_runtime_subcommands",
    "cmd_home_index_rebuild",
    "cmd_home_retention",
    "cmd_home_status",
    "cmd_memory_daily_list",
    "cmd_task_workspace_list",
]
