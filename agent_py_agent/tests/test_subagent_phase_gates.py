"""LLM: focused tests for hierarchy phase gates discovered by real Stage7 E2E.

模块用途: 验证 root 可按任务混合直派 worker/leaf，并验证 quality/test runner 不抢在生产线前执行。
"""

from __future__ import annotations

from types import SimpleNamespace

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
        status="PLANNING",
        verification_status="UNVERIFIED",
        channel_status="OK",
        capability_requests=[],
        capability_gaps=[],
        failure_type="",
        runner_attempts=0,
    )


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
