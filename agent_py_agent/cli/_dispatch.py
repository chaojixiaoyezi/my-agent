

from __future__ import annotations

import json
import sys

from ..agent.agent_core.orchestration.dispatch.params import (
    DispatchExecutionPlan,
    DispatchParams,
    DispatchRuntimePolicy,
    WatchParams,
)
from ..agent.agent_core.runner.gate import get_task_timeout, resolve_runner_config
from ..agent.agent_core.runner.worker import RunSubagentWorkerParams, _run_subagent_worker
from ..agent.agent_core.subagent import SubagentRunParams
from ..agent.capability_config import load_capability_config
from ..agent.config import load_config
from ..agent.subagent_workflows import (
    WorkflowPlanConstraints,
    WorkflowPlanningResult,
    plan_workflow_for_goal,
    write_workflow_plan_preview,
)
from .common import make_agent, make_capability_router
from .dispatch_background import BackgroundLaunchUpdate, mark_background_launch
from .models import SubagentsDispatchOptions


def _subagents_dispatch_options(args, agent=None) -> SubagentsDispatchOptions:
    config = getattr(agent, "config", None)
    policy = DispatchRuntimePolicy.from_config(config)
    return SubagentsDispatchOptions(
        mutate_state=bool(args.apply),
        start_runners=bool(args.start_runners),
        planner=bool(args.planner),
        workflow_mode=args.workflow_mode or "off",
        max_runners=_configured_int(args.max_runners, policy.default_max_runners),
        limit=_configured_int(args.limit, policy.default_limit),
        reviewer=args.reviewer or "parent-dispatch",
        note=args.note or "",
        instruction=args.instruction or "",
        max_cards=int(args.max_cards or 0),
        probe=not bool(args.no_probe),
        take_over_by=args.take_over_by or "",
        locked_files=args.locked_file or [],
        interval=_configured_float(args.interval, policy.default_watch_interval),
        max_cycles=int(args.max_cycles or 0),
        advance=getattr(args, "advance", False) is True,
        force_lock=bool(args.force_lock),
        watch=bool(args.watch),
        run_ids=_flatten_run_ids(getattr(args, "run_id", []) or []),
        background_launch_id=str(getattr(args, "background_launch_id", "") or "").strip(),
    )


def _configured_int(value: object, default: int) -> int:
    if value is not None:
        return int(value)
    return int(default or 0)


def _configured_float(value: object, default: float) -> float:
    if value is not None:
        return float(value)
    return float(default or 0.0)


def _dispatch_params(options: SubagentsDispatchOptions) -> DispatchParams:
    return DispatchParams(
        execution_plan=DispatchExecutionPlan.from_parts(
            mutate_state=options.mutate_state,
            start_runners=options.start_runners,
            max_runners=options.max_runners,
        ),
        planner=options.planner,
        workflow_mode=options.workflow_mode,
        limit=options.limit,
        reviewer=options.reviewer,
        note=options.note,
        runner_instruction=options.instruction,
        max_cards=options.max_cards,
        probe=options.probe,
        take_over_by=options.take_over_by,
        locked_files=options.locked_files,
        include_run_ids=options.run_ids or None,
        background_launch_id=options.background_launch_id,
    )


def _watch_params(options: SubagentsDispatchOptions) -> WatchParams:
    return WatchParams(
        **_dispatch_params(options).__dict__,
        interval=options.interval,
        max_cycles=options.max_cycles,
        advance=options.advance,
        force_lock=options.force_lock,
    )


def _flatten_run_ids(values: list[object]) -> list[str]:
    run_ids: list[str] = []
    for value in values:
        items = (item.strip() for item in str(value or "").replace(",", " ").split())
        run_ids.extend(item for item in items if item and item not in run_ids)
    return run_ids


