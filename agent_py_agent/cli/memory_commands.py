from __future__ import annotations

"""LLM: implements visible CLI diagnostics for memory routing and archive state.

给人看的解释：
这里放 memory 新骨架的命令行入口：一个用来试跑长期规则路由，一个用来体检 memory 配置、
路由索引和 hook/raw 归档目录。命令只读文件，不调用模型，也不写业务数据。
"""

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from ..agent.memory_archive import raw_event_path_for, snapshot_path_for
from ..agent.memory_routing import (
    MemoryRoute,
    MemoryRouteMatch,
    load_routes,
    match_routes,
    resolve_required_paths,
    validate_routes,
)
from .common import make_agent


DEFAULT_ROUTE_INDEX = Path("memory") / "routing" / "INDEX.md"
RECENT_ARCHIVE_FILE_LIMIT = 5


def cmd_memory_route(args) -> int:
    """LLM: route a query through the configured memory index and print a stable report.

    大白话：这条命令让你不用等 runtime 接完，也能直接问“这句话会命中哪些长期规则文件”。
    索引缺失、配置关闭或索引写坏时，它会把诊断写出来，而不是把 CLI 砸掉。
    """

    agent = make_agent(args)
    mode = _resolve_route_mode(args.mode, agent.config)
    auto_read_limit = _resolve_auto_read_limit(args.auto_read_limit, agent.config)
    index_path = _resolve_index_path(agent.root, args.index)
    warnings = _config_warnings(agent.config)
    diagnostics: dict[str, Any] = {
        "config_warnings": warnings,
        "route_warnings": [],
        "messages": [],
    }
    routes: list[MemoryRoute] = []
    matches: list[MemoryRouteMatch] = []
    required_read_paths: list[str] = []
    candidate_paths: list[str] = []
    ok = True

    if not index_path.exists():
        ok = False
        diagnostics["messages"].append(f"memory route index not found: {index_path}")
    elif mode == "off":
        diagnostics["messages"].append("memory routing mode is off; route matching skipped.")
    else:
        try:
            routes = load_routes(index_path)
            diagnostics["route_warnings"] = validate_routes(routes, agent.root)
            matches = match_routes(args.query, routes, limit=args.limit)
            resolution = resolve_required_paths(
                matches,
                mode=mode,
                auto_read_limit=auto_read_limit,
            )
            required_read_paths = resolution.required_read_paths
            candidate_paths = resolution.candidate_paths
        except Exception as exc:
            ok = False
            diagnostics["messages"].append(f"memory route index could not be loaded: {type(exc).__name__}: {exc}")

    payload = {
        "ok": ok,
        "workspace_root": str(agent.root),
        "query": args.query,
        "mode": mode,
        "limit": args.limit,
        "auto_read_limit": auto_read_limit,
        "routing_enabled": bool(getattr(agent.config, "memory_rule_routing_enabled", True)),
        "index": _index_payload(index_path),
        "warnings": warnings,
        "routes": {
            "count": len(routes),
            "validation_warnings": diagnostics["route_warnings"],
        },
        "matches": [_match_payload(match) for match in matches],
        "required_read_paths": required_read_paths,
        "candidate_paths": candidate_paths,
        "diagnostics": diagnostics,
    }
    _print_memory_route_report(payload, json_output=args.json)
    return 0


def cmd_memory_doctor(args) -> int:
    """LLM: inspect effective memory config, route index health, and archive directories.

    大白话：这是 memory 专用体检命令。它会告诉你配置最终生效成什么、配置有没有回退 warning、
    默认或指定路由索引能不能读，以及 hook/raw 目录里现在有没有归档文件。
    """

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


def _resolve_index_path(root: Path, raw_index: str | None) -> Path:
    """LLM: resolve CLI index paths against the agent workspace root.

    大白话：不传参数时用 `memory/routing/INDEX.md`。传相对路径时按当前 agent 工作区找，
    这样测试 fixture 和真实工作区都会落在同一套规则里。
    """

    candidate = Path(raw_index).expanduser() if raw_index else DEFAULT_ROUTE_INDEX
    if candidate.is_absolute():
        return candidate.resolve()
    return (root / candidate).resolve()


def _resolve_route_mode(raw_mode: str | None, config: object) -> str:
    """LLM: choose the CLI routing mode, falling back to normalized agent config.

    大白话：命令行显式传了 `--mode` 就听命令行；没传就用配置里已经安全清洗过的模式。
    如果旧配置对象没有这个字段，就按 soft 处理。
    """

    return str(raw_mode or getattr(config, "memory_rule_routing_mode", "soft") or "soft").strip().lower()


def _resolve_auto_read_limit(raw_limit: int | None, config: object) -> int:
    """LLM: choose the route auto-read limit, falling back to normalized agent config.

    大白话：`--auto-read-limit` 没传时，就使用配置里的 `memory_rule_auto_read_limit`。
    这个值已经在加载配置时做过安全归一化。
    """

    if raw_limit is not None:
        return raw_limit
    return int(getattr(config, "memory_rule_auto_read_limit", 3))


def _build_routing_doctor(root: Path, index_path: Path) -> dict[str, Any]:
    """LLM: load and validate the route index for doctor output without raising to callers.

    大白话：doctor 要像体检报告，不能因为索引文件缺失或写坏就直接中断。
    所以这里把缺失、加载错误、校验 warning 都收进一个稳定 JSON 结构。
    """

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
    """LLM: summarize memory hook and raw archive directories using public archive path helpers.

    大白话：archive 模块负责定义 hook/raw 文件落在哪里。doctor 只顺着公开 helper 找目录，
    然后数一数文件、列出最近几个文件，方便用户判断归档骨架有没有在工作。
    """

    hook_today_path = snapshot_path_for(root)
    raw_today_path = raw_event_path_for(root)
    return {
        "retention": {
            "memory_hook_retention_days": int(getattr(config, "memory_hook_retention_days", 7)),
            "memory_hook_enabled": bool(getattr(config, "memory_hook_enabled", True)),
            "memory_hook_archive_level": int(getattr(config, "memory_hook_archive_level", 3)),
            "memory_archive_level": int(getattr(config, "memory_archive_level", 3)),
        },
        "hook": _archive_dir_payload(hook_today_path.parent, hook_today_path),
        "raw": _archive_dir_payload(raw_today_path.parent, raw_today_path),
    }


