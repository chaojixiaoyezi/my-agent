"""Dispatch and runner commands: dispatch, run, workflow plan."""

from __future__ import annotations

import json
import sys

from ..agent.agent_core.dispatch_mixin import DispatchParams
from ..agent.capability_config import load_capability_config
from ..agent.config import load_config
from ..agent.subagent_workflows import (
    WorkflowPlanningResult,
    plan_workflow_for_goal,
    write_workflow_plan_preview,
)
from .common import make_agent, make_capability_router


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
                workflow_mode=args.workflow_mode,
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
        workflow_mode=args.workflow_mode,
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


def cmd_subagents_workflow_plan(args) -> int:
    """Preview automatic subagent workflow routing without dispatching workers."""

    config = load_config(args.config)
    result = plan_workflow_for_goal(
        args.goal,
        config=config,
        explicit_template_id=args.template_id or "",
    )
    payload = result.to_dict()
    written_paths = None
    if args.output_dir:
        written_paths = write_workflow_plan_preview(result, args.output_dir)
        payload["preview_paths"] = {key: str(value) for key, value in written_paths.items()}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print("SUBAGENT WORKFLOW PLAN")
    print(
        f"mode={payload['mode']} enabled={payload['enabled']} ok={payload['ok']} "
        f"needs_confirmation={payload['needs_confirmation']}"
    )
    print(
        f"selected_template_id={payload['selected_template_id'] or 'none'} "
        f"task_type={payload['task_type']}"
    )
    print(f"reason={payload['reason']}")
    print(f"workers={payload['worker_count']}")
    for worker in payload["workers"]:
        depends_on = ",".join(worker["depends_on"]) if worker["depends_on"] else "none"
        print(
            f"- {worker['phase_id']} role={worker['role']} kind={worker['kind']} "
            f"depends_on={depends_on} checks={worker['acceptance_check_count']} :: {worker['task']}"
        )
    print(f"parent_acceptance_checks={payload['parent_acceptance_check_count']}")
    for check in payload["parent_acceptance_checklist"]:
        print(f"- {check}")
    if payload["issues"]:
        print("issues=" + json.dumps(payload["issues"], ensure_ascii=False))
    if written_paths is not None:
        print(f"preview_json={written_paths['json']}")
        print(f"preview_markdown={written_paths['markdown']}")
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
