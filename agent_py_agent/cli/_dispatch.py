# LLM: 内部启动输入先校验再创建宿主；命令行只运输身份，最终执行权仍由原 RuntimeDB 裁决。
# 模块用途: 把 CLI 调度和执行请求接入正式运行链，拒绝缺失或冲突的后台启动参数。
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
from ..agent.capability.config import load_capability_config
from .common import make_agent, make_capability_router
from .dispatch_background import BackgroundLaunchUpdate, mark_background_launch
from .models import SubagentsDispatchOptions


# LLM: CLI 固定身份与可配置默认值分别解析；不得从当前数据库补充遗漏的宿主字段。
# 函数用途: 把命令行输入变成单次派工选项，严格保留原执行轮映射。
def _subagents_dispatch_options(args, agent=None) -> SubagentsDispatchOptions:
    config = getattr(agent, "config", None)
    policy = DispatchRuntimePolicy.from_config(config)
    return SubagentsDispatchOptions(
        mutate_state=bool(args.apply),
        start_runners=bool(args.start_runners),
        planner=bool(args.planner),
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
        expected_attempt_ids=_expected_attempt_ids(args),
    )


def _configured_int(value: object, default: int) -> int:
    if value is not None:
        return int(value)
    return int(default or 0)


def _configured_float(value: object, default: float) -> float:
    if value is not None:
        return float(value)
    return float(default or 0.0)


# LLM: 原启动身份直接传入派工 DTO，不写入 note/instruction 等模型上下文。
# 函数用途: 让 CLI 与进程内后台派工使用相同的执行参数。
def _dispatch_params(options: SubagentsDispatchOptions) -> DispatchParams:
    return DispatchParams(
        execution_plan=DispatchExecutionPlan.from_parts(
            mutate_state=options.mutate_state,
            start_runners=options.start_runners,
            max_runners=options.max_runners,
        ),
        planner=options.planner,
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
        expected_attempt_ids=dict(options.expected_attempt_ids) if options.expected_attempt_ids is not None else None,
    )


# LLM: 纯解析先于 agent 构造；重复/缺项/越界与 watch 混用均拒绝。空 attempt 只声明 unmanaged，managed 在接纳处拒绝。
# 函数用途: 校验内部启动参数的完整对应关系，避免丢参数后变成全树调度或重新领取当前轮。
def _expected_attempt_ids(args) -> dict[str, str] | None:
    pairs = getattr(args, "expected_attempt", None)
    launch_id = getattr(args, "background_launch_id", "")
    if pairs is None:
        if launch_id:
            raise ValueError("后台启动缺少 --expected-attempt 身份")
        return None
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("--expected-attempt 必须提供运行编号和执行轮编号")
    result = {}
    for pair in pairs:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ValueError("--expected-attempt 必须成对提供")
        run_id, attempt_id = pair
        if not isinstance(run_id, str) or not run_id or run_id != run_id.strip():
            raise ValueError("启动运行编号必须是规范的非空字符串")
        if not isinstance(attempt_id, str) or attempt_id != attempt_id.strip():
            raise ValueError("启动执行轮编号必须是规范字符串")
        if run_id in result:
            raise ValueError("同一启动运行编号不能重复声明")
        result[run_id] = attempt_id
    if set(result) != set(_flatten_run_ids(getattr(args, "run_id", []) or [])):
        raise ValueError("启动身份必须与 --run-id 范围逐项对应")
    if getattr(args, "watch", False) or not args.apply or not args.start_runners:
        raise ValueError("固定启动身份只用于 --apply --start-runners 单次派工")
    return result


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


# LLM: 内部参数先纯校验再创建依赖；条件 marker 失败不进入 dispatch，最终 worker 仍须独立精确激活。
# 函数用途: 执行 CLI 调度，在启动身份无效时明确报错而不推进任务。
def cmd_subagents_dispatch(args) -> int:
    try:
        _expected_attempt_ids(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
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

    launch_report = mark_background_launch(agent, options, BackgroundLaunchUpdate("running"))
    _print_background_launch_report(launch_report)
    if not launch_report.ok:
        _print_background_launch_report(mark_background_launch(
            agent, options, BackgroundLaunchUpdate("failed", "启动标记未全部确认，派工未执行"),
        ))
        return 2
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
