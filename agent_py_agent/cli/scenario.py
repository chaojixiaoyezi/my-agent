from __future__ import annotations

"""LLM: routes scenario-test CLI cases and implements the happy-path full-flow scenario.

给人看的解释：
这个文件是 scenario-test 命令入口。
普通 happy path 在这里，几个更极端的专项 case 拆到了 scenario_cases.py。
"""

import argparse
import json
import sys

from ..agent.capability_config import load_capability_config
from .common import make_capability_router
from .scenario_cases import (
    run_scenario_gateway_cross_day_resume_case,
    run_scenario_gateway_restart_case,
    run_scenario_gateway_stale_lease_case,
    run_scenario_parent_subagent_cross_day_resume_case,
    run_scenario_runner_retry_case,
    run_scenario_structured_repair_case,
    run_scenario_verification_case,
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
    scenario_tasks_verified,
    write_scenario_summary,
)


def print_dispatch_report(report) -> None:
    """LLM: render a scenario dispatch report in a compact human-readable form.

    给人看的解释：
    场景测试每轮 dispatch 都会产生很多记录。
    这里把每条记录压成一行，方便人快速看出哪一步失败。
    """

    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        run = record.run_id or "global"
        print(
            f"- [{status}] {record.step}/{record.action} run={run} "
            f"applied={record.applied} :: {record.message}"
        )


def cmd_scenario_test(args) -> int:
    """跑一轮可观察、隔离的真实任务全流程。"""

    if args.case == "all":
        return run_scenario_suite(args)
    if args.case == "verification":
        return run_scenario_verification_case(args)
    if args.case == "gateway-restart":
        return run_scenario_gateway_restart_case(args)
    if args.case == "gateway-cross-day-resume":
        return run_scenario_gateway_cross_day_resume_case(args)
    if args.case == "gateway-stale-lease":
        return run_scenario_gateway_stale_lease_case(args)
    if args.case == "parent-subagent-cross-day-resume":
        return run_scenario_parent_subagent_cross_day_resume_case(args)
    if args.case == "structured-repair":
        return run_scenario_structured_repair_case(args)
    if args.case == "runner-retry":
        return run_scenario_runner_retry_case(args)

    if args.count <= 0:
        print("--count 必须大于 0。", file=sys.stderr)
        return 2
    if args.max_runners <= 0 and not args.dry_run:
        print("--max-runners 必须大于 0；如果只想预览，请加 --dry-run。", file=sys.stderr)
        return 2
    if args.max_cycles <= 0:
        print("--max-cycles 必须大于 0。", file=sys.stderr)
        return 2

    paths = create_scenario_workspace(args)
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

    print_scenario_step(3, "父代理调度 runner 和验收")
    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)
    dispatch_summaries: list[dict[str, object]] = []
    final_ok = False
    for cycle in range(1, args.max_cycles + 1):
        print(f"\n--- dispatch cycle {cycle}/{args.max_cycles} ---")
        report = agent.dispatch_subagents(
            router,
            capability_config,
            apply=True,
            execute_runners=not args.dry_run,
            planner=args.planner,
            max_runners=args.max_runners,
            limit=0,
            reviewer="scenario-test",
            note="isolated full-flow scenario test",
            runner_instruction=build_scenario_runner_instruction(),
            max_cards=0,
            probe=True,
        )
        dispatch_summaries.append(
            {
                "cycle": cycle,
                "summary": report.summary,
                "record_count": len(report.records),
                "ok": all(item.ok for item in report.records),
            }
        )
        print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
        for record in report.records:
            status = "OK" if record.ok else "FAIL"
            run = record.run_id or "global"
            print(
                f"- [{status}] {record.step}/{record.action} run={run} "
                f"applied={record.applied} :: {record.message}"
            )
        print_scenario_board(agent, limit=args.count + 5)
        final_ok = scenario_tasks_verified(agent, args.count)
        if final_ok:
            break

    print_scenario_step(4, "核对隔离文件和最终报告")
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
            "created_via": created_via,
            "gateway": gateway_payload,
            "dispatch": dispatch_summaries,
            "report_files": [str(item) for item in report_files],
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def run_scenario_suite(args) -> int:
    """连续运行一组隔离场景。"""

    # Keep all cheap deterministic recovery cases before the happy path, which may call a real model.
    cases = [
        "verification",
        "gateway-restart",
        "gateway-cross-day-resume",
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
