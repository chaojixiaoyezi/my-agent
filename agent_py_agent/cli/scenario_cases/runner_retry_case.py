# LLM: 本场景只模拟 runner 重试；入口同步 scenario 注册表，既有验收断言的债务见场景测试。
# 模块用途: 在隔离目录内模拟一次 runner 失败后的再次调度，不调用真实模型。
from __future__ import annotations

import json
from dataclasses import dataclass

from ...agent.agent_core.orchestration.dispatch.params import DispatchExecutionPlan
from ...agent.capability.config import load_capability_config
from ..common import make_capability_router
from ..scenario_utils import (
    create_scenario_workspace,
    install_scenario_backend,
    load_scenario_agent,
    print_scenario_step,
    write_scenario_summary,
)
from .runner_retry_backend import ScenarioRetryBackend


# LLM: 场景的单轮调度参数不增加执行权限，仍由正式 dispatcher 校验。
# 类用途: 携带模拟场景执行一轮调度所需的对象和记录说明。
@dataclass(frozen=True)
class DispatchRoundRequest:
    agent: object
    router: object
    capability_config: object
    reviewer: str
    note: str


# LLM: 保留现有重试场景断言的数据，不从模型正文推断状态。
# 类用途: 汇集两次调度的结果和持久状态，供场景判定使用。
@dataclass(frozen=True)
class RunnerRetryVerifyRequest:
    first: object
    second: object
    after_first: object
    loaded: object
    backend: object


# LLM: 此输出仅为诊断展示，不能回流成为运行事实。
# 函数用途: 把本轮调度记录打印到终端。
def print_dispatch_report(report) -> None:

    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        run = record.run_id or "global"
        print(
            f"- [{status}] {record.step}/{record.action} run={run} "
            f"applied={record.applied} :: {record.message}"
        )


# LLM: 仅在场景私有目录装配固定后端，配置和持久结果不能写入用户日常目录。
# 函数用途: 建立隔离工作区和模拟失败任务，写入本轮诊断文件。
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
        plan=["第一次 runner 失败", "下一轮自动重试", "成功后交回真实结果"],
        acceptance_checks=["第二次 runner 必须生成证据", "父代理必须完成普通收口"],
    )
    print(f"run_id={task.id}")
    return paths, agent, backend, capability_config, router, task


# LLM: 复用正式 dispatch，固定后端负责造错；运行会更新该隔离任务的账本。
# 函数用途: 推进一轮模拟任务并打印调度结果。
def _run_dispatch_round(request: DispatchRoundRequest):
    report = request.agent.dispatch_subagents(
        request.router,
        request.capability_config,
        execution_plan=DispatchExecutionPlan.from_parts(
            mutate_state=True,
            start_runners=True,
            max_runners=1,
        ),
        max_runners=1, probe=False, reviewer=request.reviewer, note=request.note,
    )
    print_dispatch_report(report)
    return report


# LLM: 保留已有严格条件及其已知失败；不得通过删断言让旧场景冒充验收通过。
# 函数用途: 核对模拟任务的重试次数和状态，返回场景判定。
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


# LLM: CLI 入口只运行离线替身；持久输出归隔离目录，当前断言债务在 test_scenario_gateway_resume 中保留。
# 函数用途: 执行两轮模拟调度、写诊断摘要，并用退出码报告结果。
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
