from __future__ import annotations

"""LLM: implements structured-repair and runner-retry scenario tests with their backend stubs.

给人看的解释：
这个文件包含结构化输出修复和 runner 重试的极端场景测试。
验证模型输出损坏时系统能自动修复，临时失败时系统能自动重试。
"""

import json
from pathlib import Path

from ...agent.backend import ModelResponse
from ...agent.capability_config import load_capability_config
from ..common import make_capability_router
from ..scenario_utils import (
    create_scenario_workspace,
    load_scenario_agent,
    print_scenario_step,
    write_scenario_summary,
)


def print_dispatch_report(report) -> None:
    """LLM: print a human-readable dispatch summary for scenario test output.

    新手说明:
    在场景测试里打印 dispatch 的摘要信息，方便看每一步执行结果。
    """

    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        run = record.run_id or "global"
        print(
            f"- [{status}] {record.step}/{record.action} run={run} "
            f"applied={record.applied} :: {record.message}"
        )


class ScenarioStructuredRepairBackend:
    """LLM: stub backend that emits a broken SUBAGENT_RESULT on first call, then a valid one on repair.

    新手说明:
    场景测试用后端——第一次输出坏的结构化结果块（JSON 不完整），
    第二次修复回合输出完整的可解析结构化结果。
    """

    name = "scenario_structured_repair_backend"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "我已经完成任务，但这次故意输出一个损坏的结构化结果块。\n"
                    "[SUBAGENT_RESULT]\n"
                    "{\n"
                    '  "status": "AWAITING_ACCEPTANCE",\n'
                    '  "summary": "这个 JSON 少了结尾，用来模拟模型输出损坏",\n'
                    '  "evidence": [\n'
                    '    {"kind": "note", "summary": "原始回复声称已有证据", "ok": true}\n'
                ),
                backend=self.name,
            )
        return ModelResponse(
            text=(
                "[SUBAGENT_RESULT]\n"
                "{\n"
                '  "status": "AWAITING_ACCEPTANCE",\n'
                '  "summary": "结构化输出损坏后已通过修复回合补齐。",\n'
                '  "used_tools": [],\n'
                '  "used_skills": [],\n'
                '  "evidence": [\n'
                '    {"kind": "note", "summary": "修复回合生成了可解析证据", "ok": true}\n'
                "  ],\n"
                '  "capability_requests": [],\n'
                '  "artifacts": [],\n'
                '  "tests": [\n'
                '    {"name": "structured repair", "command": "", "ok": true, "summary": "坏 JSON 已修复"}\n'
                "  ],\n"
                '  "patches": [],\n'
                '  "lessons": ["结构化输出损坏时先做格式修复，不新增事实"],\n'
                '  "next_actions": [],\n'
                '  "blocked_reason": "",\n'
                '  "failure_type": ""\n'
                "}\n"
                "[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
        )


def run_scenario_structured_repair_case(args) -> int:
    """LLM: verify that a broken structured output triggers a repair round and then passes acceptance.

    新手说明:
    让 runner 故意输出损坏的 JSON，验证系统会自动触发修复回合。
    修复后父代理应该能验收通过。
    """

    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=structured-repair")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    backend = ScenarioStructuredRepairBackend()
    agent.backend = backend
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

    print_scenario_step(2, "执行 dispatch：runner 输出坏 JSON 后修复并验收")
    report = agent.dispatch_subagents(
        router,
        capability_config,
        apply=True,
        execute_runners=True,
        max_runners=1,
        probe=False,
        reviewer="scenario-structured-repair",
        note="structured output damage should be repaired",
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

    final_ok = (
        backend.calls == 2
        and loaded.status == "DONE"
        and loaded.verification_status == "VERIFIED"
        and runner.get("structured_output_found") is True
        and runner.get("structured_output_ok") is True
        and runner.get("structured_repair_attempted") is True
        and runner.get("structured_repair_ok") is True
        and output.get("structured_output", {}).get("repair_attempted") is True
        and any(item.step == "acceptance" and item.ok for item in report.records)
    )
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


class ScenarioRetryBackend:
    """LLM: stub backend that fails on first generate, then returns a valid structured result on second call.

    新手说明:
    场景测试用后端——第一次调用抛出 RuntimeError 模拟临时失败，
    第二次调用返回可验收的结构化结果。
    """

    name = "scenario_retry_backend"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("scenario transient runner failure")
        return ModelResponse(
            text=(
                "[SUBAGENT_RESULT]\n"
                "{\n"
                '  "status": "AWAITING_ACCEPTANCE",\n'
                '  "summary": "runner 在第二次尝试中完成，已生成可验收证据。",\n'
                '  "used_tools": [],\n'
                '  "used_skills": [],\n'
                '  "evidence": [\n'
                '    {"kind": "note", "summary": "第二次 runner 尝试成功", "ok": true}\n'
                "  ],\n"
                '  "capability_requests": [],\n'
                '  "artifacts": [],\n'
                '  "tests": [\n'
                '    {"name": "runner retry", "command": "", "ok": true, "summary": "第二次尝试通过"}\n'
                "  ],\n"
                '  "patches": [],\n'
                '  "lessons": ["临时 runner 错误可以由父代理有限重试恢复"],\n'
                '  "next_actions": [],\n'
                '  "blocked_reason": "",\n'
                '  "failure_type": ""\n'
                "}\n"
                "[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
        )


def run_scenario_runner_retry_case(args) -> int:
    """LLM: verify that a transient runner failure is automatically retried on the next dispatch round.

    新手说明:
    让 runner 第一次执行失败，验证下一轮 dispatch 会自动重试。
    重试成功后父代理应该能验收通过。
    """

    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=runner-retry")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    backend = ScenarioRetryBackend()
    agent.backend = backend
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

    print_scenario_step(2, "第一轮 dispatch：模拟 runner 临时失败")
    first = agent.dispatch_subagents(
        router,
        capability_config,
        apply=True,
        execute_runners=True,
        max_runners=1,
        probe=False,
        reviewer="scenario-runner-retry",
        note="first attempt should fail",
    )
    print_dispatch_report(first)
    after_first = agent.subagents.load(task.id)
    print(
        f"after_first status={after_first.status} failure_type={after_first.failure_type} "
        f"attempts={after_first.runner_attempts}"
    )

    print_scenario_step(3, "第二轮 dispatch：自动重试并验收")
    second = agent.dispatch_subagents(
        router,
        capability_config,
        apply=True,
        execute_runners=True,
        max_runners=1,
        probe=False,
        reviewer="scenario-runner-retry",
        note="retry should succeed",
    )
    print_dispatch_report(second)
    loaded = agent.subagents.load(task.id)
    print(
        f"final status={loaded.status} verify={loaded.verification_status} "
        f"attempts={loaded.runner_attempts} backend_calls={backend.calls}"
    )

    first_runner = [item for item in first.records if item.step == "runner"]
    second_runner = [item for item in second.records if item.step == "runner"]
    final_ok = (
        first_runner
        and first_runner[0].action == "execute_runner"
        and not first_runner[0].ok
        and after_first.status == "BLOCKED"
        and after_first.failure_type == "runner_error"
        and after_first.runner_attempts == 1
        and second_runner
        and second_runner[0].action == "retry_runner"
        and second_runner[0].ok
        and loaded.status == "DONE"
        and loaded.verification_status == "VERIFIED"
        and loaded.runner_attempts == 2
        and backend.calls == 2
    )
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
