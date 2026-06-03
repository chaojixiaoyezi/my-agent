
from __future__ import annotations

import json
from typing import Any

from ..agent.user_space.home_index_rebuild import rebuild_home_indexes
from ..agent.user_space.home_migration import apply_home_migration, plan_home_migration
from ..agent.user_space.home_retention import apply_owner_retention, plan_owner_retention
from ..agent.user_space.home_runtime_query import (
    DailyMemoryQuery,
    TaskWorkspaceQuery,
    home_runtime_status,
    list_task_workspaces,
    read_daily_memory_records_report,
)
from .common import make_agent


def cmd_memory_daily_list(args) -> int:
    agent = make_agent(args)
    request = DailyMemoryQuery(
        query=getattr(args, "query", "") or "",
        date_key=getattr(args, "date", None),
        role=getattr(args, "role", "") or "",
        kind=getattr(args, "kind", "") or "",
        limit=_limit_from_args(agent, args),
    )
    report = read_daily_memory_records_report(agent.home_paths, request)
    payload = {
        "ok": True,
        "home": str(agent.home_paths.root),
        "query": request.query,
        "date": request.date_key or "",
        "role": request.role,
        "kind": request.kind,
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


def cmd_home_migrate(args) -> int:
    agent = make_agent(args)
    result = apply_home_migration(agent.home_paths) if bool(getattr(args, "apply", False)) else plan_home_migration(agent.home_paths)
    payload = {"ok": True, "home": str(agent.home_paths.root), "migration": result.to_dict()}
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_home_migrate(payload)
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
        print(f"- {record.get('date', '-')}: {record.get('role', '-')} {record.get('kind', '-')} :: {record.get('content', '')}")


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


def _print_home_migrate(payload: dict[str, Any]) -> None:
    migration = payload["migration"]
    print("MY-AGENT HOME MIGRATE")
    print(f"home={payload['home']} applied={migration['applied']} actions={len(migration['actions'])}")
    for action in migration["actions"]:
        print(f"- {action['status']} {action['action']}: {action['source']} -> {action['target']}")


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
    "cmd_home_index_rebuild",
    "cmd_home_migrate",
    "cmd_home_retention",
    "cmd_home_status",
    "cmd_memory_daily_list",
    "cmd_task_workspace_list",
]
