# LLM: 当前 owner 的显式运维入口；结清只走原 RuntimeRepository，不授予 unknown 副作用恢复许可。
# 模块用途: 提供家目录、保留期和运行欠账的查看与显式清理命令。
from __future__ import annotations

import argparse
import json
import stat
from dataclasses import replace
from typing import Any

from ..agent.conversation.agent_activity import settle_orphaned_capability_requests
from ..agent.gateway_parts.io import read_json_file_report
from ..agent.gateway_parts.owner_retention import gateway_terminal_exists
from ..agent.gateway_parts.paths import gateway_paths
from ..agent.memory_api import DAILY_ACTORS, DAILY_EVENT_TYPES
from ..agent.runtime_errors import DataCorruptionError
from ..agent.settings.defaults import default_config_int
from ..agent.user_space.home_index_rebuild import rebuild_home_indexes
from ..agent.user_space.home_retention import apply_owner_retention, plan_owner_retention
from ..agent.user_space.home_runtime_query import (
    DailyMemoryQuery,
    TaskWorkspaceQuery,
    home_runtime_status,
    list_task_workspaces,
    read_daily_memory_records_report,
)
from .common import int_arg_or_default, make_agent
from .task_commands import CLI_TASK_LIST_COUNT

_TERMINAL_GATEWAY_STATUSES = frozenset({"done", "completed", "interrupted", "stopped", "cancelled", "failed"})


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

    stale_attempts = sub.add_parser("runtime-stale-attempts", help="列出（可选结清）没有进程身份、无法自动判死的悬挂运行轮")
    stale_attempts.add_argument("--settle", action="store_true", help="结清无尝试孤儿、确认历史 unknown 运维欠账并回收死进程锁；不确认未知副作用")
    stale_attempts.add_argument("--older-than-days", type=float, default=1.0, help="只结清开始于多少天前的运行轮；默认 1 天，最小 0")
    stale_attempts.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    stale_attempts.set_defaults(func=cmd_runtime_stale_attempts)


