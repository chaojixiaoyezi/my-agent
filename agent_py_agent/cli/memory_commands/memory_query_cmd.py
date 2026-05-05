"""LLM: implements memory route CLI diagnostics.

给人看的解释：
route 命令通过已配置的 memory 索引来路由查询，并打印稳定报告。
索引缺失、配置关闭或索引写坏时，会输出诊断信息而不是让 CLI 崩溃。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...agent.memory_routing import (
    MemoryRoute,
    MemoryRouteMatch,
    load_routes,
    match_routes,
    resolve_required_paths,
    validate_routes,
)

DEFAULT_ROUTE_INDEX = Path("memory") / "routing" / "INDEX.md"


def _resolve_agent(args):
    """创建 agent 实例，支持测试 patching。"""
    import sys
    memory_mod = sys.modules.get("agent_py_agent.cli.memory_commands")
    if memory_mod is not None:
        return memory_mod.make_agent(args)
    from ..common import make_agent
    return make_agent(args)


def _load_and_validate_routes(index_path: Path, agent) -> tuple[list[MemoryRoute], list[str]]:
    """Load routes from index and validate them, returning (routes, warnings)."""
    from ...agent.memory_routing import load_routes, validate_routes
    routes = load_routes(index_path)
    warnings = validate_routes(routes, agent.root)
    return routes, warnings


def _load_routes_for_matching(index_path: Path, agent, args) -> tuple[list[MemoryRoute], list[MemoryRouteMatch], list[str], list[str]]:
    """Load routes and perform matching, returning routes, matches, req_paths, cand_paths."""
    from ...agent.memory_routing import load_routes, match_routes, resolve_required_paths

    routes = load_routes(index_path)
    matches = match_routes(args.query, routes, limit=args.limit)
    mode = str(getattr(args, "mode", None) or getattr(agent.config, "memory_rule_routing_mode", "soft")).strip().lower()
    auto_read_limit = getattr(args, "auto_read_limit", None)
    if auto_read_limit is None:
        auto_read_limit = int(getattr(agent.config, "memory_rule_auto_read_limit", 3))

    resolution = resolve_required_paths(matches, mode=mode, auto_read_limit=auto_read_limit)
    return routes, matches, resolution.required_read_paths, resolution.candidate_paths


def _execute_route_logic(args, agent, index_path: Path, mode: str, auto_read_limit: int):
    """Execute routing match logic, returning (ok, routes, matches, required_paths, candidate_paths, diagnostics)."""
    warnings = _config_warnings(agent.config)
    diagnostics: dict[str, Any] = {"config_warnings": warnings, "route_warnings": [], "messages": []}
    routes: list[MemoryRoute] = []
    matches: list[MemoryRouteMatch] = []
    required_read_paths: list[str] = []
    candidate_paths: list[str] = []
    ok = True

    if not index_path.exists():
        ok = False
        diagnostics["messages"].append(f"memory route index not found: {index_path}")
    elif bool(getattr(args, "validate", False)):
        try:
            routes, diagnostics["route_warnings"] = _load_and_validate_routes(index_path, agent)
            diagnostics["messages"].append("memory route validation completed.")
        except Exception as exc:
            ok = False
            diagnostics["messages"].append(f"memory route index could not be loaded: {type(exc).__name__}: {exc}")
    elif mode == "off":
        diagnostics["messages"].append("memory routing mode is off; route matching skipped.")
    else:
        try:
            routes, matches, required_read_paths, candidate_paths = _load_routes_for_matching(index_path, agent, args)
        except Exception as exc:
            ok = False
            diagnostics["messages"].append(f"memory route index could not be loaded: {type(exc).__name__}: {exc}")

    return ok, routes, matches, required_read_paths, candidate_paths, diagnostics


def cmd_memory_route(args) -> int:
    """Route a query through the configured memory index and print a stable report.

    参数说明:
    `args` 是 argparse 解析后的对象，至少包含 `query`、`index`、`mode`、`limit`、
    `auto_read_limit`、`json`，以及创建 agent 需要的通用 CLI 参数。

    返回说明:
    返回进程退出码；当前诊断类命令成功打印报告后返回 0。
    """
    agent = _resolve_agent(args)
    mode = _resolve_route_mode(args.mode, agent.config)
    auto_read_limit = _resolve_auto_read_limit(args.auto_read_limit, agent.config)
    index_path = _resolve_index_path(agent.root, args.index)
    ok, routes, matches, req_paths, cand_paths, diagnostics = _execute_route_logic(
        args, agent, index_path, mode, auto_read_limit,
    )
    payload = {
        "ok": ok, "workspace_root": str(agent.root), "query": args.query,
        "validate": bool(getattr(args, "validate", False)),
        "mode": mode, "limit": args.limit, "auto_read_limit": auto_read_limit,
        "routing_enabled": bool(getattr(agent.config, "memory_rule_routing_enabled", True)),
        "index": _index_payload(index_path),
        "warnings": diagnostics["config_warnings"],
        "routes": {"count": len(routes), "validation_warnings": diagnostics["route_warnings"]},
        "matches": [_match_payload(m) for m in matches],
        "required_read_paths": req_paths, "candidate_paths": cand_paths,
        "diagnostics": diagnostics,
    }
    _print_memory_route_report(payload, json_output=args.json)
    return 0


def _resolve_index_path(root: Path, raw_index: str | None) -> Path:
    """Resolve CLI index paths against the agent workspace root."""
    candidate = Path(raw_index).expanduser() if raw_index else DEFAULT_ROUTE_INDEX
    if candidate.is_absolute():
        return candidate.resolve()
    return (root / candidate).resolve()


def _resolve_route_mode(raw_mode: str | None, config: object) -> str:
    """Choose the CLI routing mode, falling back to normalized agent config."""
    return str(raw_mode or getattr(config, "memory_rule_routing_mode", "soft") or "soft").strip().lower()


def _resolve_auto_read_limit(raw_limit: int | None, config: object) -> int:
    """Choose the route auto-read limit, falling back to normalized agent config."""
    if raw_limit is not None:
        return raw_limit
    return int(getattr(config, "memory_rule_auto_read_limit", 3))


def _normalize_warning_item(item: Any) -> dict[str, Any]:
    """Normalize a single config warning item to a dictionary."""
    if isinstance(item, dict):
        return dict(item)
    elif hasattr(item, "to_dict"):
        return item.to_dict()
    elif hasattr(item, "__dataclass_fields__"):
        from dataclasses import asdict
        return asdict(item)
    else:
        return {"message": str(item)}


def _config_warnings(config: object) -> list[dict[str, Any]]:
    """Return normalized memory config fallback warnings as dictionaries."""
    warnings = getattr(config, "memory_config_warnings", []) or []
    return [_normalize_warning_item(item) for item in warnings]


def _index_payload(index_path: Path) -> dict[str, Any]:
    """Serialize route index location and existence state."""
    return {
        "path": str(index_path),
        "exists": index_path.exists(),
        "is_file": index_path.is_file(),
    }


def _match_payload(match: MemoryRouteMatch) -> dict[str, Any]:
    """Serialize one route match with enough evidence for deterministic tests."""
    return {
        "route_id": match.route.route_id,
        "topic": match.route.topic,
        "authority_path": match.route.authority_file(),
        "scope": match.route.scope,
        "priority": match.route.priority,
        "score": match.score,
        "matched_terms": match.matched_terms,
        "reasons": match.reasons,
    }


def _print_memory_route_report(payload: dict[str, Any], *, json_output: bool) -> None:
    """Render the memory-route payload as either stable JSON or compact text."""
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


def _print_path_list(paths: list[str]) -> None:
    """Print a small stable list of route paths for text reports."""
    if not paths:
        print("- none")
        return
    for path in paths:
        print(f"- {path}")


__all__ = [
    "cmd_memory_route",
]