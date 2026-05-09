# LLM: CLI boundary for explicit subagent hierarchy scheduling.
# 模块用途: 把 subagents-hierarchy 命令解析成 HierarchyScheduleRequest，默认只预览。

from __future__ import annotations

import json
from dataclasses import asdict

from ..agent.subagents.services.hierarchy_recovery import HierarchyRecoveryRequest
from ..agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)
from .common import make_agent


# LLM: _parse_child_spec accepts a compact ROLE:AGENT:GOAL form for CLI ergonomics.
# 函数用途: 将命令行 child 字符串转换为 HierarchyChildSpec；goal 中允许继续包含冒号。
def _parse_child_spec(raw: str) -> HierarchyChildSpec:
    parts = raw.split(":", 2)
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise ValueError("child spec must be ROLE:AGENT_NAME:GOAL")
    role, agent_name, goal = (part.strip() for part in parts)
    return HierarchyChildSpec(role=role, agent_name=agent_name, goal=goal)


# LLM: _hierarchy_payload renders dataclass results into stable JSON-compatible dictionaries.
# 函数用途: 转换层级调度结果，供 CLI JSON 输出和测试读取。
def _hierarchy_payload(result) -> dict:
    return asdict(result)


# LLM: _print_hierarchy_result keeps human output refs-only and compact.
# 函数用途: 展示层级调度摘要，不展开任何 artifact 正文。
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


# LLM: cmd_subagents_hierarchy is the explicit command for creating child/grandchild tasks.
# 函数用途: 执行 subagents-hierarchy CLI；默认 dry-run，带 --apply 才写任务。
def cmd_subagents_hierarchy(args) -> int:
    try:
        child_specs = [_parse_child_spec(raw) for raw in args.child]
    except ValueError as exc:
        print(str(exc))
        return 2
    agent = make_agent(args)
    result = agent.subagents.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=args.run_id,
            child_specs=child_specs,
            apply=bool(args.apply),
            requested_by=args.requested_by or "parent",
            max_children=int(args.max_children or 0),
            max_depth=int(args.max_depth or 2),
        )
    )
    if args.json:
        print(json.dumps(_hierarchy_payload(result), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_hierarchy_result(result)
    return 0


# LLM: cmd_subagents_recovery_tree prints refs-only hierarchy recovery packets.
# 函数用途: 执行 subagents-recovery-tree CLI；只查询恢复线索，不接管、不读 artifact 正文。
def cmd_subagents_recovery_tree(args) -> int:
    agent = make_agent(args)
    result = agent.subagents.build_hierarchy_recovery_packet(
        params=HierarchyRecoveryRequest(
            root_run_id=args.run_id,
            requested_by=args.requested_by or "parent",
            include_healthy=not bool(args.hide_healthy),
            max_nodes=int(args.max_nodes or 200),
        )
    )
    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_recovery_tree(payload)
    return 0


# LLM: _print_recovery_tree keeps human recovery output short and refs-only.
# 函数用途: 展示恢复树摘要和候选节点，不展开恢复 refs 正文。
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
