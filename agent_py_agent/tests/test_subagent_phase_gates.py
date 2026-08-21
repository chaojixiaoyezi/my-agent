"""LLM: focused tests for hierarchy dispatch behavior discovered by real E2E.

模块用途: 验证 root 可按任务混合直派 worker/leaf；旧阶段/依赖硬门已删除。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchContext
from agent_py_agent.agent.agent_core.orchestration.dispatch.progress_payload import (
    _progress_payload,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.runner_batches import (
    _runner_candidates_for_context,
)
from agent_py_agent.agent.agent_core.runner.dispatch import _dispatch_runner_candidates
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


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
    )


def _dispatch_ctx(include_run_ids: list[str], max_runners: int = 3) -> DispatchContext:
    return DispatchContext(
        cfg=SimpleNamespace(),
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


def _attach_tmp_workspace(tmp_path, tasks) -> None:
    for task in tasks:
        task.allowed_write_roots = [str(tmp_path)]
        task.task_dir = str(tmp_path / ".my-agent" / "subagents" / task.id)


def test_root_with_coordinators_can_still_create_direct_leaf(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(goal="root delegates", thought="plan", plan=["plan"], role="coordinator")
    first = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(goal="auth coordinator", agent_name="auth-coordinator", role="coordinator")
            ],
            apply=True,
        )
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(goal="write auth page", agent_name="auth-leaf", role="worker")
            ],
            apply=True,
        )
    )

    assert first.created_run_ids
    assert result.blocked is False
    assert result.created_run_ids


def test_runner_candidates_keep_creation_order_without_hidden_role_phase():
    tasks = [
        _runner_task("auth", "coordinator", "auth-coordinator", "write auth pages"),
        _runner_task("catalog", "coordinator", "catalog-coordinator", "write catalog pages"),
        _runner_task("quality", "coordinator", "quality-coordinator", "quality check all pages"),
    ]

    selected = _dispatch_runner_candidates(tasks, max_runners=4)

    assert [task.id for task in selected] == ["auth", "catalog", "quality"]


def test_runner_selection_does_not_hide_requested_quality_roles():
    inherited_contract = (
        "父级要求至少创建 tester / bug_finder；"
        "当前 coordinator 先创建下一层 worker，不要自己做 QA。"
    )
    tasks = [
        _runner_task("qa-test", "tester", "小傻妞-tester", "test build when ready"),
        _runner_task("qa-bug", "bug_finder", "小傻妞-bug_finder", "find bugs when ready"),
        _runner_task("coord", "coordinator", "小傻妞-coord", inherited_contract),
    ]

    selected = _dispatch_runner_candidates(tasks, max_runners=4)

    assert [task.id for task in selected] == ["qa-test", "qa-bug", "coord"]


def test_explicit_run_ids_keep_mixed_worker_and_coordinator_targets():
    tasks = [
        _runner_task("market", "worker", "小傻妞-市场环境"),
        _runner_task("competition", "worker", "小傻妞-竞争格局"),
        _runner_task("strategy", "coordinator", "小傻妞-进入策略"),
    ]
    ctx = DispatchContext(
        cfg=SimpleNamespace(),
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


def test_explicit_run_ids_do_not_wait_for_missing_input_refs(tmp_path):
    data = _runner_task("collect", "worker", "小傻妞-数据", "生成 data/weekly_data.json")
    report = _runner_task("report", "worker", "小傻妞-报告", "读取 data/weekly_data.json，生成 final_report.md")
    data.attributes = {"output_refs": ["data/weekly_data.json"]}
    report.context_manifest = SimpleNamespace(required_read_paths=["data/weekly_data.json"])
    _attach_tmp_workspace(tmp_path, [data, report])
    ctx = _dispatch_ctx(["collect", "report"], max_runners=2)

    selected = _runner_candidates_for_context([data, report], ctx, runner_max_attempts=1)

    assert [task.id for task in selected] == ["collect", "report"]


def test_explicit_run_ids_keep_named_upstream_output_refs_in_same_wave(tmp_path):
    collect = _runner_task("collect", "worker", "小傻妞-数据搜集", "输出到 data/source_data.md")
    analysis = _runner_task(
        "analysis",
        "worker",
        "小傻妞-核验翻译",
        "接收小傻妞-数据搜集的输出 data/source_data.md，输出到 data/source_analysis.md",
    )
    report = _runner_task(
        "report",
        "worker",
        "小傻妞-生成报告",
        "读取小傻妞-核验翻译的输出 data/source_analysis.md，并生成 xlsx/final_report.md",
    )
    collect.attributes = {"output_refs": ["data/source_data.md"]}
    analysis.attributes = {"output_refs": ["data/source_analysis.md"]}
    analysis.context_manifest = SimpleNamespace(required_read_paths=["data/source_data.md"])
    report.context_manifest = SimpleNamespace(required_read_paths=["data/source_analysis.md"])
    _attach_tmp_workspace(tmp_path, [collect, analysis, report])
    ctx = _dispatch_ctx(["report", "analysis", "collect"])

    selected = _runner_candidates_for_context([report, analysis, collect], ctx, runner_max_attempts=1)

    assert [task.id for task in selected] == ["report", "analysis", "collect"]


def test_progress_payload_surfaces_blocked_children():
    tasks = [
        _runner_task("done", "worker"),
        _runner_task("blocked", "worker"),
    ]
    tasks[0].status = "DONE"
    tasks[0].verification_status = "VERIFIED"
    tasks[1].status = "BLOCKED"
    tasks[1].verification_status = "FAILED"

    payload = _progress_payload("parent", tasks)["direct_children"]

    assert payload["has_unfinished"] is False
    assert payload["has_failures"] is True
    assert payload["failed_run_ids"] == ["blocked"]
