
from __future__ import annotations

import json
import sys

from ..agent.agent_core.dispatch_params import DispatchParams, WatchParams
from ..agent.agent_core.subagent_params import SubagentRunParams
from ..agent.capability_config import load_capability_config
from ..agent.config import load_config
from ..agent.subagent_workflows import (
    WorkflowPlanConstraints,
    WorkflowPlanningResult,
    plan_workflow_for_goal,
    write_workflow_plan_preview,
)
from .common import make_agent, make_capability_router
from .models import SubagentsDispatchOptions


def _subagents_dispatch_options(args) -> SubagentsDispatchOptions:
    # LLM: subagent dispatch CLI args collapse into one bundle before agent calls.
    return SubagentsDispatchOptions(
        apply=bool(args.apply),
        execute_runners=bool(args.execute_runners),
        planner=bool(args.planner),
        workflow_mode=args.workflow_mode or "off",
        max_runners=int(args.max_runners or 0),
        limit=int(args.limit or 0),
        reviewer=args.reviewer or "parent-dispatch",
        note=args.note or "",
        instruction=args.instruction or "",
        max_cards=int(args.max_cards or 0),
        probe=not bool(args.no_probe),
        take_over_by=args.take_over_by or "",
        locked_files=args.locked_file or [],
        interval=float(args.interval or 0),
        max_cycles=int(args.max_cycles or 0),
        force_lock=bool(args.force_lock),
        watch=bool(args.watch),
    )


def _dispatch_params(options: SubagentsDispatchOptions) -> DispatchParams:
    return DispatchParams(
        apply=options.apply,
        execute_runners=options.execute_runners,
        planner=options.planner,
        workflow_mode=options.workflow_mode,
        max_runners=options.max_runners,
        limit=options.limit,
        reviewer=options.reviewer,
        note=options.note,
        runner_instruction=options.instruction,
        max_cards=options.max_cards,
        probe=options.probe,
        take_over_by=options.take_over_by,
        locked_files=options.locked_files,
    )


def _watch_params(options: SubagentsDispatchOptions) -> WatchParams:
    return WatchParams(
        **_dispatch_params(options).__dict__,
        interval=options.interval,
        max_cycles=options.max_cycles,
        force_lock=options.force_lock,
    )


def _print_watch_report(agent, report, options: SubagentsDispatchOptions) -> None:
    mode = "apply" if options.apply else "dry-run"
    print("SUBAGENT DISPATCH WATCH")
    print(
        f"mode={mode} planner={options.planner} execute_runners={options.execute_runners} "
        f"cycles={report.summary.get('total', 0)}"
    )
    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        print(
            f"- [{status}] cycle={record.cycle} records={record.dispatch_record_count} "
            f":: {record.message}"
        )
    ws = agent.subagents.workspace
    print(f"\n已写入: {ws / 'subagent_dispatch_watch_report.json'}")
    print(f"已写入: {ws / 'SUBAGENT_DISPATCH_WATCH.md'}")
    print(f"heartbeat: {ws / 'subagent_dispatch_watch_heartbeat.json'}")
    print(f"watch log: {ws / 'subagent_dispatch_watch_log.jsonl'}")
    print(f"watch log: {ws / 'DISPATCH_WATCH_LOG.md'}")
    if options.planner:
        print(f"planner: {ws / 'parent_planner_report.json'}")
        print(f"planner: {ws / 'PARENT_PLANNER.md'}")


def _print_dispatch_report(agent, report, options: SubagentsDispatchOptions) -> None:
    mode = "apply" if options.apply else "dry-run"
    print("SUBAGENT DISPATCH")
    print(
        f"mode={mode} planner={options.planner} execute_runners={options.execute_runners} "
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
    ws = agent.subagents.workspace
    print(f"\n已写入: {ws / 'subagent_dispatch_report.json'}")
    print(f"已写入: {ws / 'SUBAGENT_DISPATCH.md'}")
    if options.apply:
        print(f"审计日志: {ws / 'subagent_dispatch_log.jsonl'}")
        print(f"审计日志: {ws / 'DISPATCH_LOG.md'}")
    if options.planner:
        print(f"planner: {ws / 'parent_planner_report.json'}")
        print(f"planner: {ws / 'PARENT_PLANNER.md'}")


def cmd_subagents_dispatch(args) -> int:

    options = _subagents_dispatch_options(args)
    if options.execute_runners and not options.apply:
        print("--execute-runners 必须和 --apply 一起使用。", file=sys.stderr)
        return 2

    agent = make_agent(args)
    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)
    if options.watch:
        try:
            report = agent.watch_subagents(router, capability_config, params=_watch_params(options))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        _print_watch_report(agent, report, options)
        return 0

    report = agent.dispatch_subagents(router, capability_config, params=_dispatch_params(options))
    _print_dispatch_report(agent, report, options)
    return 0


def cmd_subagents_workflow_plan(args) -> int:

    config = load_config(args.config)
    result = plan_workflow_for_goal(
        args.goal,
        constraints=WorkflowPlanConstraints(
            config=config,
            explicit_template_id=args.template_id or "",
        ),
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

    agent = make_agent(args)
    result = agent.run_subagent(
        params=SubagentRunParams(
            run_id=args.run_id,
            instruction=args.instruction or "",
            dry_run=not args.execute,
            max_cards=args.max_cards,
            probe=not args.no_probe,
        )
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
