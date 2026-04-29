from __future__ import annotations

"""LLM: implements CLI commands for subagent boards, due-checks, actions, routing, reviews, dispatch, and runner entrypoints.

给人看的解释：
这个文件只管用户在命令行里怎么操作子代理。
实际状态机和文件写入仍然交给 SimpleAgent/SubAgentManager，这里主要做参数转发和结果打印。
"""

import json
import sys

from ..agent.capability_config import load_capability_config
from ..agent.subagent import filter_board_items
from .common import make_agent, make_capability_router


def cmd_spawn(args) -> int:
    """生成子任务记录。"""

    agent = make_agent(args)
    tasks = agent.spawn_subagents(args.goal, args.count)
    for task in tasks:
        print(json.dumps(task.__dict__, ensure_ascii=False))
    return 0


def cmd_subagents(args) -> int:
    """显示子代理红绿灯看板。"""

    agent = make_agent(args)
    board = agent.subagents.write_board(recent_limit=args.limit)
    items = filter_board_items(
        board.items if args.all else board.hot_list or board.recent,
        status=args.status or "",
        owner=args.owner or "",
        root_id=args.root_id or "",
    )
    print("SUBAGENT BOARD")
    print(f"total={board.summary.get('total', 0)} hot={len(board.hot_list)}")
    print("summary=" + json.dumps(board.summary, ensure_ascii=False, sort_keys=True))
    if not items:
        print("没有匹配的子代理记录。")
        return 0
    for item in items[: args.limit]:
        flags = ",".join(item.risk_flags) if item.risk_flags else "ok"
        print(
            f"- {item.id} status={item.status} verify={item.verification_status} "
            f"channel={item.channel_status} depth={item.depth} "
            f"owner={item.owner or 'none'} final={item.final_owner or 'none'} "
            f"evidence={item.evidence_count} requests={item.open_request_count} "
            f"gaps={item.open_gap_count} flags={flags} :: {item.goal}"
        )
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_board.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_BOARD.md'}")
    return 0


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


def cmd_subagents_plan_actions(args) -> int:
    """根据 due-check 生成 dry-run 动作计划。"""

    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    report = agent.subagents.write_action_plan(capability_config)
    actions = report.actions if args.all else report.actions[: args.limit]
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


def cmd_subagents_apply_actions(args) -> int:
    """执行或 dry-run 执行 action plan。"""

    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    report = agent.subagents.write_action_apply_report(
        capability_config,
        apply=args.apply,
        action_filter=args.action or "",
        run_id=args.run_id or "",
        take_over_by=args.take_over_by or "",
        locked_files=args.locked_file or [],
        limit=args.limit,
    )
    mode = "apply" if args.apply else "dry-run"
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
    if args.apply:
        print(f"审计日志: {agent.subagents.workspace / 'subagent_action_apply_log.jsonl'}")
        print(f"审计日志: {agent.subagents.workspace / 'ACTION_APPLY_LOG.md'}")
    return 0


def cmd_subagents_route_capabilities(args) -> int:
    """路由 OPEN capability request，默认 dry-run。"""

    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)
    report = agent.subagents.write_capability_route_report(
        router,
        capability_config,
        apply=args.apply,
        run_ids=args.run_id or None,
        limit=args.limit,
    )
    mode = "apply" if args.apply else "dry-run"
    print("SUBAGENT CAPABILITY ROUTE")
    print(f"mode={mode} total_records={report.summary.get('total', 0)}")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not report.records:
        print("暂时没有 OPEN capability request。")
    for record in report.records:
        cards = ", ".join(f"{item['kind']}:{item['name']}" for item in record.selected_cards) or "none"
        print(
            f"- [{record.status}] {record.run_id} request={record.request_id} "
            f"cards={cards} :: {record.message}"
        )
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_capability_route_report.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_CAPABILITY_ROUTE.md'}")
    if args.apply:
        print(f"审计日志: {agent.subagents.workspace / 'subagent_capability_route_log.jsonl'}")
        print(f"审计日志: {agent.subagents.workspace / 'CAPABILITY_ROUTE_LOG.md'}")
    return 0


def cmd_subagents_acceptance(args) -> int:
    """验收等待验收的 subagent，默认 dry-run。"""

    agent = make_agent(args)
    report = agent.subagents.write_acceptance_review_report(
        run_ids=args.run_id or None,
        apply=args.apply,
        reviewer=args.reviewer,
        note=args.note or "",
        limit=args.limit,
    )
    mode = "apply" if args.apply else "dry-run"
    print("SUBAGENT ACCEPTANCE")
    print(f"mode={mode} total_records={report.summary.get('total', 0)}")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not report.records:
        print("暂时没有等待验收的 subagent。")
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        print(
            f"- [{status}] {record.run_id} decision={record.decision} "
            f"applied={record.applied} {record.before_status}/{record.before_verification_status}"
            f"->{record.after_status}/{record.after_verification_status} :: {record.message}"
        )
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_acceptance_report.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_ACCEPTANCE.md'}")
    if args.apply:
        print(f"审计日志: {agent.subagents.workspace / 'subagent_acceptance_log.jsonl'}")
        print(f"审计日志: {agent.subagents.workspace / 'ACCEPTANCE_REVIEW_LOG.md'}")
    return 0


