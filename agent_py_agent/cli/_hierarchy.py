
from __future__ import annotations

import json
from dataclasses import asdict

from ..agent.capability.config import load_capability_config
from ..agent.subagents.services.hierarchy.recovery import HierarchyRecoveryRequest
from ..agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)
from .common import int_arg_or_default, make_agent

# 参数减量第 3 批：层级创建默认深度与恢复树默认节点数不再是配置项，--max-depth / --max-nodes 仍优先。
_SUBAGENT_HIERARCHY_DEFAULT_MAX_DEPTH = 0
# 恢复代理树最多重建 200 个节点，--max-nodes 优先
_SUBAGENT_HIERARCHY_RECOVERY_MAX_NODE_COUNT = 200


def _parse_child_spec(raw: str) -> HierarchyChildSpec:
    parts = raw.split(":", 2)
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise ValueError("child spec must be ROLE:AGENT_NAME:GOAL")
    role, agent_name, goal = (part.strip() for part in parts)
    return HierarchyChildSpec(role=role, agent_name=agent_name, goal=goal)


def _hierarchy_payload(result) -> dict:
    return asdict(result)


def _print_hierarchy_result(result) -> None:
    mode = "dry-run" if result.dry_run else "apply"
    status = "BLOCKED" if result.blocked else "OK"
    print("SUBAGENT HIERARCHY")
    print(
        f"mode={mode} status={status} parent={result.parent_run_id} "
        f"root={result.root_id} planned={result.planned_count} created={len(result.created_run_ids)}"
    )
    print(f"reason={result.reason}")
    for item in result.items:
        run = item.run_id or "planned"
        print(f"- depth={item.depth} run={run} parent={item.parent_id} role={item.role} :: {item.goal}")


def cmd_subagents_hierarchy(args) -> int:
    try:
        child_specs = [_parse_child_spec(raw) for raw in args.child]
    except ValueError as exc:
        print(str(exc))
        return 2
    agent = make_agent(args)
    result = agent.subagents.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=args.run_id,
            child_specs=child_specs,
            apply=bool(args.apply),
            requested_by=args.requested_by or "parent",
            max_children=int(args.max_children or 0),
            max_depth=int_arg_or_default(args, "max_depth", _SUBAGENT_HIERARCHY_DEFAULT_MAX_DEPTH),
        )
    )
    if args.json:
        print(json.dumps(_hierarchy_payload(result), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_hierarchy_result(result)
    return 0


def cmd_subagents_recovery_tree(args) -> int:
    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    result = agent.subagents.hierarchy.build_hierarchy_recovery_packet(
        params=HierarchyRecoveryRequest(
            root_run_id=args.run_id,
            requested_by=args.requested_by or "parent",
            include_healthy=not bool(args.hide_healthy),
            max_nodes=int_arg_or_default(args, "max_nodes", _SUBAGENT_HIERARCHY_RECOVERY_MAX_NODE_COUNT),
            heartbeat_timeout=float(capability_config.subagent_heartbeat_timeout),
            run_timeout=float(capability_config.subagent_run_timeout),
        )
    )
    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_recovery_tree(payload)
    return 0


def _print_recovery_tree(payload: dict[str, object]) -> None:
    print("SUBAGENT RECOVERY TREE")
    print(
        f"root={payload.get('root_run_id', '')} nodes={payload.get('node_count', 0)} "
        f"candidates={payload.get('recovery_candidate_count', 0)} "
        f"omitted_healthy={payload.get('omitted_healthy_count', 0)}"
    )
    for node in payload.get("nodes", []):
        if not isinstance(node, dict):
            continue
        marker = "RECOVERY" if node.get("needs_recovery") else "OK"
        print(
            f"- [{marker}] depth={node.get('depth', 0)} run={node.get('run_id', '')} "
            f"status={node.get('status', '')} reason={node.get('recovery_reason', '')}"
        )
        if node.get("takeover_readiness_ref"):
            print(f"  takeover_readiness_ref={node.get('takeover_readiness_ref')}")
