"""LLM: focused tests for hierarchy phase gates discovered by real Stage7 E2E.

模块用途: 验证 root 可按任务混合直派 worker/leaf，并验证 quality/test runner 不抢在生产线前执行。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.dispatch_params import DispatchContext
from agent_py_agent.agent.agent_core.dispatch_runner_batches import _runner_candidates_for_context
from agent_py_agent.agent.agent_core.orchestration_progress_payload import _progress_payload
from agent_py_agent.agent.agent_core.runner_dispatch import _dispatch_runner_candidates
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


# LLM: _runner_task builds dispatch candidate fixtures with all fields the runner gate reads.
# 函数用途: 构造 runner dispatch 候选任务，避免测试依赖完整持久化任务。
def _runner_task(run_id: str, role: str, agent_name: str = "", goal: str = ""):
    return SimpleNamespace(
        id=run_id,
        role=role,
        agent_name=agent_name,
        goal=goal,
        task_dir="",
        allowed_write_roots=[],
        context_manifest=SimpleNamespace(required_read_paths=[]),
        status="PLANNING",
        verification_status="UNVERIFIED",
        channel_status="OK",
        capability_requests=[],
        capability_gaps=[],
        failure_type="",
        runner_attempts=0,
        workflow_parent_run_id="",
        workflow_phase_id="",
        workflow_depends_on=[],
    )


# LLM: _dispatch_ctx builds runner selection context without repeating unrelated defaults.
# 函数用途: 让 phase-gate 测试只声明本例关心的 run_ids 和并发数量。
def _dispatch_ctx(include_run_ids: list[str], max_runners: int = 3) -> DispatchContext:
    return DispatchContext(
        cfg=SimpleNamespace(),
        normalized_workflow_mode="off",
        apply=True,
        planner=False,
        runner_instruction="",
        max_runners=max_runners,
        limit=20,
        reviewer="tester",
        note="",
        take_over_by="",
        locked_files=None,
        router=SimpleNamespace(),
        include_run_ids=include_run_ids,
    )


# LLM: _attach_tmp_workspace gives fixture tasks real roots for dependency-ref checks.
# 函数用途: 批量设置 allowed_write_roots 和 task_dir，避免每个测试重复路径样板。
def _attach_tmp_workspace(tmp_path, tasks) -> None:
    for task in tasks:
        task.allowed_write_roots = [str(tmp_path)]
        task.task_dir = str(tmp_path / ".my-agent" / "subagents" / task.id)


# LLM: test_root_with_coordinators_can_still_create_direct_leaf keeps dispatch policy flexible.
# 函数用途: root 已创建 coordinator 后，仍可为别的工作分支直接创建 leaf_worker；是否满足层级链路交给验收判断。
def test_root_with_coordinators_can_still_create_direct_leaf(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(goal="root delegates", thought="plan", plan=["plan"], role="coordinator")
    first = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(goal="auth coordinator", agent_name="auth-coordinator", role="coordinator")
            ],
            apply=True,
        )
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(goal="write auth page", agent_name="auth-leaf", role="leaf_worker")
            ],
            apply=True,
        )
    )

    assert first.created_run_ids
    assert result.blocked is False
    assert result.created_run_ids


# LLM: test_runner_candidates_defer_quality_until_producers_finish covers producer/QA phase order.
# 函数用途: 同一 dispatch 范围内有生产 coordinator 时，quality/test coordinator 先不进入执行候选。
def test_runner_candidates_defer_quality_until_producers_finish():
    tasks = [
        _runner_task("auth", "coordinator", "auth-coordinator", "write auth pages"),
        _runner_task("catalog", "coordinator", "catalog-coordinator", "write catalog pages"),
        _runner_task("quality", "coordinator", "quality-coordinator", "quality check all pages"),
    ]

    selected = _dispatch_runner_candidates(tasks, max_runners=4)

    assert [task.id for task in selected] == ["auth", "catalog"]


# LLM: test_runner_phase_ignores_inherited_qa_contract_for_plain_coordinator covers Stage7 R63.
# 函数用途: coordinator 的父级 goal 里有 tester/bug_finder/acceptor 要求时，仍先跑 coordinator，不让 QA 抢跑。
def test_runner_phase_ignores_inherited_qa_contract_for_plain_coordinator():
    inherited_contract = (
        "父级要求至少创建 tester / bug_finder / acceptor；"
        "当前 coordinator 先创建下一层 worker，不要自己做 QA。"
    )
    tasks = [
        _runner_task("qa-test", "tester", "小傻妞-tester", "test build when ready"),
        _runner_task("qa-bug", "bug_finder", "小傻妞-bug_finder", "find bugs when ready"),
        _runner_task("coord", "coordinator", "小傻妞-coord", inherited_contract),
    ]

    selected = _dispatch_runner_candidates(tasks, max_runners=4)

    assert [task.id for task in selected] == ["coord"]


# LLM: explicit run_ids must remain exact instead of being silently narrowed by phase gates.
# 函数用途: 覆盖 Task17 真实 E2E：root 明确传 3 个 run_ids 时，不能只因 coordinator 优先就只跑 1 个。
def test_explicit_run_ids_keep_mixed_worker_and_coordinator_targets():
    tasks = [
        _runner_task("market", "worker", "小傻妞-市场环境"),
        _runner_task("competition", "worker", "小傻妞-竞争格局"),
        _runner_task("strategy", "coordinator", "小傻妞-进入策略"),
    ]
    ctx = DispatchContext(
        cfg=SimpleNamespace(),
        normalized_workflow_mode="off",
        apply=True,
        planner=False,
        runner_instruction="",
        max_runners=3,
        limit=20,
        reviewer="tester",
        note="",
        take_over_by="",
        locked_files=None,
        router=SimpleNamespace(),
        include_run_ids=["market", "competition", "strategy"],
    )

    selected = _runner_candidates_for_context(tasks, ctx, runner_max_attempts=1)

    assert [task.id for task in selected] == ["market", "competition", "strategy"]


# LLM: explicit run_ids should not force downstream workers to start before their input refs exist.
# 函数用途: 覆盖 Task18 真实 E2E：读取 data/weekly_data.json 的下游任务不能和生成该文件的上游任务同轮抢跑。
def test_explicit_run_ids_wait_for_missing_input_refs(tmp_path):
    data = _runner_task("collect", "worker", "小傻妞-数据", "生成 data/weekly_data.json")
    report = _runner_task("report", "worker", "小傻妞-报告", "读取 data/weekly_data.json，生成 final_report.md")
    data.attributes = {"output_refs": ["data/weekly_data.json"]}
    report.context_manifest = SimpleNamespace(required_read_paths=["data/weekly_data.json"])
    _attach_tmp_workspace(tmp_path, [data, report])
    ctx = _dispatch_ctx(["collect", "report"], max_runners=2)

    selected = _runner_candidates_for_context([data, report], ctx, runner_max_attempts=1)

    assert [task.id for task in selected] == ["collect"]

    data.status = "DONE"
    data.verification_status = "VERIFIED"
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "weekly_data.json").write_text("{}", encoding="utf-8")
    selected = _runner_candidates_for_context([data, report], ctx, runner_max_attempts=1)

    assert [task.id for task in selected] == ["report"]


# LLM: natural sibling-output wording must still block downstream runners until the file exists.
# 函数用途: 覆盖 Task18 真实 E2E：“读取某代理的输出 data/x”应视为输入依赖，不是当前任务输出目标。
def test_explicit_run_ids_wait_for_named_upstream_output_refs(tmp_path):
    collect = _runner_task("collect", "worker", "小傻妞-数据搜集", "输出到 data/github_star_data.md")
    analysis = _runner_task(
        "analysis",
        "worker",
        "小傻妞-核验翻译",
        "接收小傻妞-数据搜集的输出 data/github_star_data.md，输出到 data/github_star_analysis.md",
    )
    report = _runner_task(
        "report",
        "worker",
        "小傻妞-生成报告",
        "读取小傻妞-核验翻译的输出 data/github_star_analysis.md，并生成 xlsx/final_report.md",
    )
    collect.attributes = {"output_refs": ["data/github_star_data.md"]}
    analysis.attributes = {"output_refs": ["data/github_star_analysis.md"]}
    analysis.context_manifest = SimpleNamespace(required_read_paths=["data/github_star_data.md"])
    report.context_manifest = SimpleNamespace(required_read_paths=["data/github_star_analysis.md"])
    _attach_tmp_workspace(tmp_path, [collect, analysis, report])
    ctx = _dispatch_ctx(["report", "analysis", "collect"])

    selected = _runner_candidates_for_context([report, analysis, collect], ctx, runner_max_attempts=1)

    assert [task.id for task in selected] == ["collect"]

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "github_star_data.md").write_text("data", encoding="utf-8")
    collect.status = "DONE"
    collect.verification_status = "VERIFIED"
    selected = _runner_candidates_for_context([report, analysis, collect], ctx, runner_max_attempts=1)

    assert [task.id for task in selected] == ["analysis"]


# LLM: explicit run_ids must still honor machine sibling dependencies.
# 函数用途: 覆盖 Task18 真实 E2E：即便模型一次传入全部 run_ids，下游也要等上游 run 完成。
def test_explicit_run_ids_wait_for_workflow_dependencies():
    collect = _runner_task("collect", "worker", "小傻妞-数据收集")
    report = _runner_task("report", "worker", "小傻妞-生成报告")
    for task in [collect, report]:
        task.workflow_parent_run_id = "batch-1"
        task.workflow_phase_id = task.id
    report.workflow_depends_on = ["collect"]
    ctx = DispatchContext(
        cfg=SimpleNamespace(),
        normalized_workflow_mode="off",
        apply=True,
        planner=False,
        runner_instruction="",
        max_runners=2,
        limit=20,
        reviewer="tester",
        note="",
        take_over_by="",
        locked_files=None,
        router=SimpleNamespace(),
        include_run_ids=["report", "collect"],
    )

    selected = _runner_candidates_for_context([report, collect], ctx, runner_max_attempts=1)

    assert [task.id for task in selected] == ["collect"]

    collect.status = "DONE"
    collect.verification_status = "VERIFIED"
    selected = _runner_candidates_for_context([report, collect], ctx, runner_max_attempts=1)

    assert [task.id for task in selected] == ["report"]


# LLM: explicit run_ids may omit already-finished upstream phases after the first wave.
# 函数用途: 覆盖真实流水线复测：第二轮只点名下游 run 时，依赖检查仍必须能看到全局已完成上游。
def test_explicit_run_ids_dependency_lookup_uses_all_visible_tasks():
    collect = _runner_task("collect", "worker", "小傻妞-数据收集")
    content = _runner_task("content", "worker", "小傻妞-内容编写")
    report = _runner_task("report", "worker", "小傻妞-生成报告")
    for task in [collect, content, report]:
        task.workflow_parent_run_id = "batch-1"
        task.workflow_phase_id = task.id
    collect.status = "DONE"
    collect.verification_status = "VERIFIED"
    content.workflow_depends_on = ["collect"]
    report.workflow_depends_on = ["collect", "content"]
    ctx = DispatchContext(
        cfg=SimpleNamespace(),
        normalized_workflow_mode="off",
        apply=True,
        planner=False,
        runner_instruction="",
        max_runners=2,
        limit=20,
        reviewer="tester",
        note="",
        take_over_by="",
        locked_files=None,
        router=SimpleNamespace(),
        include_run_ids=["content", "report"],
    )

    selected = _runner_candidates_for_context(
        [content, report],
        ctx,
        runner_max_attempts=1,
        dependency_tasks=[collect, content, report],
    )

    assert [task.id for task in selected] == ["content"]


# LLM: short required_read_paths should resolve to completed dependency artifact refs.
# 函数用途: 覆盖真实流水线复测：下游写 data_collection.md 短名时，能接上上游 run 目录里的同名 artifact。
def test_required_read_path_can_use_completed_dependency_artifact(tmp_path):
    collect = _runner_task("collect", "worker", "小傻妞-数据收集")
    content = _runner_task("content", "worker", "小傻妞-内容编写")
    artifact = tmp_path / "data" / "subagents" / "collect" / "data_collection.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("data", encoding="utf-8")
    for task in [collect, content]:
        task.workflow_parent_run_id = "batch-1"
        task.workflow_phase_id = task.id
        task.allowed_write_roots = [str(tmp_path)]
        task.task_dir = str(tmp_path / "data" / "subagents" / task.id)
    collect.status = "DONE"
    collect.verification_status = "VERIFIED"
    collect.artifact_refs = [str(artifact)]
    content.workflow_depends_on = ["collect"]
    content.context_manifest.required_read_paths = ["data_collection.md"]
    ctx = DispatchContext(
        cfg=SimpleNamespace(),
        normalized_workflow_mode="off",
        apply=True,
        planner=False,
        runner_instruction="",
        max_runners=1,
        limit=20,
        reviewer="tester",
        note="",
        take_over_by="",
        locked_files=None,
        router=SimpleNamespace(),
        include_run_ids=["content"],
    )

    selected = _runner_candidates_for_context(
        [content],
        ctx,
        runner_max_attempts=1,
        dependency_tasks=[collect, content],
    )

    assert [task.id for task in selected] == ["content"]


# LLM: workflow phase dependencies must be respected before broad role ordering.
# 函数用途: producer/critic/repair 同时存在时，只能先跑无依赖的 produce，不能让 repair 空转抢跑。
def test_runner_candidates_wait_for_workflow_depends_on_refs():
    produce = _runner_task("produce", "worker", "小小傻妞-produce")
    critic = _runner_task("critic", "review", "小小傻妞-critic")
    repair = _runner_task("repair", "worker", "小小傻妞-repair")
    for task, phase, deps in [
        (produce, "produce", []),
        (critic, "critic", ["produce"]),
        (repair, "repair", ["critic"]),
    ]:
        task.workflow_parent_run_id = "parent-workflow"
        task.workflow_phase_id = phase
        task.workflow_depends_on = deps

    selected = _dispatch_runner_candidates([repair, critic, produce], max_runners=4)

    assert [task.id for task in selected] == ["produce"]

    produce.status = "AWAITING_ACCEPTANCE"
    produce.verification_status = "NEEDS_ACCEPTANCE"
    selected = _dispatch_runner_candidates([repair, critic, produce], max_runners=4)

    assert [task.id for task in selected] == ["critic"]


# LLM: test_progress_payload_surfaces_blocked_children covers parent recovery after a child fails.
# 函数用途: 直接 child 已失败/阻塞时，dispatch payload 必须给出可执行 run_ids，而不是假装没有下一步。
def test_progress_payload_surfaces_blocked_children():
    tasks = [
        _runner_task("done", "leaf_worker"),
        _runner_task("blocked", "leaf_worker"),
    ]
    tasks[0].status = "DONE"
    tasks[0].verification_status = "VERIFIED"
    tasks[1].status = "BLOCKED"
    tasks[1].verification_status = "FAILED"

    payload = _progress_payload("parent", tasks)["direct_children"]

    assert payload["needs_more_dispatch"] is False
    assert payload["needs_recovery"] is True
    assert payload["recovery_run_ids"] == ["blocked"]
