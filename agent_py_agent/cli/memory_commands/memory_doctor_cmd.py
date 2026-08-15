

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ...agent.memory_archive import (
    compression_snapshot_dir,
    raw_event_path_for,
    snapshot_path_for,
)
from ...agent.user_space.home_layout import DEFAULT_ROUTE_INDEX, resolve_route_index_target

RECENT_ARCHIVE_FILE_LIMIT = 5


def cmd_memory_doctor(args) -> int:
    # Access make_agent through the module to allow test patching
    import sys
    memory_mod = sys.modules.get("agent_py_agent.cli.memory_commands")
    if memory_mod is not None:
        agent = memory_mod.make_agent(args)
    else:
        from ..common import make_agent
        agent = make_agent(args)
    index_path = _resolve_index_path(agent.root, args.index, home_paths=getattr(agent, "home_paths", None))
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


def _resolve_index_path(root: Path, raw_index: str | None, *, home_paths: object | None = None) -> Path:
    return resolve_route_index_target(root, raw_index, home_paths=home_paths).path


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


def _build_routing_doctor(root: Path, index_path: Path) -> dict[str, Any]:
    from ...agent.memory_routing import load_routes, validate_routes

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


def _build_archive_doctor(root: Path, config: object) -> dict[str, Any]:
    hook_today_path = snapshot_path_for(root)
    raw_today_path = raw_event_path_for(root)
    snapshot_dir = compression_snapshot_dir(root)
    return {
        "retention": {
            "memory_hook_retention_days": int(getattr(config, "memory_hook_retention_days", 7)),
            "memory_hook_enabled": bool(getattr(config, "memory_hook_enabled", True)),
            "memory_hook_archive_level": int(getattr(config, "memory_hook_archive_level", 3)),
            "memory_archive_level": int(getattr(config, "memory_archive_level", 3)),
        },
        "hook": _archive_dir_payload(hook_today_path.parent, hook_today_path, config=config),
        "raw": _archive_dir_payload(raw_today_path.parent, raw_today_path, config=config),
        "snapshots": _snapshot_dir_payload(snapshot_dir, config=config),
        "consistency_warnings": _archive_consistency_warnings(hook_today_path.parent, snapshot_dir),
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
            for path in files[: _recent_archive_file_limit(config)]
        ],
    }


def _recent_archive_file_limit(config: object | None) -> int:
    return int(getattr(config, "memory_doctor_recent_archive_file_limit", 5) or 0)


def _archive_file_payload(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": stat.st_mtime,
    }


def _snapshot_dir_payload(directory: Path, *, config: object) -> dict[str, Any]:
    files = sorted(
        [path for path in directory.glob("*.json") if path.is_file()] if directory.exists() else [],
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    invalid_json_files: list[str] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            invalid_json_files.append(str(path))
            continue
        if not isinstance(payload, dict):
            invalid_json_files.append(str(path))
    return {
        "dir": str(directory),
        "exists": directory.exists(),
        "file_count": len(files),
        "invalid_json_files": invalid_json_files,
        "recent_files": [
            _archive_file_payload(path)
            for path in files[: int(getattr(config, "memory_doctor_recent_archive_file_limit", 5) or 0)]
        ],
    }


def _archive_consistency_warnings(hook_dir: Path, snapshot_dir: Path) -> list[str]:
    warnings: list[str] = []
    hook_exists = hook_dir.exists() and any(hook_dir.glob("*.jsonl"))
    snapshot_exists = snapshot_dir.exists() and any(snapshot_dir.glob("*.json"))
    if hook_exists and not snapshot_exists:
        warnings.append("hook layer has records but memory_archive/snapshots is empty")
    if snapshot_exists and not hook_exists:
        warnings.append("snapshot JSON files exist but hook JSONL layer is empty")
    return warnings


def _index_payload(index_path: Path) -> dict[str, Any]:
    return {
        "path": str(index_path),
        "exists": index_path.exists(),
        "is_file": index_path.is_file(),
    }


def _route_payload(route) -> dict[str, Any]:
    from ...agent.memory_routing import MemoryRoute
    return {
        "route_id": route.route_id,
        "topic": route.topic,
        "trigger_keywords": route.trigger_keywords,
        "related_terms": route.related_terms,
        "when_to_read": route.when_to_read,
        "authority_path": route.authority_path,
        "source_file": route.source_file,
        "inject_mode": route.inject_mode,
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


__all__ = [
    "cmd_memory_doctor",
]
