# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""routes scenario-test CLI cases and implements the happy-path full-flow scenario.

给人看的解释：
这个文件是 scenario-test 命令入口。
普通 happy path 在这里，几个更极端的专项 case 拆到了 scenario_cases.py。
"""

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any

from ..agent.capability_config import load_capability_config
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


# LLM: print_dispatch_report 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def print_dispatch_report(report) -> None:

    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        run = record.run_id or "global"
        print(
            f"- [{status}] {record.step}/{record.action} run={run} "
            f"applied={record.applied} :: {record.message}"
        )


# LLM: _cmd_scenario_validate_args 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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


# LLM: _cmd_scenario_happy_path 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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
    _cmd_scenario_verify_files(agent, args, paths, final_ok)
    return 0 if final_ok else 2


# LLM: ScenarioDispatchRequest 是scenario CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class ScenarioDispatchRequest:
    agent: Any
    args: Any
    paths: Any
    created_via: str
    gateway_payload: dict[str, object]


# LLM: _cmd_scenario_dispatch 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _cmd_scenario_dispatch(request: ScenarioDispatchRequest):
    agent = request.agent
    args = request.args
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
    return final_ok


# LLM: _cmd_scenario_verify_files 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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


# LLM: cmd_scenario_test 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
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


# LLM: _scenario_case_runners 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _scenario_case_runners():
    return {
        "verification": run_scenario_verification_case,
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


# LLM: run_scenario_suite 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
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
