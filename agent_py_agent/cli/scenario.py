
from __future__ import annotations

"""routes scenario-test CLI cases and implements the happy-path full-flow scenario.

给人看的解释：
这个文件是 scenario-test 命令入口。
普通 happy path 在这里，几个更极端的专项 case 拆到了 scenario_cases.py。
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Any

from ..agent.agent_core.orchestration.dispatch.params import DispatchExecutionPlan
from ..agent.capability.config import load_capability_config
from .common import make_capability_router
from .scenario_cases import (
    run_scenario_gateway_cross_day_resume_case,
    run_scenario_gateway_delayed_response_case,
    run_scenario_gateway_multi_worker_case,
    run_scenario_gateway_processing_stop_case,
    run_scenario_gateway_restart_case,
    run_scenario_gateway_stale_lease_case,
    run_scenario_parent_subagent_cross_day_resume_case,
    run_scenario_real_model_recovery_case,
    run_scenario_real_model_recovery_multi_round_case,
    run_scenario_runner_retry_case,
    run_scenario_structured_repair_case,
)
from .scenario_utils import (
    build_scenario_prompt,
    build_scenario_runner_instruction,
    collect_scenario_report_files,
    create_scenario_workspace,
    load_scenario_agent,
    print_scenario_board,
    print_scenario_step,
    run_scenario_gateway_ask,
    scenario_tasks_active,
    scenario_tasks_verified,
    write_scenario_summary,
)


def print_dispatch_report(report) -> None:

    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        run = record.run_id or "global"
        print(
            f"- [{status}] {record.step}/{record.action} run={run} "
            f"applied={record.applied} :: {record.message}"
        )


def _cmd_scenario_validate_args(args) -> bool:
    if args.count <= 0:
        print("--count 必须大于 0。", file=sys.stderr)
        return False
    if args.max_runners <= 0 and not args.dry_run:
        print("--max-runners 必须大于 0；如果只想预览，请加 --dry-run。", file=sys.stderr)
        return False
    if args.max_cycles <= 0:
        print("--max-cycles 必须大于 0。", file=sys.stderr)
        return False
    return True


def _cmd_scenario_happy_path(args, paths):
    print("MY-AGENT SCENARIO TEST")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")
    print("")

    prompt = build_scenario_prompt(args.count)
    created_via = "direct"
    gateway_payload: dict[str, object] = {}
    if args.direct:
        print_scenario_step(1, "主代理聊天派工（direct agent.run）")
        agent = load_scenario_agent(paths.config)
        result = agent.run(prompt, save=False)
        print(result.response)
        print(f"[backend={result.backend}; tool_rounds={result.tool_rounds}]")
    else:
        print_scenario_step(1, "主代理聊天派工（gateway ask）")
        created_via = "gateway"
        gateway_payload = run_scenario_gateway_ask(paths, prompt, timeout=args.timeout)
        if not gateway_payload.get("ok"):
            write_scenario_summary(paths, ok=False, reason="gateway ask failed", extra={"gateway": gateway_payload})
            return 2

    agent = load_scenario_agent(paths.config)
    tasks = agent.subagents.list_runs()
    print_scenario_step(2, "检查派工结果")
    print_scenario_board(agent, limit=args.count + 5)
    if len(tasks) < args.count:
        reason = f"期望至少创建 {args.count} 个子代理，实际只有 {len(tasks)} 个。"
        print(f"SCENARIO_FAIL: {reason}", file=sys.stderr)
        write_scenario_summary(paths, ok=False, reason=reason, extra={"created_via": created_via})
        return 2

    final_ok = _cmd_scenario_dispatch(
        ScenarioDispatchRequest(agent, args, paths, created_via, gateway_payload)
    )
    verified_ok = _cmd_scenario_verify_files(agent, args, paths, final_ok)
    return 0 if verified_ok else 2


@dataclass(frozen=True)
class ScenarioDispatchRequest:
    agent: Any
    args: Any
    paths: Any
    created_via: str
    gateway_payload: dict[str, object]


def _cmd_scenario_dispatch(request: ScenarioDispatchRequest):
    agent = request.agent
    args = request.args
    print_scenario_step(3, "父代理调度 runner 和收口")
    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)
    deadline = _scenario_dispatch_deadline(args)
    final_ok = False
    for cycle in range(1, args.max_cycles + 1):
        print(f"\n--- dispatch cycle {cycle}/{args.max_cycles} ---")
        report = _run_scenario_dispatch_cycle(agent, args, router, capability_config)
        print_dispatch_report(report)
        print_scenario_board(agent, limit=args.count + 5)
        final_ok = scenario_tasks_verified(agent, args.count)
        if final_ok:
            break
        if cycle < args.max_cycles and scenario_tasks_active(agent, args.count):
            wait_seconds = _scenario_dispatch_wait_seconds(args)
            print(f"仍有子代理 RUNNING，等待 {wait_seconds:.1f}s 后继续检查。")
            time.sleep(wait_seconds)
    if not final_ok:
        final_ok = _cmd_scenario_observe_until_done(agent, args, deadline)
    return final_ok


def _cmd_scenario_observe_until_done(agent, args, deadline: float) -> bool:
    while scenario_tasks_active(agent, args.count) and _scenario_before_deadline(deadline):
        wait_seconds = min(
            _scenario_dispatch_wait_seconds(args),
            max(0.0, deadline - _scenario_now()),
        )
        if wait_seconds <= 0:
            break
        print(f"主动 dispatch 轮次已用完，仍有子代理未结束，继续观察 {wait_seconds:.1f}s。")
        time.sleep(wait_seconds)
        print_scenario_board(agent, limit=args.count + 5)
        if scenario_tasks_verified(agent, args.count):
            return True
    return scenario_tasks_verified(agent, args.count)


def _scenario_dispatch_deadline(args) -> float:
    return _scenario_now() + _scenario_dispatch_total_wait_seconds(args)


def _scenario_dispatch_total_wait_seconds(args) -> float:
    wait_seconds = _scenario_dispatch_wait_seconds(args)
    try:
        cycles = int(getattr(args, "max_cycles", 1) or 1)
    except (TypeError, ValueError):
        cycles = 1
    try:
        timeout = float(getattr(args, "timeout", 0) or 0)
    except (TypeError, ValueError):
        timeout = 0.0
    return max(wait_seconds * max(cycles, 1), timeout + 60.0, 60.0)


def _scenario_before_deadline(deadline: float) -> bool:
    return _scenario_now() < deadline


def _scenario_now() -> float:
    monotonic = getattr(time, "monotonic", None)
    if callable(monotonic):
        return float(monotonic())
    return 0.0


def _scenario_dispatch_wait_seconds(args) -> float:
    try:
        timeout = float(getattr(args, "timeout", 0) or 0)
    except (TypeError, ValueError):
        timeout = 0.0
    if timeout <= 0:
        return 10.0
    return min(30.0, max(5.0, timeout / 20.0))


def _run_scenario_dispatch_cycle(agent, args, router, capability_config):
    return agent.dispatch_subagents(
        router,
        capability_config,
        execution_plan=DispatchExecutionPlan.from_parts(
            mutate_state=True,
            start_runners=not args.dry_run,
            max_runners=args.max_runners,
        ),
        planner=args.planner,
        max_runners=args.max_runners,
        limit=0,
        reviewer="scenario-test",
        note="isolated full-flow scenario test",
        runner_instruction=build_scenario_runner_instruction(),
        max_cards=0,
        probe=True,
    )


def _cmd_scenario_verify_files(agent, args, paths, final_ok):
    report_files = collect_scenario_report_files(agent, paths.fixture_root, args.count)
    if args.dry_run:
        files_ok = True
        print("dry_run=true，跳过 runner 写文件检查。")
    else:
        files_ok = len(report_files) >= args.count
        print(f"scenario_output_files={len(report_files)}")
        for item in report_files:
            print(f"- {item}")
    final_ok = final_ok and files_ok
    reason = "scenario passed" if final_ok else "scenario did not reach verified state"
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason=reason,
        extra={
            "report_files": [str(item) for item in report_files],
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return final_ok


def scenario_case_choices() -> list[str]:
    """scenario-test --case 的合法值：happy/verification + runner 注册表 + all，单一事实来源。"""
    registered = [name for name in _scenario_case_runners() if name not in {"happy", "verification"}]
    return ["happy", "verification", *registered, "all"]


def cmd_scenario_test(args) -> int:

    if args.case == "all":
        return run_scenario_suite(args)
    case_runner = _scenario_case_runners().get(args.case)
    if case_runner is not None:
        return case_runner(args)

    if not _cmd_scenario_validate_args(args):
        return 2

    paths = create_scenario_workspace(args)
    return _cmd_scenario_happy_path(args, paths)


def _scenario_case_runners():
    return {
        "gateway-restart": run_scenario_gateway_restart_case,
        "gateway-cross-day-resume": run_scenario_gateway_cross_day_resume_case,
        "gateway-delayed-response": run_scenario_gateway_delayed_response_case,
        "gateway-multi-worker": run_scenario_gateway_multi_worker_case,
        "gateway-stale-lease": run_scenario_gateway_stale_lease_case,
        "gateway-processing-stop": run_scenario_gateway_processing_stop_case,
        "parent-subagent-cross-day-resume": run_scenario_parent_subagent_cross_day_resume_case,
        "real-model-recovery": run_scenario_real_model_recovery_case,
        "real-model-recovery-multi-round": run_scenario_real_model_recovery_multi_round_case,
        "structured-repair": run_scenario_structured_repair_case,
        "runner-retry": run_scenario_runner_retry_case,
    }


def run_scenario_suite(args) -> int:

    # Keep all cheap deterministic recovery cases before the happy path, which may call a real model.
    cases = [
        "verification",
        "gateway-restart",
        "gateway-cross-day-resume",
        "gateway-delayed-response",
        "gateway-multi-worker",
        "gateway-stale-lease",
        "parent-subagent-cross-day-resume",
        "structured-repair",
        "runner-retry",
        "happy",
    ]
    results: list[dict[str, object]] = []
    for case in cases:
        print(f"\n######## SCENARIO CASE: {case} ########")
        case_args = argparse.Namespace(**vars(args))
        case_args.case = case
        code = cmd_scenario_test(case_args)
        results.append({"case": case, "ok": code == 0, "exit_code": code})
        if code != 0:
            print("SCENARIO_SUITE_FAIL")
            print(json.dumps(results, ensure_ascii=False, indent=2))
            return code
    print("SCENARIO_SUITE_PASS")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0
