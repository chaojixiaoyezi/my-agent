# LLM: 本场景只模拟 runner 重试；入口同步 scenario 注册表，既有验收断言的债务见场景测试。
# 模块用途: 在隔离目录内模拟一次 runner 失败后的再次调度，不调用真实模型。
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ...agent.agent_core.orchestration.dispatch.params import DispatchExecutionPlan
from ...agent.capability.config import load_capability_config
from ...agent.conversation.task_run_closeout import settle_terminal_task_run
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


@dataclass(frozen=True)
class RunnerRetryReportEvidence:
    """离线场景的已核验证据输入；登记仍走原生命周期服务，不导入其内部实现。"""

    path: str
    kind: str = "file_check"
    summary: str = "宿主已核对本 run 原生 write_file 账本、登记产物及实际报告全文。"
    command: str = ""
    url: str = ""
    ok: bool = True


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
    # CLI 父任务保持活动；子轮失败不能把整棵 TaskRun 树提前关闭。
    parent_id = "scenario-runner-retry-parent"
    home = agent.home_paths
    thread = agent.conversation_store.threads.create(
        title="runner-retry CLI", workspace_root=str(paths.fixture_root), cwd=str(paths.fixture_root),
        owner_id=home.owner_id, owner_home=str(home.owner_home_dir),
    )
    agent.conversation_store.tasks.bind({
        "thread_id": thread.thread_id, "task_id": parent_id, "run_id": parent_id,
        "status": "active", "goal": "runner-retry CLI 父级验收",
    })
    agent.subagents.runtime_db.record_run_creation(
        owner_id=home.owner_id, goal="runner-retry CLI 父级验收", run_id=parent_id, role="main",
        conversation_task_id=parent_id, thread_id=thread.thread_id,
    )
    task = agent.subagents.create_run(
        goal="极端场景：runner 第一次调用模型失败，下一轮 dispatch 应自动重试",
        thought="验证临时模型/接口错误不会让任务永久卡死。",
        plan=["第一次 runner 失败", "下一轮自动重试", "成功后交回真实结果"],
        allowed_tools=["write_file"],
        acceptance_checks=["第二次 runner 必须生成证据", "父代理必须完成普通收口"],
        parent_id=parent_id, root_id=parent_id, depth=1,
    )
    backend.report_path = Path(task.agent_run_workspace_dir) / "scenario_outputs" / "runner-retry.md"
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


# LLM: 只核对正式 runner 状态、工具账本与磁盘产物；正文不能代替 VERIFIED。
# 函数用途: 核对真实失败、原生工具重试及宿主收口结果。
def _verify_runner_retry(request: RunnerRetryVerifyRequest):
    first_runner = [item for item in request.first.records if item.step == "runner"]
    second_runner = [item for item in request.second.records if item.step == "runner"]
    return (
        first_runner
        and first_runner[0].action == "execute_runner"
        and not first_runner[0].ok
        and request.after_first.status == "FAILED"
        and request.after_first.failure_type == "runner_error"
        and request.after_first.runner_attempts == 1
        and "scenario transient runner failure" in request.after_first.runner_last_error
        and second_runner
        and second_runner[0].action == "retry_runner"
        and second_runner[0].ok
        and request.loaded.status == "DONE"
        and request.loaded.verification_status == "VERIFIED"
        and request.loaded.runner_attempts == 2
        and request.backend.calls == 3
        and "write_file" in request.loaded.used_tools
        and str(request.backend.report_path) in request.loaded.artifact_refs
        and request.backend.report_path.is_file()
        and request.backend.report_path.read_text(encoding="utf-8") == request.backend.report_content
    )


# LLM: 只在正式 runner 已成功且本 run 的工具账本、产物与实际字节一致时登记父级验收；不信模型自报 tests/status。
# 函数用途: 由宿主真实检查报告，再经原生命周期入口保存验收证据。
def _verify_retry_report(agent, task, backend, report) -> None:
    runner_ok = any(item.step == "runner" and item.ok for item in report.records)
    if not (runner_ok and task.status == "DONE" and "write_file" in task.used_tools
            and str(backend.report_path) in task.artifact_refs and backend.report_path.is_file()
            and backend.report_path.read_text(encoding="utf-8") == backend.report_content):
        return
    agent.subagents.lifecycle.record_evidence(task.id, RunnerRetryReportEvidence(path=str(backend.report_path)))


# LLM: CLI 父级只按本次真实执行退出码结束自身；TaskRun 由正式会话收口核对全树，不替子 run 代写终态。
# 函数用途: 执行隔离重试演练，并在退出时结束本场景自己的父级运行与会话链接。
def run_scenario_runner_retry_case(args) -> int:
    paths, agent, backend, capability_config, router, task = _runner_retry_setup(args)
    exit_code = 2
    try:
        exit_code = _run_runner_retry_attempts(paths, agent, backend, capability_config, router, task)
        return exit_code
    finally:
        _settle_runner_retry_parent(agent, task, exit_code)


# LLM: 收口仅使用此隔离子工单的 canonical parent_id；不绕过整树 CAS，不以自然语言或子代理自报完成裁决。
# 函数用途: 记录 CLI 父级执行结束，关闭会话链接，再交正式入口核对并关闭 TaskRun。
def _settle_runner_retry_parent(agent, task, exit_code: int) -> None:
    repo = agent.subagents.runtime_db
    parent = repo.agent_run_for_run_id(task.parent_id)
    if parent is None:
        raise RuntimeError("scenario CLI parent runtime authority is missing")
    status = "done" if exit_code == 0 else "failed"
    result = repo.settle_agent_run(agent_run_id=parent["agent_run_id"], status=status)
    if not result.get("settled"):
        raise RuntimeError(f"scenario CLI parent closeout failed: {result}")
    link = agent.conversation_store.tasks.update_status({
        "task_id": task.parent_id, "expected_status": "active",
        "status": "completed" if exit_code == 0 else "failed",
    })
    if link is None:
        raise RuntimeError("scenario CLI parent task link closeout failed")
    settle_terminal_task_run(repo, agent.conversation_store, run_id=task.parent_id)
    tree = repo.get_task_run(parent["task_run_id"])
    if exit_code == 0 and (tree is None or tree["status"] != "done" or not tree["closed_at"]):
        raise RuntimeError("scenario CLI agent tree did not close")


# LLM: 保留两次正式 dispatch、工具账本与磁盘检查；失败不能改成成功或隐藏。
# 函数用途: 运行两轮重试、真实验收报告并写入诊断摘要。
def _run_runner_retry_attempts(paths, agent, backend, capability_config, router, task) -> int:

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
    _verify_retry_report(agent, loaded, backend, second)
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
            "first_error": after_first.runner_last_error,
            "final_status": loaded.status,
            "verification_status": loaded.verification_status,
            "runner_attempts": loaded.runner_attempts,
            "report_file": str(backend.report_path),
            "used_tools": loaded.used_tools,
            "artifact_registered": str(backend.report_path) in loaded.artifact_refs,
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2
