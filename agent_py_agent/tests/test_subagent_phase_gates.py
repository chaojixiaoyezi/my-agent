"""LLM: focused tests for hierarchy phase gates discovered by real Stage7 E2E.

模块用途: 验证 root 不绕过 coordinator 直建 leaf，并验证 quality/test runner 不抢在生产线前执行。
"""

from __future__ import annotations

from types import SimpleNamespace

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


# LLM: test_root_with_coordinators_cannot_bypass_into_leaf protects layered delegation.
# 函数用途: root 已创建 coordinator 后，不能再直接创建 leaf_worker，应让对应 coordinator 继续派下一层。
def test_root_with_coordinators_cannot_bypass_into_leaf(tmp_path):
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
    assert result.blocked is True
    assert result.reason.startswith("root_leaf_bypass_existing_coordinators")


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
