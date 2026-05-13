# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""implements the memory-doctor CLI command and its supporting helpers.

新手说明:
这里放 memory doctor 体检命令入口和它专用的 helper 函数。
doctor 会告诉你配置最终生效成什么、配置有没有回退 warning、
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
from .common import make_agent

DEFAULT_ROUTE_INDEX = Path("memory") / "routing" / "INDEX.md"
RECENT_ARCHIVE_FILE_LIMIT = 5


# LLM: cmd_memory_doctor 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_memory_doctor(args) -> int:

    agent = make_agent(args)
    index_path = _resolve_index_path(agent.root, args.index)
    warnings = _config_warnings(agent.config)
    routing = _build_routing_doctor(agent.root, index_path)
    archive = _build_archive_doctor(agent.root, agent.config)
    payload = {
        "ok": routing["load_error"] == "",
        "workspace_root": str(agent.root),
        "config": _memory_config_payload(agent.config),
        "warnings": warnings,
        "routing": routing,
        "archive": archive,
    }
    _print_memory_doctor_report(payload, json_output=args.json)
    return 0


# LLM: _resolve_index_path 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 解析路径、模式或配置默认值，返回后续流程使用的稳定值。
def _resolve_index_path(root: Path, raw_index: str | None) -> Path:

    candidate = Path(raw_index).expanduser() if raw_index else DEFAULT_ROUTE_INDEX
    if candidate.is_absolute():
        return candidate.resolve()
    return (root / candidate).resolve()


# LLM: _build_routing_doctor 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 构造下游调用需要的参数包、状态对象或命令对象。
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


# LLM: _build_archive_doctor 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 构造下游调用需要的参数包、状态对象或命令对象。
def _build_archive_doctor(root: Path, config: object) -> dict[str, Any]:

    hook_today_path = snapshot_path_for(root)
    raw_today_path = raw_event_path_for(root)
    return {
        "retention": {
            "memory_hook_retention_days": int(getattr(config, "memory_hook_retention_days", 7)),
            "memory_hook_enabled": bool(getattr(config, "memory_hook_enabled", True)),
            "memory_hook_archive_level": int(getattr(config, "memory_hook_archive_level", 3)),
            "memory_archive_level": int(getattr(config, "memory_archive_level", 3)),
        },
        "hook": _archive_dir_payload(hook_today_path.parent, hook_today_path, config=config),
        "raw": _archive_dir_payload(raw_today_path.parent, raw_today_path, config=config),
    }


# LLM: _archive_dir_payload 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
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
            for path in files[: _recent_archive_file_limit(config)]
        ],
    }


# LLM: _recent_archive_file_limit preserves old helper calls while making doctor limits config-backed.
# 函数用途: 从配置读取 memory doctor 最近文件数量上限；没有配置时回退 5。
def _recent_archive_file_limit(config: object | None) -> int:
    return int(getattr(config, "memory_doctor_recent_archive_file_limit", 5) or 0)


# LLM: _archive_file_payload 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _archive_file_payload(path: Path) -> dict[str, Any]:

    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": stat.st_mtime,
    }


# LLM: _memory_config_payload 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _memory_config_payload(config: object) -> dict[str, Any]:

    fields = [
        "memory_archive_level",
        "memory_hook_enabled",
        "memory_hook_archive_level",
        "memory_hook_retention_days",
        "memory_rule_routing_enabled",
        "memory_rule_routing_mode",
        "memory_rule_auto_read_limit",
        "memory_rule_receipt_enabled",
    ]
    return {field: getattr(config, field) for field in fields}


# LLM: _normalize_warning_item 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 解析路径、模式或配置默认值，返回后续流程使用的稳定值。
def _normalize_warning_item(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "to_dict"):
        return item.to_dict()
    if hasattr(item, "__dataclass_fields__"):
        return asdict(item)
    return {"message": str(item)}


# LLM: _config_warnings 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _config_warnings(config: object) -> list[dict[str, Any]]:
    warnings = getattr(config, "memory_config_warnings", []) or []
    return [_normalize_warning_item(item) for item in warnings]


# LLM: _index_payload 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _index_payload(index_path: Path) -> dict[str, Any]:

    return {
        "path": str(index_path),
        "exists": index_path.exists(),
        "is_file": index_path.is_file(),
    }


# LLM: _route_payload 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _route_payload(route: MemoryRoute) -> dict[str, Any]:

    return {
        "route_id": route.route_id,
        "topic": route.topic,
        "trigger_keywords": route.trigger_keywords,
        "aliases": route.aliases,
        "when_to_read": route.when_to_read,
        "authority_path": route.authority_path,
        "scope": route.scope,
        "priority": route.priority,
        "stale_check": route.stale_check,
        "last_verified_at": route.last_verified_at,
        "source_path": route.source_path,
    }


# LLM: _print_memory_doctor_report 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_memory_doctor_report(payload: dict[str, Any], *, json_output: bool) -> None:

    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return

    print("MY-AGENT MEMORY DOCTOR")
    print(f"ok={payload['ok']}")
    print(f"workspace={payload['workspace_root']}")
    print("Config")
    for key, value in payload["config"].items():
        print(f"- {key}={value}")
    print(f"warnings={len(payload['warnings'])}")
    for warning in payload["warnings"]:
        field_name = warning.get("field_name", "warning")
        reason = warning.get("reason", warning.get("message", ""))
        fallback = warning.get("fallback_value", "-")
        print(f"- {field_name}: {reason} fallback={fallback}")
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
