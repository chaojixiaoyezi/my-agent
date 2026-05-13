# LLM: CLI surface for subagent leadership recovery planning; keep commands dry-run by default.
# 模块用途: 提供批量 coordinator 领导权恢复计划命令，不执行任务树改写。

from __future__ import annotations

"""CLI command for refs-only leadership recovery planning."""

import json
from dataclasses import asdict

from ..agent.capability_config import load_capability_config
from ..agent.subagents.models import (
    SubAgentLeadershipRecoveryApplyOptions,
    SubAgentLeadershipRecoveryPlanOptions,
)
from .common import make_agent


# LLM: cmd_subagents_leadership_recovery_plan prints a dry-run batch handoff plan for stale coordinators.
# 函数用途: CLI 入口；把 root、候选 leader 和容量上限整理成 bundle 交给 subagent manager。
def cmd_subagents_leadership_recovery_plan(args) -> int:
    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    params = SubAgentLeadershipRecoveryPlanOptions(
        config=capability_config,
        write_report=True,
        root_id=str(args.root_id or ""),
        leader_ids=list(args.leader or []),
        max_children_per_leader=_leader_config_int(
            agent,
            args,
            "max_children_per_leader",
            "subagent_hierarchy_max_children_per_tool_call",
        ),
    )
    report = agent.subagents.write_leadership_recovery_plan(params=params)
    if args.json:
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        return 0
    print("SUBAGENT LEADERSHIP RECOVERY PLAN")
    print("mode=dry-run")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not report.assignments:
        print("暂时没有可分配的 leadership recovery 批次。")
    for item in report.assignments:
        print(
            f"- coordinator={item.coordinator_id} -> leader={item.leader_id} "
            f"children={len(item.child_ids)} apply_supported={item.apply_supported}"
        )
        if item.suggested_command:
            print(f"  future $ {item.suggested_command}")
    for item in report.unassigned:
        print(f"- unassigned coordinator={item.coordinator_id} children={len(item.child_ids)} reason={item.reason}")
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_leadership_recovery_plan.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_LEADERSHIP_RECOVERY_PLAN.md'}")
    return 0


# LLM: cmd_subagents_leadership_recovery_apply performs a controlled subset handoff only with --apply.
# 函数用途: CLI 分批接管入口；默认 dry-run，显式 --apply 才移动 child 子树。
def cmd_subagents_leadership_recovery_apply(args) -> int:
    agent = make_agent(args)
    params = SubAgentLeadershipRecoveryApplyOptions(
        root_id=str(args.root_id or ""),
        coordinator_id=str(args.coordinator or ""),
        leader_id=str(args.leader or ""),
        child_ids=list(args.child_run_id or []),
        apply=bool(args.apply),
        max_children_per_leader=_leader_config_int(
            agent,
            args,
            "max_children_per_leader",
            "subagent_hierarchy_max_children_per_tool_call",
        ),
    )
    report = agent.subagents.write_leadership_recovery_apply(params=params)
    if args.json:
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        return 0
    mode = "apply" if params.apply else "dry-run"
    print("SUBAGENT LEADERSHIP RECOVERY APPLY")
    print(f"mode={mode}")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        print(
            f"- [{status}] coordinator={record.coordinator_id} -> leader={record.leader_id} "
            f"applied={record.applied} moved={len(record.moved_child_ids)} :: {record.message}"
        )
        if record.blocked_by:
            print("  blocked_by=" + ",".join(record.blocked_by))
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_leadership_recovery_apply_report.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_LEADERSHIP_RECOVERY_APPLY.md'}")
    return 0


# LLM: _leader_config_int keeps leadership recovery capacities configurable.
# 函数用途: leadership recovery CLI 未传容量时，读取 agent_config.yaml 默认值。
def _leader_config_int(agent, args, arg_name: str, config_name: str) -> int:
    value = getattr(args, arg_name, None)
    if value is not None:
        return int(value)
    return int(getattr(agent.config, config_name, 0) or 0)
