# LLM: 记忆体检只读配置、路由索引与归档目录，不写任何记忆文件。
# 模块用途: 记忆体检命令的配置、路由与归档部分的辅助函数。
from __future__ import annotations

"""implements the memory-doctor CLI command and its supporting helpers.

新手说明:
这里放 memory doctor 体检命令入口和它专用的 helper 函数。
doctor 会告诉你配置最终生效成什么、配置有没有默认值 warning、
默认或指定路由索引能不能读，以及 hook/raw 目录里现在有没有归档文件。
route 相关的功能在 memory_commands.py。
"""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..agent.memory_archive import raw_event_path_for, snapshot_path_for
from ..agent.memory_routing import (
    MemoryRoute,
    load_routes,
    validate_routes,
)
from ..agent.user_space.home_doctor import build_home_doctor_report
from ..agent.user_space.home_layout import DEFAULT_ROUTE_INDEX, resolve_route_index_target
from ..agent.user_space.home_runtime_query import home_runtime_status
from .common import make_agent
from .memory_commands.memory_doctor_cmd import MEMORY_DOCTOR_RECENT_ARCHIVE_FILE_COUNT


def cmd_memory_doctor(args) -> int:

    agent = make_agent(args)
    index_target = resolve_route_index_target(agent.root, args.index, home_paths=agent.home_paths)
    index_path = index_target.path
    warnings = _config_warnings(agent.config)
    routing = _build_routing_doctor(index_target.authority_root, index_path)
    archive = _build_archive_doctor(agent.root, agent.config)
    home_doctor = build_home_doctor_report(agent.home_paths)
    payload = {
        "ok": routing["load_error"] == "",
        "workspace_root": str(agent.root),
        "home": home_runtime_status(agent.home_paths),
        "home_doctor": home_doctor,
        "config": _memory_config_payload(agent.config),
        "warnings": warnings,
        "routing": routing,
        "archive": archive,
    }
    _print_memory_doctor_report(payload, json_output=args.json)
    return 0


def _resolve_index_path(root: Path, raw_index: str | None, *, home_paths: object | None = None) -> Path:

    return resolve_route_index_target(root, raw_index, home_paths=home_paths).path


def _build_routing_doctor(root: Path, index_path: Path) -> dict[str, Any]:

    payload: dict[str, Any] = {
        "index": _index_payload(index_path),
        "routes": [],
        "route_count": 0,
        "validation_warnings": [],
        "load_error": "",
    }
    if not index_path.exists():
        payload["load_error"] = f"memory route index not found: {index_path}"
        return payload
    try:
        routes = load_routes(index_path)
    except Exception as exc:
        payload["load_error"] = f"{type(exc).__name__}: {exc}"
        return payload
    payload["routes"] = [_route_payload(route) for route in routes]
    payload["route_count"] = len(routes)
    payload["validation_warnings"] = validate_routes(routes, root)
    return payload


# LLM: 只读归档目录与配置，汇总保留策略与 hook/raw 目录状态；归档级别只有 memory_archive_level 一个。
# 函数用途: 给记忆体检生成归档部分的结果。
def _build_archive_doctor(root: Path, config: object) -> dict[str, Any]:

    hook_today_path = snapshot_path_for(root)
    raw_today_path = raw_event_path_for(root)
    return {
        # 参数减量第 1 批（2026-09-27）：memory_hook_retention_days 没有清理器读取，已从配置删除，不再回显。
        "retention": {
            "memory_hook_enabled": bool(getattr(config, "memory_hook_enabled", True)),
            "memory_archive_level": int(getattr(config, "memory_archive_level", 3)),
        },
        "hook": _archive_dir_payload(hook_today_path.parent, hook_today_path, config=config),
        "raw": _archive_dir_payload(raw_today_path.parent, raw_today_path, config=config),
    }