def cmd_subagents_patches(args) -> int:
    """审核 runner 输出里的 patch 记录，默认 dry-run。"""

    agent = make_agent(args)
    report = agent.subagents.write_patch_review_report(
        run_ids=args.run_id or None,
        apply=args.apply,
        reviewer=args.reviewer,
        note=args.note or "",
        limit=args.limit,
    )
    mode = "apply" if args.apply else "dry-run"
    print("SUBAGENT PATCH REVIEW")
    print(f"mode={mode} total_records={report.summary.get('total', 0)}")
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not report.records:
        print("暂时没有 patch 需要审核。")
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        print(
            f"- [{status}] {record.run_id} decision={record.decision} "
            f"patches={record.patch_count} approved={record.approved_count} "
            f"blocked={record.blocked_count} applied={record.applied} :: {record.message}"
        )
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_patch_review_report.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_PATCH_REVIEW.md'}")
    if args.apply:
        print(f"审计日志: {agent.subagents.workspace / 'subagent_patch_review_log.jsonl'}")
        print(f"审计日志: {agent.subagents.workspace / 'PATCH_REVIEW_LOG.md'}")
    return 0


def cmd_subagents_dispatch(args) -> int:
    """执行一轮父代理调度，默认 dry-run。"""

    if args.execute_runners and not args.apply:
        print("--execute-runners 必须和 --apply 一起使用。", file=sys.stderr)
        return 2

    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)
    if args.watch:
        try:
            report = agent.watch_subagents(
                router,
                capability_config,
                apply=args.apply,
                execute_runners=args.execute_runners,
                planner=args.planner,
                max_runners=args.max_runners,
                limit=args.limit,
                reviewer=args.reviewer,
                note=args.note or "",
                runner_instruction=args.instruction or "",
                max_cards=args.max_cards,
                probe=not args.no_probe,
                take_over_by=args.take_over_by or "",
                locked_files=args.locked_file or [],
                interval=args.interval,
                max_cycles=args.max_cycles,
                force_lock=args.force_lock,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        mode = "apply" if args.apply else "dry-run"
        print("SUBAGENT DISPATCH WATCH")
        print(
            f"mode={mode} planner={args.planner} execute_runners={args.execute_runners} "
            f"cycles={report.summary.get('total', 0)}"
        )
        print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
        for record in report.records:
            status = "OK" if record.ok else "FAIL"
            print(
                f"- [{status}] cycle={record.cycle} records={record.dispatch_record_count} "
                f":: {record.message}"
            )
        print(f"\n已写入: {agent.subagents.workspace / 'subagent_dispatch_watch_report.json'}")
        print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_DISPATCH_WATCH.md'}")
        print(f"heartbeat: {agent.subagents.workspace / 'subagent_dispatch_watch_heartbeat.json'}")
        print(f"watch log: {agent.subagents.workspace / 'subagent_dispatch_watch_log.jsonl'}")
        print(f"watch log: {agent.subagents.workspace / 'DISPATCH_WATCH_LOG.md'}")
        if args.planner:
            print(f"planner: {agent.subagents.workspace / 'parent_planner_report.json'}")
            print(f"planner: {agent.subagents.workspace / 'PARENT_PLANNER.md'}")
        return 0

    report = agent.dispatch_subagents(
        router,
        capability_config,
        apply=args.apply,
        execute_runners=args.execute_runners,
        planner=args.planner,
        max_runners=args.max_runners,
        limit=args.limit,
        reviewer=args.reviewer,
        note=args.note or "",
        runner_instruction=args.instruction or "",
        max_cards=args.max_cards,
        probe=not args.no_probe,
        take_over_by=args.take_over_by or "",
        locked_files=args.locked_file or [],
    )
    mode = "apply" if args.apply else "dry-run"
    print("SUBAGENT DISPATCH")
    print(
        f"mode={mode} planner={args.planner} execute_runners={args.execute_runners} "
        f"total_records={report.summary.get('total', 0)}"
    )
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    if not report.records:
        print("暂时没有调度动作。")
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        run = record.run_id or "global"
        print(
            f"- [{status}] {record.step}/{record.action} run={run} "
            f"applied={record.applied} :: {record.message}"
        )
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_dispatch_report.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_DISPATCH.md'}")
    if args.apply:
        print(f"审计日志: {agent.subagents.workspace / 'subagent_dispatch_log.jsonl'}")
        print(f"审计日志: {agent.subagents.workspace / 'DISPATCH_LOG.md'}")
    if args.planner:
        print(f"planner: {agent.subagents.workspace / 'parent_planner_report.json'}")
        print(f"planner: {agent.subagents.workspace / 'PARENT_PLANNER.md'}")
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


def cmd_subagent_run(args) -> int:
    """按 execution context 运行或 dry-run 一个 subagent。"""

    agent = make_agent(args)
    result = agent.run_subagent(
        args.run_id,
        instruction=args.instruction or "",
        dry_run=not args.execute,
        max_cards=args.max_cards,
        probe=not args.no_probe,
    )
    mode = "execute" if args.execute else "dry-run"
    status = "OK" if result.ok else "FAIL"
    print("SUBAGENT RUNNER")
    print(
        f"mode={mode} ok={status} run_id={result.run_id} "
        f"status={result.status} verify={result.verification_status}"
    )
    print(f"message={result.message}")
    print(f"已写入: {result.execution_context_json}")
    print(f"已写入: {result.result_json}")
    print(f"已写入: {result.result_file}")
    if result.prompt_file:
        print(f"prompt: {result.prompt_file}")
    if result.response_file:
        print(f"response: {result.response_file}")
    return 0 if result.ok else 1


def cmd_subagent_detail(args) -> int:
    """显示单个子代理运行详情。"""

    agent = make_agent(args)
    task = agent.subagents.load(args.run_id)
    print(json.dumps(task.__dict__, ensure_ascii=False, indent=2, default=lambda value: value.__dict__))
    return 0
