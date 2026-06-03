

from __future__ import annotations

import json
from dataclasses import asdict

from ..agent.capability_config import load_capability_config
from ..agent.subagents.models import SubAgentChannelProbeOptions, SubAgentDueCheckOptions
from ..agent.subagents.run_budget import SubagentRunBudgetRequest
from .common import make_agent
from .models import SubagentContextOptions, SubagentsDueCheckOptions, SubagentsProbeOptions


def cmd_subagents_due_check(args) -> int:

    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    options = _subagents_due_check_options(args, agent=agent)
    report = agent.subagents.write_due_check(
        params=SubAgentDueCheckOptions(
            config=capability_config,
            write_report=True,
            root_id=options.root_id,
        ),
    )
    issues = report.issues if options.all else report.issues[: options.limit]
    print("SUBAGENT DUE CHECK")
    print(f"total_issues={report.summary.get('total', 0)}")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not issues:
        print("暂时没有需要父代理介入的问题。")
    for issue in issues:
        flags = ",".join(issue.risk_flags) if issue.risk_flags else "ok"
        print(
            f"- [{issue.severity}] {issue.run_id} kind={issue.kind} "
            f"status={issue.status} action={issue.suggested_action} "
            f"owner={issue.owner or 'none'} final={issue.final_owner or 'none'} "
            f"flags={flags} :: {issue.message}"
        )
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_due_check.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_DUE_CHECK.md'}")
    return 0


def cmd_subagents_probe(args) -> int:

    agent = make_agent(args)
    options = _subagents_probe_options(args, agent=agent)
    report = agent.subagents.write_channel_probe_report(
        params=SubAgentChannelProbeOptions(run_ids=options.run_ids, limit=options.limit),
    )
    print("SUBAGENT CHANNEL PROBE")
    print(f"total={report.summary.get('total', 0)}")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not report.results:
        print("暂时没有可检查的子代理记录。")
    for result in report.results:
        failed = [check for check in result.checks if not check.ok]
        print(
            f"- {result.run_id} channel={result.channel_status} "
            f"failed_checks={len(failed)} :: {result.goal}"
        )
        for check in failed[:3]:
            print(f"  [{check.severity}] {check.name}: {check.summary} {check.error}".rstrip())
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_channel_probe.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_CHANNEL_PROBE.md'}")
    return 0


def cmd_subagents_budget(args) -> int:
    agent = make_agent(args)
    report = agent.subagents.write_run_budget_report(
        params=SubagentRunBudgetRequest(
            manager=agent.subagents,
            root_id=str(getattr(args, "root_id", "") or ""),
            max_model_calls=int(getattr(args, "max_model_calls", 0) or 0),
            max_tool_rounds=int(getattr(args, "max_tool_rounds", 0) or 0),
            max_prompt_response_tokens=int(getattr(args, "max_prompt_response_tokens", 0) or 0),
            include_dry_runs=bool(getattr(args, "include_dry_runs", False)),
        )
    )
    if bool(getattr(args, "json", False)):
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        return 0
    print("SUBAGENT RUN BUDGET")
    print(f"root_id={report.root_id or 'all'}")
    print("totals=" + json.dumps(report.totals, ensure_ascii=False, sort_keys=True))
    print("limits=" + json.dumps(report.limits, ensure_ascii=False, sort_keys=True))
    print("exceeded=" + json.dumps(report.exceeded, ensure_ascii=False))
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_run_budget.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_RUN_BUDGET.md'}")
    return 0


def cmd_subagent_context(args) -> int:

    agent = make_agent(args)
    options = _subagent_context_options(args)
    context = agent.subagents.write_execution_context(options.run_id, max_cards=options.max_cards)
    print("SUBAGENT EXECUTION CONTEXT")
    print(
        f"run_id={context.run_id} skills={len(context.allowed_skills)} "
        f"tools={len(context.allowed_tools)} cards={len(context.granted_cards)}"
    )
    print(f"已写入: {context.execution_context_json}")
    print(f"已写入: {context.execution_context_file}")
    return 0


def _subagents_due_check_options(args, *, agent=None) -> SubagentsDueCheckOptions:
    return SubagentsDueCheckOptions(
        all=bool(args.all),
        limit=_subagent_config_int(agent, args, "limit", "subagent_cli_default_limit"),
        root_id=str(getattr(args, "root_id", "") or ""),
    )


def _subagents_probe_options(args, *, agent=None) -> SubagentsProbeOptions:
    return SubagentsProbeOptions(
        run_ids=args.run_id or None,
        limit=_subagent_config_int(agent, args, "limit", "subagent_probe_default_limit"),
    )


def _subagent_context_options(args) -> SubagentContextOptions:
    return SubagentContextOptions(run_id=args.run_id, max_cards=int(args.max_cards or 0))


def _subagent_config_int(agent, args, arg_name: str, config_name: str) -> int:
    value = getattr(args, arg_name, None)
    if value is not None:
        return int(value)
    return int(getattr(getattr(agent, "config", None), config_name, 0) or 0)
