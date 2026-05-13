# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。


from __future__ import annotations

import json

from ..agent.capability_config import load_capability_config
from ..agent.subagents.models import SubAgentCapabilityRouteOptions, SubAgentPlanActionsOptions
from ..agent.subagents.services.action_options import ActionApplyOptions
from .common import make_agent, make_capability_router
from .models import SubagentsCapabilityRouteOptions, SubagentsPlanActionsOptions


# LLM: cmd_subagents_plan_actions preserves root-scoped dry-run planning for noisy shared workspaces.
# 函数用途: CLI 动作计划入口；可按 root_id 限定一棵任务树，输出和落盘使用同一作用域。
def cmd_subagents_plan_actions(args) -> int:

    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    options = _subagents_plan_actions_options(args, agent=agent)
    report = agent.subagents.write_action_plan(
        params=SubAgentPlanActionsOptions(
            config=capability_config,
            write_report=True,
            root_id=options.root_id,
        ),
    )
    actions = report.actions if options.all else report.actions[: options.limit]
    print("SUBAGENT ACTION PLAN")
    print(f"total_actions={report.summary.get('total', 0)} mode=dry-run")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not actions:
        print("暂时没有建议动作。")
    for action in actions:
        kinds = ",".join(action.source_issue_kinds)
        print(
            f"- [{action.severity}] {action.run_id} action={action.action} "
            f"priority={action.priority} sources={kinds} :: {action.reason}"
        )
        for command in action.suggested_commands[:3]:
            print(f"  $ {command}")
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_action_plan.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_ACTION_PLAN.md'}")
    return 0


# LLM: _subagents_plan_actions_options 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成 action-plan 结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _subagents_plan_actions_options(args, *, agent=None) -> SubagentsPlanActionsOptions:
    return SubagentsPlanActionsOptions(
        all=bool(args.all),
        limit=_subagent_config_int(agent, args, "limit", "subagent_cli_default_limit"),
        root_id=str(getattr(args, "root_id", "") or ""),
    )


# LLM: cmd_subagents_apply_actions 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_subagents_apply_actions(args) -> int:

    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    options = _subagents_action_apply_options(args, agent=agent)
    report = agent.subagents.write_action_apply_report(
        capability_config,
        options=options,
    )
    mode = "apply" if options.apply else "dry-run"
    print("SUBAGENT ACTION APPLY")
    print(f"mode={mode} total_records={report.summary.get('total', 0)}")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not report.records:
        print("暂时没有匹配的动作。")
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        print(
            f"- [{status}] {record.run_id} action={record.action} "
            f"applied={record.applied} {record.before_status}->{record.after_status} :: "
            f"{record.message}"
        )
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_action_apply_report.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_ACTION_APPLY.md'}")
    if options.apply:
        print(f"审计日志: {agent.subagents.workspace / 'subagent_action_apply_log.jsonl'}")
        print(f"审计日志: {agent.subagents.workspace / 'ACTION_APPLY_LOG.md'}")
    return 0


# LLM: _subagents_action_apply_options 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _subagents_action_apply_options(args, *, agent=None) -> ActionApplyOptions:
    return ActionApplyOptions(
        apply=bool(getattr(args, "apply", False)),
        action_filter=getattr(args, "action", None) or "",
        run_id=getattr(args, "run_id", None) or "",
        take_over_by=getattr(args, "take_over_by", None) or "",
        locked_files=getattr(args, "locked_file", None) or [],
        limit=_subagent_config_int(agent, args, "limit", "subagent_cli_default_limit"),
    )


# LLM: cmd_subagents_route_capabilities 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_subagents_route_capabilities(args) -> int:

    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)
    options = _subagents_capability_route_options(args, agent=agent)
    report = agent.subagents.write_capability_route_report(
        router,
        capability_config,
        params=SubAgentCapabilityRouteOptions(
            apply=options.apply,
            run_ids=options.run_ids,
            limit=options.limit,
        ),
    )
    mode = "apply" if options.apply else "dry-run"
    print("SUBAGENT CAPABILITY ROUTE")
    print(f"mode={mode} total_records={report.summary.get('total', 0)}")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not report.records:
        print("暂时没有 OPEN capability request。")
    for record in report.records:
        cards = (
            ", ".join(f"{item['kind']}:{item['name']}" for item in record.selected_cards) or "none"
        )
        print(
            f"- [{record.status}] {record.run_id} request={record.request_id} "
            f"cards={cards} :: {record.message}"
        )
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_capability_route_report.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_CAPABILITY_ROUTE.md'}")
    if options.apply:
        print(f"审计日志: {agent.subagents.workspace / 'subagent_capability_route_log.jsonl'}")
        print(f"审计日志: {agent.subagents.workspace / 'CAPABILITY_ROUTE_LOG.md'}")
    return 0


# LLM: _subagents_capability_route_options 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _subagents_capability_route_options(args, *, agent=None) -> SubagentsCapabilityRouteOptions:
    return SubagentsCapabilityRouteOptions(
        apply=bool(args.apply),
        run_ids=args.run_id or None,
        limit=_subagent_config_int(agent, args, "limit", "subagent_cli_default_limit"),
    )


# LLM: _subagent_config_int keeps action CLI defaults in agent_config.yaml.
# 函数用途: 子代理 action/route/plan 命令没有显式 limit 时，读取统一默认值。
def _subagent_config_int(agent, args, arg_name: str, config_name: str) -> int:
    value = getattr(args, arg_name, None)
    if value is not None:
        return int(value)
    return int(getattr(getattr(agent, "config", None), config_name, 0) or 0)
