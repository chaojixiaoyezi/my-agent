

from __future__ import annotations

import json
from dataclasses import dataclass
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
from ...agent.user_space.home_layout import DEFAULT_ROUTE_INDEX, resolve_route_index_target


@dataclass(frozen=True)
class RouteLogicRequest:
    args: Any
    agent: Any
    index_path: Path
    authority_root: Path
    mode: str
    auto_read_limit: int


def _resolve_agent(args):
    import sys
    memory_mod = sys.modules.get("agent_py_agent.cli.memory_commands")
    if memory_mod is not None:
        return memory_mod.make_agent(args)
    from ..common import make_agent
    return make_agent(args)


def _load_and_validate_routes(index_path: Path, agent) -> tuple[list[MemoryRoute], list[str]]:
    from ...agent.memory_routing import load_routes, validate_routes
    routes = load_routes(index_path)
    warnings = validate_routes(routes, agent.root)
    return routes, warnings


def _load_routes_for_matching(index_path: Path, agent, args) -> tuple[list[MemoryRoute], list[MemoryRouteMatch], list[str], list[str]]:
    from ...agent.memory_routing import load_routes, match_routes, resolve_required_paths

    routes = load_routes(index_path)
    matches = match_routes(args.query, routes, limit=args.limit)
    mode = str(getattr(args, "mode", None) or getattr(agent.config, "memory_rule_routing_mode", "soft")).strip().lower()
    auto_read_limit = getattr(args, "auto_read_limit", None)
    if auto_read_limit is None:
        auto_read_limit = int(getattr(agent.config, "memory_rule_auto_read_limit", 3))

    resolution = resolve_required_paths(matches, mode=mode, auto_read_limit=auto_read_limit)
    return routes, matches, resolution.required_read_paths, resolution.candidate_paths


def _try_validate_routes(index_path: Path, agent) -> tuple[bool, list[MemoryRoute], list[str], list[str]]:
    try:
        routes, route_warnings = _load_and_validate_routes(index_path, agent)
        return True, routes, route_warnings, ["memory route validation completed."]
    except Exception as exc:
        return False, [], [], [f"memory route index could not be loaded: {type(exc).__name__}: {exc}"]


def _try_validate_routes_for_root(index_path: Path, authority_root: Path) -> tuple[bool, list[MemoryRoute], list[str], list[str]]:
    try:
        routes = load_routes(index_path)
        route_warnings = validate_routes(routes, authority_root)
        return True, routes, route_warnings, ["memory route validation completed."]
    except Exception as exc:
        return False, [], [], [f"memory route index could not be loaded: {type(exc).__name__}: {exc}"]


def _try_match_routes(index_path: Path, agent, args) -> tuple[bool, list[MemoryRoute], list[MemoryRouteMatch], list[str], list[str], list[str]]:
    try:
        routes, matches, required_paths, candidate_paths = _load_routes_for_matching(index_path, agent, args)
        return True, routes, matches, required_paths, candidate_paths, []
    except Exception as exc:
        return False, [], [], [], [], [f"memory route index could not be loaded: {type(exc).__name__}: {exc}"]


def _execute_route_logic(request: RouteLogicRequest):
    args = request.args
    agent = request.agent
    warnings = _config_warnings(agent.config)
    diagnostics: dict[str, Any] = {"config_warnings": warnings, "route_warnings": [], "messages": []}
    routes: list[MemoryRoute] = []
    matches: list[MemoryRouteMatch] = []
    required_read_paths: list[str] = []
    candidate_paths: list[str] = []
    ok = True

    if not request.index_path.exists():
        ok = False
        diagnostics["messages"].append(f"memory route index not found: {request.index_path}")
        return ok, routes, matches, required_read_paths, candidate_paths, diagnostics

    if bool(getattr(args, "validate", False)):
        ok, routes, diagnostics["route_warnings"], msgs = _try_validate_routes_for_root(
            request.index_path,
            request.authority_root,
        )
        diagnostics["messages"].extend(msgs)
        return ok, routes, matches, required_read_paths, candidate_paths, diagnostics

    if request.mode == "off":
        diagnostics["messages"].append("memory routing mode is off; route matching skipped.")
        return ok, routes, matches, required_read_paths, candidate_paths, diagnostics

    ok, routes, matches, required_read_paths, candidate_paths, msgs = _try_match_routes(request.index_path, agent, args)
    diagnostics["messages"].extend(msgs)
    return ok, routes, matches, required_read_paths, candidate_paths, diagnostics


def cmd_memory_route(args) -> int:
    agent = _resolve_agent(args)
    if getattr(args, "limit", None) is None:
        args.limit = int(getattr(agent.config, "cli_memory_route_limit", 5) or 0)
    mode = _resolve_route_mode(args.mode, agent.config)
    auto_read_limit = _resolve_auto_read_limit(args.auto_read_limit, agent.config)
    index_target = resolve_route_index_target(agent.root, args.index, home_paths=agent.home_paths)
    index_path = index_target.path
    ok, routes, matches, req_paths, cand_paths, diagnostics = _execute_route_logic(
        RouteLogicRequest(args, agent, index_path, index_target.authority_root, mode, auto_read_limit),
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


def _resolve_index_path(root: Path, raw_index: str | None, *, home_paths: object | None = None) -> Path:
    return resolve_route_index_target(root, raw_index, home_paths=home_paths).path


def _resolve_route_mode(raw_mode: str | None, config: object) -> str:
    return str(raw_mode or getattr(config, "memory_rule_routing_mode", "soft") or "soft").strip().lower()


def _resolve_auto_read_limit(raw_limit: int | None, config: object) -> int:
    if raw_limit is not None:
        return raw_limit
    return int(getattr(config, "memory_rule_auto_read_limit", 3))


def _normalize_warning_item(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "to_dict"):
        return item.to_dict()
    if hasattr(item, "__dataclass_fields__"):
        from dataclasses import asdict
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


def _match_payload(match: MemoryRouteMatch) -> dict[str, Any]:
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
    if not paths:
        print("- none")
        return
    for path in paths:
        print(f"- {path}")


__all__ = [
    "cmd_memory_route",
]