def _archive_dir_payload(directory: Path, today_path: Path, *, config: object | None = None) -> dict[str, Any]:

    files = sorted(
        [path for path in directory.glob("*.jsonl") if path.is_file()] if directory.exists() else [],
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    return {
        "dir": str(directory),
        "exists": directory.exists(),
        "file_count": len(files),
        "today_path": str(today_path),
        "recent_files": [
            _archive_file_payload(path)
            for path in files[:MEMORY_DOCTOR_RECENT_ARCHIVE_FILE_COUNT]
        ],
    }


def _archive_file_payload(path: Path) -> dict[str, Any]:

    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": stat.st_mtime,
    }


# LLM: 只回显 AgentConfig 上真实存在的 Memory 字段；字段名单与 settings/_memory_types.MemorySettings 同步删减；只读配置对象。
# 函数用途: 把 doctor 展示的记忆配置生效值收成一个字典。
def _memory_config_payload(config: object) -> dict[str, Any]:

    fields = [
        "memory_archive_level",
        "memory_hook_enabled",
        "memory_rule_routing_mode",
        "memory_rule_auto_read_limit",
    ]
    return {field: getattr(config, field) for field in fields}


def _normalize_warning_item(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "to_dict"):
        return item.to_dict()
    if hasattr(item, "__dataclass_fields__"):
        return asdict(item)
    return {"message": str(item)}


def _config_warnings(config: object) -> list[dict[str, Any]]:
    warnings = getattr(config, "memory_config_warnings", []) or []
    return [_normalize_warning_item(item) for item in warnings]


def _index_payload(index_path: Path) -> dict[str, Any]:

    return {
        "path": str(index_path),
        "exists": index_path.exists(),
        "is_file": index_path.is_file(),
    }


def _route_payload(route: MemoryRoute) -> dict[str, Any]:

    return {
        "route_id": route.route_id,
        "topic": route.topic,
        "trigger_keywords": route.trigger_keywords,
        "related_terms": route.related_terms,
        "when_to_read": route.when_to_read,
        "authority_path": route.authority_path,
        "scope": route.scope,
        "priority": route.priority,
        "stale_check": route.stale_check,
        "last_verified_at": route.last_verified_at,
        "source_path": route.source_path,
    }


def _print_memory_doctor_report(payload: dict[str, Any], *, json_output: bool) -> None:

    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return

    print("MY-AGENT MEMORY DOCTOR")
    print(f"ok={payload['ok']}")
    print(f"workspace={payload['workspace_root']}")
    _print_home_report(payload["home"])
    _print_home_doctor_summary(payload["home_doctor"])
    print("Config")
    for key, value in payload["config"].items():
        print(f"- {key}={value}")
    print(f"warnings={len(payload['warnings'])}")
    for warning in payload["warnings"]:
        field_name = warning.get("field_name", "warning")
        reason = warning.get("reason", warning.get("message", ""))
        default = warning.get("default_value", "-")
        print(f"- {field_name}: {reason} default={default}")
    routing = payload["routing"]
    print("Routing")
    print(f"- index={routing['index']['path']} exists={routing['index']['exists']}")
    print(f"- routes={routing['route_count']} validation_warnings={len(routing['validation_warnings'])}")
    if routing["load_error"]:
        print(f"- load_error={routing['load_error']}")
    for item in routing["validation_warnings"]:
        print(f"- warning: {item}")
    archive = payload["archive"]
    print("Archive")
    print(
        "- retention="
        + json.dumps(archive["retention"], ensure_ascii=False, sort_keys=True)
    )
    for label in ("hook", "raw"):
        state = archive[label]
        recent = state["recent_files"][0]["name"] if state["recent_files"] else "-"
        print(
            f"- {label}: dir={state['dir']} exists={state['exists']} "
            f"files={state['file_count']} recent={recent}"
        )


def _print_home_report(home: dict[str, Any]) -> None:
    print("Home")
    print(f"- root={home['root']}")
    print(f"- daily_files={home['counts']['daily_files']} task_workspaces={home['counts']['task_workspaces']}")
    for name, state in home["directories"].items():
        print(f"- {name}: exists={state['exists']}")


def _print_home_doctor_summary(report: dict[str, Any]) -> None:
    print("Home Doctor")
    print(
        f"- dangling_refs={report['indexes']['dangling_count']} "
        f"retention_candidates={report['retention']['planned_count']}"
    )
