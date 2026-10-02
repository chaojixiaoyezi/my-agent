

from __future__ import annotations

import json

from ..agent.capability.config import load_capability_config
from ..agent.subagents.models import SubAgentCapabilityRouteOptions, SubAgentPlanActionsOptions
from ..agent.subagents.services.actions import ActionApplyOptions
from ._board import SUBAGENT_CLI_DEFAULT_COUNT
from .common import int_arg_or_default, make_agent, make_capability_router
from .models import SubagentsCapabilityRouteOptions, SubagentsPlanActionsOptions


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


def _subagents_plan_actions_options(args, *, agent=None) -> SubagentsPlanActionsOptions:
    return SubagentsPlanActionsOptions(
        all=bool(args.all),
        limit=int_arg_or_default(args, "limit", SUBAGENT_CLI_DEFAULT_COUNT),
        root_id=str(getattr(args, "root_id", "") or ""),
    )


def cmd_subagents_apply_actions(args) -> int:

    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    options = _subagents_action_apply_options(args, agent=agent)
    report = agent.subagents.actions.write_action_apply_report(
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


def _subagents_action_apply_options(args, *, agent=None) -> ActionApplyOptions:
    return ActionApplyOptions(
        apply=bool(getattr(args, "apply", False)),
        action_filter=getattr(args, "action", None) or "",
        run_id=getattr(args, "run_id", None) or "",
        take_over_by=getattr(args, "take_over_by", None) or "",
        locked_files=getattr(args, "locked_file", None) or [],
        limit=int_arg_or_default(args, "limit", SUBAGENT_CLI_DEFAULT_COUNT),
    )


def cmd_subagents_route_capabilities(args) -> int:

    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)
    options = _subagents_capability_route_options(args, agent=agent)
    report = agent.subagents.capability.write_capability_route_report(
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


def _subagents_capability_route_options(args, *, agent=None) -> SubagentsCapabilityRouteOptions:
    return SubagentsCapabilityRouteOptions(
        apply=bool(args.apply),
        run_ids=args.run_id or None,
        limit=int_arg_or_default(args, "limit", SUBAGENT_CLI_DEFAULT_COUNT),
    )
