from __future__ import annotations

"""LLM: implements structured-repair and runner-retry scenario tests with their backend stubs.

给人看的解释：
这个文件包含结构化输出修复和 runner 重试的极端场景测试。
验证模型输出损坏时系统能自动修复，临时失败时系统能自动重试。
"""

import json
from dataclasses import dataclass
from pathlib import Path

from ...agent.capability_config import load_capability_config
from ..common import make_capability_router
from ..scenario_utils import (
    create_scenario_workspace,
    install_scenario_backend,
    load_scenario_agent,
    print_scenario_step,
    write_scenario_summary,
)
from .repair_retry_backends import ScenarioRetryBackend, ScenarioStructuredRepairBackend


@dataclass(frozen=True)
class StructuredRepairVerifyRequest:
    backend: object
    loaded: object
    runner: dict
    output: dict
    report: object


@dataclass(frozen=True)
class DispatchRoundRequest:
    agent: object
    router: object
    capability_config: object
    reviewer: str
    note: str


@dataclass(frozen=True)
class RunnerRetryVerifyRequest:
    first: object
    second: object
    after_first: object
    loaded: object
    backend: object


def print_dispatch_report(report) -> None:

    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        run = record.run_id or "global"
        print(
            f"- [{status}] {record.step}/{record.action} run={run} "
            f"applied={record.applied} :: {record.message}"
        )


def _structured_repair_setup(args):
    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=structured-repair")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    backend = ScenarioStructuredRepairBackend()
    install_scenario_backend(agent, backend)
    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)

    print_scenario_step(1, "创建会输出坏 JSON 的子代理工单")
    task = agent.subagents.create_run(
        goal="极端场景：runner 输出损坏的 SUBAGENT_RESULT，父代理应触发修复回合",
        thought="验证结构化输出坏掉时不会直接把任务丢成无法验收。",
        plan=["输出损坏结果块", "修复结构化结果", "父代理验收"],
        acceptance_checks=["必须触发 structured repair", "修复后必须有证据", "父代理必须验收通过"],
    )
    print(f"run_id={task.id}")
    return paths, agent, backend, capability_config, router, task


def _verify_structured_repair(request: StructuredRepairVerifyRequest):
    return (
        request.backend.calls == 2
        and request.loaded.status == "DONE"
        and request.loaded.verification_status == "VERIFIED"
        and request.runner.get("structured_output_found") is True
        and request.runner.get("structured_output_ok") is True
        and request.runner.get("structured_repair_attempted") is True
        and request.runner.get("structured_repair_ok") is True
        and request.output.get("structured_output", {}).get("repair_attempted") is True
        and any(item.step == "acceptance" and item.ok for item in request.report.records)
    )


def run_scenario_structured_repair_case(args) -> int:

    paths, agent, backend, capability_config, router, task = _structured_repair_setup(args)

    print_scenario_step(2, "执行 dispatch：runner 输出坏 JSON 后修复并验收")
    report = agent.dispatch_subagents(
        router, capability_config, apply=True, execute_runners=True,
        max_runners=1, probe=False,
        reviewer="scenario-structured-repair", note="structured output damage should be repaired",
    )
    print_dispatch_report(report)
    loaded = agent.subagents.load(task.id)
    runner = json.loads(Path(loaded.runner_result_json).read_text(encoding="utf-8"))
    output = json.loads(Path(loaded.output_json).read_text(encoding="utf-8"))
    print(
        f"final status={loaded.status} verify={loaded.verification_status} "
        f"backend_calls={backend.calls} repair_attempted={runner.get('structured_repair_attempted')} "
        f"repair_ok={runner.get('structured_repair_ok')}"
    )

    final_ok = _verify_structured_repair(StructuredRepairVerifyRequest(backend, loaded, runner, output, report))
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="structured repair passed" if final_ok else "structured repair failed",
        extra={
            "case": "structured-repair",
            "run_id": task.id,
            "backend_calls": backend.calls,
            "final_status": loaded.status,
            "structured_repair_attempted": runner.get("structured_repair_attempted"),
            "structured_repair_ok": runner.get("structured_repair_ok"),
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def _runner_retry_setup(args):
    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=runner-retry")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    backend = ScenarioRetryBackend()
    install_scenario_backend(agent, backend)
    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)

    print_scenario_step(1, "创建会先失败一次的子代理工单")
    task = agent.subagents.create_run(
        goal="极端场景：runner 第一次调用模型失败，下一轮 dispatch 应自动重试",
        thought="验证临时模型/接口错误不会让任务永久卡死。",
        plan=["第一次 runner 失败", "下一轮自动重试", "成功后父代理验收"],
        acceptance_checks=["第二次 runner 必须生成证据", "父代理必须验收通过"],
    )
    print(f"run_id={task.id}")
    return paths, agent, backend, capability_config, router, task


def _run_dispatch_round(request: DispatchRoundRequest):
    report = request.agent.dispatch_subagents(
        request.router, request.capability_config, apply=True, execute_runners=True,
        max_runners=1, probe=False, reviewer=request.reviewer, note=request.note,
    )
    print_dispatch_report(report)
    return report


def _verify_runner_retry(request: RunnerRetryVerifyRequest):
    first_runner = [item for item in request.first.records if item.step == "runner"]
    second_runner = [item for item in request.second.records if item.step == "runner"]
    return (
        first_runner
        and first_runner[0].action == "execute_runner"
        and not first_runner[0].ok
        and request.after_first.status == "BLOCKED"
        and request.after_first.failure_type == "runner_error"
        and request.after_first.runner_attempts == 1
        and second_runner
        and second_runner[0].action == "retry_runner"
        and second_runner[0].ok
        and request.loaded.status == "DONE"
        and request.loaded.verification_status == "VERIFIED"
        and request.loaded.runner_attempts == 2
        and request.backend.calls == 2
    )


def run_scenario_runner_retry_case(args) -> int:

    paths, agent, backend, capability_config, router, task = _runner_retry_setup(args)

    print_scenario_step(2, "第一轮 dispatch：模拟 runner 临时失败")
    first = _run_dispatch_round(
        DispatchRoundRequest(agent, router, capability_config, "scenario-runner-retry", "first attempt should fail")
    )
    after_first = agent.subagents.load(task.id)
    print(
        f"after_first status={after_first.status} failure_type={after_first.failure_type} "
        f"attempts={after_first.runner_attempts}"
    )

    print_scenario_step(3, "第二轮 dispatch：自动重试并验收")
    second = _run_dispatch_round(
        DispatchRoundRequest(agent, router, capability_config, "scenario-runner-retry", "retry should succeed")
    )
    loaded = agent.subagents.load(task.id)
    print(
        f"final status={loaded.status} verify={loaded.verification_status} "
        f"attempts={loaded.runner_attempts} backend_calls={backend.calls}"
    )

    final_ok = _verify_runner_retry(RunnerRetryVerifyRequest(first, second, after_first, loaded, backend))
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="runner retry passed" if final_ok else "runner retry failed",
        extra={
            "case": "runner-retry",
            "run_id": task.id,
            "backend_calls": backend.calls,
            "first_status": after_first.status,
            "final_status": loaded.status,
            "runner_attempts": loaded.runner_attempts,
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2
