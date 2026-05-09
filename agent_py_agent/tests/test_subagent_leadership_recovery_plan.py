"""Tests for dry-run batch coordinator leadership recovery planning."""

from __future__ import annotations

import time

from agent_py_agent.agent.capability_config import CapabilityConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import SubAgentLeadershipRecoveryPlanOptions


# LLM: _stale_coordinator marks a planning parent as overdue while keeping child links intact.
# 函数用途: 构造失联 coordinator 测试数据，用于验证批量接管计划不会直接改树。
def _stale_coordinator(manager: SubAgentManager, root_id: str, child_count: int):
    coordinator = manager.create_run(
        goal="stale coordinator",
        thought="waiting for children",
        plan=["coordinate"],
        role="coordinator",
        root_id=root_id,
    )
    children = [
        manager.create_run(
            goal=f"child {index}",
            thought="work",
            plan=["do work"],
            parent_id=coordinator.id,
            root_id=root_id,
            depth=coordinator.depth + 1,
        )
        for index in range(child_count)
    ]
    old = time.time() - 120
    coordinator = manager.load(coordinator.id)
    coordinator.status = "PLANNING"
    coordinator.runner_active_attempt_id = ""
    coordinator.heartbeat_at = old
    coordinator.updated_at = old
    coordinator.child_ids = [child.id for child in children]
    manager.save_hierarchy_links(coordinator)
    return coordinator, children


# LLM: _leader creates an available receiver under the same root tree.
# 函数用途: 构造可接管子树的候选 leader。
def _leader(manager: SubAgentManager, root_id: str, goal: str):
    leader = manager.create_run(
        goal=goal,
        thought="ready",
        plan=["take leadership"],
        role="coordinator",
        root_id=root_id,
    )
    leader.status = "RUNNING"
    leader.channel_status = "OK"
    manager.save(leader)
    return leader


def test_leadership_recovery_plan_splits_children_across_candidate_leaders(tmp_path):
    """A stale coordinator with many children should be split across leaders without mutating parents."""
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="orchestrate", plan=["split"], role="coordinator")
    coordinator, children = _stale_coordinator(manager, root.id, child_count=5)
    leader_a = _leader(manager, root.id, "leader a")
    leader_b = _leader(manager, root.id, "leader b")

    report = manager.plan_leadership_recovery(
        params=SubAgentLeadershipRecoveryPlanOptions(
            config=CapabilityConfig(subagent_heartbeat_timeout=1),
            root_id=root.id,
            leader_ids=[leader_a.id, leader_b.id],
            max_children_per_leader=3,
        )
    )

    assert report.summary["assigned_children"] == 5
    assert report.summary["unassigned_children"] == 0
    assert [(item.leader_id, len(item.child_ids)) for item in report.assignments] == [
        (leader_a.id, 3),
        (leader_b.id, 2),
    ]
    assert all(item.coordinator_id == coordinator.id for item in report.assignments)
    assert all(item.apply_supported is False for item in report.assignments)
    assert manager.load(children[0].id).parent_id == coordinator.id


def test_leadership_recovery_plan_reports_unassigned_children_when_capacity_runs_out(tmp_path):
    """If there is not enough leader capacity, the report must list remaining child IDs explicitly."""
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="orchestrate", plan=["split"], role="coordinator")
    _, children = _stale_coordinator(manager, root.id, child_count=5)
    leader = _leader(manager, root.id, "leader")

    report = manager.plan_leadership_recovery(
        params=SubAgentLeadershipRecoveryPlanOptions(
            config=CapabilityConfig(subagent_heartbeat_timeout=1),
            root_id=root.id,
            leader_ids=[leader.id],
            max_children_per_leader=2,
        )
    )

    assert report.summary["assigned_children"] == 2
    assert report.summary["unassigned_children"] == 3
    assert report.assignments[0].child_ids == [children[0].id, children[1].id]
    assert report.unassigned[0].child_ids == [child.id for child in children[2:]]
    assert report.unassigned[0].reason == "leader_capacity_exhausted"
