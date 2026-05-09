# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。


from __future__ import annotations

import json

from ..agent.capability_config import load_capability_config
from ..agent.subagents.models import SubAgentChannelProbeOptions, SubAgentDueCheckOptions
from .common import make_agent
from .models import SubagentContextOptions, SubagentsDueCheckOptions, SubagentsProbeOptions


# LLM: cmd_subagents_due_check keeps root-scoped reports available for noisy shared workspaces.
# 函数用途: CLI 到期检查入口；可按 root_id 限定一棵任务树，输出和落盘使用同一作用域。
def cmd_subagents_due_check(args) -> int:

    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    options = _subagents_due_check_options(args)
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


# LLM: cmd_subagents_probe 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_subagents_probe(args) -> int:

    agent = make_agent(args)
    options = _subagents_probe_options(args)
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


# LLM: cmd_subagent_context 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
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


# LLM: _subagents_due_check_options 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _subagents_due_check_options(args) -> SubagentsDueCheckOptions:
    return SubagentsDueCheckOptions(
        all=bool(args.all),
        limit=int(args.limit or 0),
        root_id=str(getattr(args, "root_id", "") or ""),
    )


# LLM: _subagents_probe_options 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _subagents_probe_options(args) -> SubagentsProbeOptions:
    return SubagentsProbeOptions(run_ids=args.run_id or None, limit=int(args.limit or 0))


# LLM: _subagent_context_options 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _subagent_context_options(args) -> SubagentContextOptions:
    return SubagentContextOptions(run_id=args.run_id, max_cards=int(args.max_cards or 0))