# LLM: Daily CLI 是只读经历检查器，不得把旧 role/kind 镜像解释成正式记忆或召回来源。
# 函数用途: 查询当前 owner 的 Daily v2 账本并呈现摘要、顺序和引用字段。
def cmd_memory_daily_list(args) -> int:
    agent = make_agent(args)
    request = DailyMemoryQuery(
        query=getattr(args, "query", "") or "",
        date_key=getattr(args, "date", None),
        actor=getattr(args, "actor", "") or "",
        event_type=getattr(args, "event_type", "") or "",
        limit=_limit_from_args(args),
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
        limit=_limit_from_args(args),
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


# LLM: 显式apply透传可信Agent配置并沿同一owner持久续点逐片完成；来源错误/法律保留不无限重试，回执保留所有已执行动作。
# 函数用途: 预览保留计划，或用有界扫描完成显式清理并汇总结果。
def cmd_home_retention(args) -> int:
    agent = make_agent(args)
    if bool(getattr(args, "apply", False)):
        actions = []
        while True:
            result = apply_owner_retention(agent.home_paths, config=getattr(agent, "config", None))
            actions.extend(result.actions)
            # scan_partial也包含不可读来源的重试续点；有错时原样交还诊断，不把重试当预算推进。
            if not result.scan_partial or not result.applied or not result.ok:
                break
        result = replace(result, actions=tuple(actions))
    else:
        result = plan_owner_retention(agent.home_paths)
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


# LLM: --settle沿原repo死亡证明/CAS直接结清，无交互确认；安全结清转cancelled/abandoned，不伪造noop或重放旧工具。
# 函数用途: 列出或取消当前owner可安全结清的历史运行，报告解锁结果及仍受保护的记录。
def _canonical_gateway_terminal_reason(paths, root_id: str) -> str | None:
    if (not isinstance(root_id, str) or not root_id or root_id in {".", ".."}
            or any(sep in root_id for sep in ("/", "\\", "\x00"))):
        return None
    name = f"{root_id}.json"
    terminal_file = paths.terminal / name
    try:
        info = terminal_file.lstat()
    except FileNotFoundError:
        try:
            exists = gateway_terminal_exists(paths, name)
        except (DataCorruptionError, OSError, ValueError):
            return None
        return "parent_turn_ended" if exists else None
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    report = read_json_file_report(terminal_file, context="cli.runtime_stale_attempts.terminal")
    row = next((v for k, v in vars(report).items() if k not in {"load_error", "lock_busy"}), None) if report.load_error is None else None
    if (
        not isinstance(row, dict)
        or row.get("schema_version") != "gateway_terminal_request.v1"
        or row.get("id") != root_id
    ):
        return None
    status = str(row.get("status") or "").strip()
    if status not in _TERMINAL_GATEWAY_STATUSES:
        return None
    return "parent_turn_interrupted" if status in {"interrupted", "stopped"} else "parent_turn_ended"


# LLM: 仅向conversation桥接层传入本owner的Gateway终态核验回调，不直接耦合subagents子模块。
# 函数用途: 结清错失父级关闭窗口的孤立能力申请，并显式报告仍未证清的记录。
def _settle_orphaned_capability_requests(agent) -> tuple[list[str], list[dict[str, str]]]:
    subagents = getattr(agent, "subagents", None)
    workspace = getattr(subagents, "workspace", None)
    root = getattr(agent, "root", None)
    gateway_workspace = getattr(getattr(agent, "config", None), "gateway_workspace", None)
    if subagents is None or workspace is None or root is None or not gateway_workspace:
        return [], []
    default_budget = default_config_int("retention_scan_slice_entries")
    configured = getattr(getattr(agent, "config", None), "retention_scan_slice_entries", default_budget)
    page_budget = configured if type(configured) is int and configured > 0 else default_budget
    paths = gateway_paths(agent)
    return settle_orphaned_capability_requests(
        agent,
        canonical_terminal_reason=lambda root_id: _canonical_gateway_terminal_reason(paths, root_id),
        page_budget=page_budget,
    )


def cmd_runtime_stale_attempts(args) -> int:
    agent = make_agent(args)
    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    if repo is None or not hasattr(repo, "unidentified_stale_attempts"):
        print(json.dumps({"ok": False, "error": "当前 home 没有权威运行库，无法列出悬挂运行轮。"}, ensure_ascii=False))
        return 1
    settled: list[str] = []
    released_locks: list[str] = []
    settled_capabilities: list[str] = []
    unproven_capabilities: list[dict[str, str]] = []
    if bool(getattr(args, "settle", False)):
        days = max(0.0, float(getattr(args, "older_than_days", 1.0) or 0.0))
        repo.recover_stale_attempts()
        # 显式运维从头完成一轮，不能漏掉此实例此前已扫过但仍留存的前缀。
        repo._stale_lock_cursor = (0.0, "")
        while True:
            released_locks.extend(repo.cleanup_stale_resource_locks())
            # 返回的是删除列表，不是扫描条数；整页受保护时也要等原游标回绕。
            if repo._stale_lock_cursor == (0.0, ""):
                break
        settled = repo.settle_unidentified_attempts(older_than_seconds=days * 86400.0)
        settled_capabilities, unproven_capabilities = _settle_orphaned_capability_requests(agent)
    remaining = repo.unidentified_stale_attempts()
    result = {
        "ok": True,
        "settled_agent_run_ids": settled,
        "released_resource_lock_ids": released_locks,
        "settled_capability_run_ids": settled_capabilities,
        "unproven_capability_requests": unproven_capabilities,
        "remaining": remaining,
    }
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print("MY-AGENT RUNTIME STALE ATTEMPTS")
    print(
        f"settled={len(settled)} remaining={len(remaining)} "
        f"settled_capabilities={len(settled_capabilities)} unproven_capabilities={len(unproven_capabilities)}"
    )
    for item in remaining:
        print(f"- {item['run_id']} role={item['role']} attempt_status={item['attempt_status']} started_at={item['started_at']:.0f}")
    for item in unproven_capabilities:
        print(
            f"- capability {item['run_id'] or '-'} root={item['root_id'] or '-'} "
            f"parent={item['parent_id'] or '-'} reason={item['reason_code']}"
        )
    if remaining and not settled:
        print("剩余记录可能仍活跃、身份不可核验或指针损坏；可安全结清的运行会取消并解除 unknown 阻塞。")
    return 0


# 函数用途: 取 --limit，没给时用任务列表的代码默认条数。
def _limit_from_args(args) -> int:
    return int_arg_or_default(args, "limit", CLI_TASK_LIST_COUNT)


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