def _archive_dir_payload(directory: Path, today_path: Path) -> dict[str, Any]:
    """LLM: produce stable file-count and recent-file metadata for one archive directory.

    大白话：这里不读 JSONL 内容，只看目录和文件状态；最近文件按修改时间倒序排列，
    输出里保留名字、路径、大小和修改时间，足够排查目录有没有动静。
    """

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
        "recent_files": [_archive_file_payload(path) for path in files[:RECENT_ARCHIVE_FILE_LIMIT]],
    }


def _archive_file_payload(path: Path) -> dict[str, Any]:
    """LLM: serialize one archive file's filesystem metadata for doctor JSON.

    大白话：把最近文件变成稳定字段，测试和人都能看：文件名、完整路径、大小和 mtime。
    """

    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": stat.st_mtime,
    }


def _memory_config_payload(config: object) -> dict[str, Any]:
    """LLM: serialize the effective normalized memory config fields.

    大白话：doctor 展示的是程序真正会使用的值，不是 YAML 里原始写法。
    如果配置写坏后被回退，这里会显示回退后的安全值。
    """

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


def _config_warnings(config: object) -> list[dict[str, Any]]:
    """LLM: return normalized memory config fallback warnings as dictionaries.

    大白话：配置加载器已经把 warning 存在 config 上。这里再做一层轻量兼容，
    避免未来 warning 对象或 dict 混用时 CLI 输出变形。
    """

    warnings = getattr(config, "memory_config_warnings", []) or []
    normalized: list[dict[str, Any]] = []
    for item in warnings:
        if isinstance(item, dict):
            normalized.append(dict(item))
        elif hasattr(item, "to_dict"):
            normalized.append(item.to_dict())
        elif hasattr(item, "__dataclass_fields__"):
            normalized.append(asdict(item))
        else:
            normalized.append({"message": str(item)})
    return normalized


def _index_payload(index_path: Path) -> dict[str, Any]:
    """LLM: serialize route index location and existence state.

    大白话：所有命令都用同一种字段说明索引在哪里、存不存在、是不是文件。
    """

    return {
        "path": str(index_path),
        "exists": index_path.exists(),
        "is_file": index_path.is_file(),
    }


def _route_payload(route: MemoryRoute) -> dict[str, Any]:
    """LLM: serialize a route without leaking dataclass implementation details.

    大白话：doctor 的 routes 列表只放维护索引时最需要扫的字段，顺序稳定、内容克制。
    """

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


def _match_payload(match: MemoryRouteMatch) -> dict[str, Any]:
    """LLM: serialize one route match with enough evidence for deterministic tests.

    大白话：这里把命中的 route、分数、命中词和理由都摊开，方便用户看懂为什么建议读某个文件。
    """

    return {
        "route_id": match.route.route_id,
        "topic": match.route.topic,
        "authority_path": match.route.authority_path,
        "scope": match.route.scope,
        "priority": match.route.priority,
        "score": match.score,
        "matched_terms": match.matched_terms,
        "reasons": match.reasons,
    }


def _print_memory_route_report(payload: dict[str, Any], *, json_output: bool) -> None:
    """LLM: render the memory-route payload as either stable JSON or compact text.

    大白话：JSON 给测试和脚本用；普通文本给人扫一眼结果，用同一个 payload 避免两套逻辑漂移。
    """

    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return

    print("MY-AGENT MEMORY ROUTE")
    print(f"ok={payload['ok']}")
    print(f"workspace={payload['workspace_root']}")
    print(f"index={payload['index']['path']} exists={payload['index']['exists']}")
    print(f"mode={payload['mode']} limit={payload['limit']} auto_read_limit={payload['auto_read_limit']}")
    print(f"routes={payload['routes']['count']} matches={len(payload['matches'])}")
    for message in payload["diagnostics"]["messages"]:
        print(f"- warning: {message}")
    if payload["routes"]["validation_warnings"]:
        print("Route Warnings")
        for item in payload["routes"]["validation_warnings"]:
            print(f"- {item}")
    print("Required Read Paths")
    _print_path_list(payload["required_read_paths"])
    print("Candidate Paths")
    _print_path_list(payload["candidate_paths"])
    print("Matches")
    if not payload["matches"]:
        print("- none")
    for match in payload["matches"]:
        terms = ", ".join(match["matched_terms"]) or "-"
        reasons = "；".join(match["reasons"]) or "-"
        print(
            f"- {match['route_id']} score={match['score']:.2f} "
            f"path={match['authority_path']} terms={terms} reasons={reasons}"
        )


def _print_memory_doctor_report(payload: dict[str, Any], *, json_output: bool) -> None:
    """LLM: render the memory-doctor payload as either stable JSON or compact text.

    大白话：doctor 文本输出只保留最关键的配置、索引和目录状态；完整字段在 `--json` 里。
    """

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


def _print_path_list(paths: list[str]) -> None:
    """LLM: print a small stable list of route paths for text reports.

    大白话：空列表就明确写 none，避免用户不知道是没输出还是被截断了。
    """

    if not paths:
        print("- none")
        return
    for path in paths:
        print(f"- {path}")
