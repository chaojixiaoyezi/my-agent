
from __future__ import annotations

import json
from dataclasses import asdict

from ..agent.capability.config import load_capability_config
from ..agent.subagents.services.hierarchy.recovery import HierarchyRecoveryRequest
from ..agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)
from .common import make_agent


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
            max_depth=_hierarchy_config_int(agent, args, "max_depth", "subagent_hierarchy_default_max_depth"),
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
            max_nodes=_hierarchy_config_int(agent, args, "max_nodes", "subagent_hierarchy_recovery_max_nodes"),
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


def _hierarchy_config_int(agent, args, arg_name: str, config_name: str) -> int:
    value = getattr(args, arg_name, None)
    if value is not None:
        return int(value)
    return int(getattr(agent.config, config_name, 0) or 0)