def _print_watch_report(agent, report, options: SubagentsDispatchOptions) -> None:
    mode = "apply" if options.mutate_state else "dry-run"
    print("SUBAGENT DISPATCH WATCH")
    print(
        f"mode={mode} planner={options.planner} start_runners={options.start_runners} "
        f"advance={options.advance} "
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
    mode = "apply" if options.mutate_state else "dry-run"
    print("SUBAGENT DISPATCH")
    print(
        f"mode={mode} planner={options.planner} start_runners={options.start_runners} "
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
    if options.mutate_state:
        print(f"审计日志: {ws / 'subagent_dispatch_log.jsonl'}")
        print(f"审计日志: {ws / 'DISPATCH_LOG.md'}")
    if options.planner:
        print(f"planner: {ws / 'parent_planner_report.json'}")
        print(f"planner: {ws / 'PARENT_PLANNER.md'}")


def cmd_subagents_dispatch(args) -> int:

    agent = make_agent(args)
    options = _subagents_dispatch_options(args, agent=agent)
    if options.start_runners and not options.mutate_state:
        print("--start-runners 必须和 --apply 一起使用。", file=sys.stderr)
        return 2

    capability_config = load_capability_config(args.capability_config)
    agent.capability_config_path = args.capability_config
    router = make_capability_router(agent, capability_config, args.skill_dir)
    if options.watch:
        if not options.advance and (options.start_runners or options.planner):
            print("--watch 下 planner/runner 推进需要显式加 --advance。", file=sys.stderr)
            return 2
        try:
            report = agent.watch_subagents(router, capability_config, params=_watch_params(options))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        _print_watch_report(agent, report, options)
        return 0

    _print_background_launch_report(mark_background_launch(agent, options, BackgroundLaunchUpdate("running")))
    try:
        report = agent.dispatch_subagents(router, capability_config, params=_dispatch_params(options))
    except Exception as exc:
        _print_background_launch_report(
            mark_background_launch(agent, options, BackgroundLaunchUpdate("failed", f"{type(exc).__name__}: {exc}"))
        )
        raise
    _print_background_launch_report(mark_background_launch(agent, options, BackgroundLaunchUpdate("finished")))
    _print_dispatch_report(agent, report, options)
    return 0


def _print_background_launch_report(report) -> None:
    if getattr(report, "ok", True):
        return
    for error in [*getattr(report, "load_errors", []), *getattr(report, "save_errors", [])]:
        print(
            "background launch state update failed "
            f"run_id={error.get('run_id') or '-'} category={error.get('category') or '-'} "
            f"context={error.get('context') or '-'} error={error.get('message') or '-'}",
            file=sys.stderr,
        )


def cmd_subagents_workflow_plan(args) -> int:

    config = load_config(args.config)
    result = plan_workflow_for_goal(
        args.goal,
        constraints=WorkflowPlanConstraints(
            config=config,
            explicit_template_id=args.template_id or "",
            workflow_task_type=args.task_type or "",
            workflow_risk_tags=args.risk_tags or None,
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
    if payload["issues"]:
        print("issues=" + json.dumps(payload["issues"], ensure_ascii=False))
    if written_paths is not None:
        print(f"preview_json={written_paths['json']}")
        print(f"preview_markdown={written_paths['markdown']}")
    return 0


def cmd_subagent_run(args) -> int:

    agent = make_agent(args)
    result = _execute_subagent_run_cli(agent, args)
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


def _execute_subagent_run_cli(agent, args):
    if not args.execute:
        return agent.run_subagent(
            params=SubagentRunParams(
                run_id=args.run_id,
                instruction=args.instruction or "",
                dry_run=True,
                max_cards=int(args.max_cards or 0),
                probe=not args.no_probe,
            )
        )
    return _run_subagent_worker(
        RunSubagentWorkerParams(
            config=agent.config,
            root=agent.root,
            run_id=args.run_id,
            instruction=args.instruction or "",
            dry_run=False,
            max_cards=int(args.max_cards or 0),
            probe=not args.no_probe,
            retry_reason="",
            timeout_seconds=_cli_subagent_run_timeout(agent, args.run_id),
            local_store=getattr(agent, "local_store", None),
            backend_override=getattr(agent, "_subagent_worker_backend_override", None),
        )
    )


def _cli_subagent_run_timeout(agent, run_id: str) -> float:
    task = agent.subagents.load(run_id)
    runner_timeout_seconds, _, _ = resolve_runner_config(agent.config, 1)
    return get_task_timeout(task, runner_timeout_seconds, agent.config)
