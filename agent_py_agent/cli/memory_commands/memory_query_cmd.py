# LLM: CLI memory command helper; keep archive/query/doctor option shapes stable.
# 模块用途: 提供 memory 查询、诊断或归档相关命令入口。


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

DEFAULT_ROUTE_INDEX = Path("memory") / "routing" / "INDEX.md"


# LLM: RouteLogicRequest 是 memory route 命令的内部请求契约。
# 类用途: 保存 args、agent、索引路径、路由模式和自动读取上限。
@dataclass(frozen=True)
class RouteLogicRequest:
    args: Any
    agent: Any
    index_path: Path
    mode: str
    auto_read_limit: int


# LLM: _resolve_agent 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 解析路径、模式或配置默认值，返回后续流程使用的稳定值。
def _resolve_agent(args):
    import sys
    memory_mod = sys.modules.get("agent_py_agent.cli.memory_commands")
    if memory_mod is not None:
        return memory_mod.make_agent(args)
    from ..common import make_agent
    return make_agent(args)


# LLM: _load_and_validate_routes 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 读取文件、索引或配置，并转换成后续逻辑可直接使用的数据。
def _load_and_validate_routes(index_path: Path, agent) -> tuple[list[MemoryRoute], list[str]]:
    from ...agent.memory_routing import load_routes, validate_routes
    routes = load_routes(index_path)
    warnings = validate_routes(routes, agent.root)
    return routes, warnings


# LLM: _load_routes_for_matching 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 读取文件、索引或配置，并转换成后续逻辑可直接使用的数据。
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


# LLM: _try_validate_routes 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _try_validate_routes(index_path: Path, agent) -> tuple[bool, list[MemoryRoute], list[str], list[str]]:
    try:
        routes, route_warnings = _load_and_validate_routes(index_path, agent)
        return True, routes, route_warnings, ["memory route validation completed."]
    except Exception as exc:
        return False, [], [], [f"memory route index could not be loaded: {type(exc).__name__}: {exc}"]


# LLM: _try_match_routes 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _try_match_routes(index_path: Path, agent, args) -> tuple[bool, list[MemoryRoute], list[MemoryRouteMatch], list[str], list[str], list[str]]:
    try:
        routes, matches, required_paths, candidate_paths = _load_routes_for_matching(index_path, agent, args)
        return True, routes, matches, required_paths, candidate_paths, []
    except Exception as exc:
        return False, [], [], [], [], [f"memory route index could not be loaded: {type(exc).__name__}: {exc}"]


# LLM: _execute_route_logic 集中运行 route validate/match 分支；输出 payload 依赖它。
# 函数用途: 校验索引、处理 off/validate/match 模式，并返回 routes、matches 和诊断。
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
        ok, routes, diagnostics["route_warnings"], msgs = _try_validate_routes(request.index_path, agent)
        diagnostics["messages"].extend(msgs)
        return ok, routes, matches, required_read_paths, candidate_paths, diagnostics

    if request.mode == "off":
        diagnostics["messages"].append("memory routing mode is off; route matching skipped.")
        return ok, routes, matches, required_read_paths, candidate_paths, diagnostics

    ok, routes, matches, required_read_paths, candidate_paths, msgs = _try_match_routes(request.index_path, agent, args)
    diagnostics["messages"].extend(msgs)
    return ok, routes, matches, required_read_paths, candidate_paths, diagnostics


# LLM: cmd_memory_route 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_memory_route(args) -> int:
    agent = _resolve_agent(args)
    if getattr(args, "limit", None) is None:
        args.limit = int(getattr(agent.config, "cli_memory_route_limit", 5) or 0)
    mode = _resolve_route_mode(args.mode, agent.config)
    auto_read_limit = _resolve_auto_read_limit(args.auto_read_limit, agent.config)
    index_path = _resolve_index_path(agent.root, args.index)
    ok, routes, matches, req_paths, cand_paths, diagnostics = _execute_route_logic(
        RouteLogicRequest(args, agent, index_path, mode, auto_read_limit),
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


# LLM: _resolve_index_path 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 解析路径、模式或配置默认值，返回后续流程使用的稳定值。
def _resolve_index_path(root: Path, raw_index: str | None) -> Path:
    candidate = Path(raw_index).expanduser() if raw_index else DEFAULT_ROUTE_INDEX
    if candidate.is_absolute():
        return candidate.resolve()
    return (root / candidate).resolve()


# LLM: _resolve_route_mode 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 解析路径、模式或配置默认值，返回后续流程使用的稳定值。
def _resolve_route_mode(raw_mode: str | None, config: object) -> str:
    return str(raw_mode or getattr(config, "memory_rule_routing_mode", "soft") or "soft").strip().lower()


# LLM: _resolve_auto_read_limit 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 解析路径、模式或配置默认值，返回后续流程使用的稳定值。
def _resolve_auto_read_limit(raw_limit: int | None, config: object) -> int:
    if raw_limit is not None:
        return raw_limit
    return int(getattr(config, "memory_rule_auto_read_limit", 3))


# LLM: _normalize_warning_item 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 解析路径、模式或配置默认值，返回后续流程使用的稳定值。
def _normalize_warning_item(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "to_dict"):
        return item.to_dict()
    if hasattr(item, "__dataclass_fields__"):
        from dataclasses import asdict
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


# LLM: _match_payload 定义单条 route match 的 CLI/JSON 字段。
# 函数用途: 提取 route id、topic、authority、scope、score、命中词和原因。
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


# LLM: _print_memory_route_report 控制 memory route 的人读输出和 JSON 输出。
# 函数用途: 打印 ok、索引、模式、warnings、必读路径、候选路径和 matches。
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


# LLM: _print_path_list 属于memory CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_path_list(paths: list[str]) -> None:
    if not paths:
        print("- none")
        return
    for path in paths:
        print(f"- {path}")


__all__ = [
    "cmd_memory_route",
]
