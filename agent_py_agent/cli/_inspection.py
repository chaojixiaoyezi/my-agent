"""Inspection commands: due-check, probe, execution context."""

from __future__ import annotations

import json

from ..agent.capability_config import load_capability_config
from .common import make_agent


def cmd_subagents_due_check(args) -> int:
    """巡检 subagent 状态，输出父代理需要处理的问题。"""

    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    report = agent.subagents.write_due_check(capability_config)
    issues = report.issues if args.all else report.issues[: args.limit]
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
    """检查 subagent 通道健康状态。"""

    agent = make_agent(args)
    run_ids = args.run_id or None
    report = agent.subagents.write_channel_probe_report(run_ids, limit=args.limit)
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


def cmd_subagent_context(args) -> int:
    """生成单个 subagent 的执行上下文包。"""

    agent = make_agent(args)
    context = agent.subagents.write_execution_context(args.run_id, max_cards=args.max_cards)
    print("SUBAGENT EXECUTION CONTEXT")
    print(
        f"run_id={context.run_id} skills={len(context.allowed_skills)} "
        f"tools={len(context.allowed_tools)} cards={len(context.granted_cards)}"
    )
    print(f"已写入: {context.execution_context_json}")
    print(f"已写入: {context.execution_context_file}")
    return 0
