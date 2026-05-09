"""Tests for dry-run batch coordinator leadership recovery planning."""

from __future__ import annotations

import time

from agent_py_agent.agent.capability_config import CapabilityConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import (
    SubAgentLeadershipRecoveryApplyOptions,
    SubAgentLeadershipRecoveryPlanOptions,
)


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


# LLM: _make_child creates one child under a known parent and refreshes the parent's child link.
# 函数用途: 构造层级测试节点，减少大型树测试里的重复样板。
# LLM: _make_child creates a worker child under a known parent and refreshes the parent's child link.
# 函数用途: 构造普通层级测试节点，保持 helper 参数低于 code-size 高风险线。
def _make_child(manager: SubAgentManager, parent, root_id: str, goal: str):
    return manager.create_run(
        goal=goal,
        thought="work",
        plan=["do work"],
        parent_id=parent.id,
        root_id=root_id,
        depth=parent.depth + 1,
        role="worker",
    )


# LLM: _make_coordinator_child creates a coordinator node for large hierarchy recovery tests.
# 函数用途: 构造 coordinator 子节点，避免 _make_child 暴露 role 参数。
def _make_coordinator_child(manager: SubAgentManager, parent, root_id: str, goal: str):
    return manager.create_run(
        goal=goal,
        thought="coordinate",
        plan=["coordinate"],
        parent_id=parent.id,
        root_id=root_id,
        depth=parent.depth + 1,
        role="coordinator",
    )


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


def test_leadership_recovery_apply_reparents_only_requested_child_subset(tmp_path):
    """Subset apply should move only listed direct children and leave the old coordinator with leftovers."""
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="orchestrate", plan=["split"], role="coordinator")
    coordinator, children = _stale_coordinator(manager, root.id, child_count=4)
    leader = _leader(manager, root.id, "leader")

    report = manager.apply_leadership_recovery(
        params=SubAgentLeadershipRecoveryApplyOptions(
            root_id=root.id,
            coordinator_id=coordinator.id,
            leader_id=leader.id,
            child_ids=[children[0].id, children[1].id],
            apply=True,
        )
    )

    assert report.summary == {"total": 1, "ok": 1, "applied": 1, "moved_children": 2}
    assert report.records[0].ok is True
    assert report.records[0].applied is True
    assert report.records[0].moved_child_ids == [children[0].id, children[1].id]
    assert manager.load(children[0].id).parent_id == leader.id
    assert manager.load(children[1].id).parent_id == leader.id
    assert manager.load(children[2].id).parent_id == coordinator.id
    assert manager.load(coordinator.id).child_ids == [children[2].id, children[3].id]
    assert manager.load(leader.id).child_ids == [children[0].id, children[1].id]


def test_leadership_recovery_apply_blocks_non_direct_child_without_mutation(tmp_path):
    """Safety checks should reject stale or wrong child IDs before mutating hierarchy links."""
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="orchestrate", plan=["split"], role="coordinator")
    coordinator, children = _stale_coordinator(manager, root.id, child_count=2)
    leader = _leader(manager, root.id, "leader")
    outside = manager.create_run(goal="outside", thought="other", plan=["other"], root_id=root.id)

    report = manager.apply_leadership_recovery(
        params=SubAgentLeadershipRecoveryApplyOptions(
            root_id=root.id,
            coordinator_id=coordinator.id,
            leader_id=leader.id,
            child_ids=[children[0].id, outside.id],
            apply=True,
        )
    )

    assert report.summary == {"total": 1, "ok": 0, "applied": 0, "moved_children": 0}
    assert report.records[0].ok is False
    assert report.records[0].blocked_by == [f"child_not_direct:{outside.id}"]
    assert manager.load(children[0].id).parent_id == coordinator.id
    assert manager.load(leader.id).child_ids == []


def test_leadership_recovery_plan_treats_failed_parent_as_recovery_source(tmp_path):
    """If a replacement leader later fails, its children should be replannable under another leader."""
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="orchestrate", plan=["split"], role="coordinator")
    failed_leader = _leader(manager, root.id, "failed leader")
    children = [_make_child(manager, failed_leader, root.id, f"failed child {index}") for index in range(3)]
    failed_leader = manager.load(failed_leader.id)
    failed_leader.status = "TIMEOUT"
    failed_leader.failure_type = "runner_timeout"
    manager.save_hierarchy_links(failed_leader)
    replacement = _leader(manager, root.id, "replacement")

    report = manager.plan_leadership_recovery(
        params=SubAgentLeadershipRecoveryPlanOptions(
            config=CapabilityConfig(subagent_heartbeat_timeout=1),
            root_id=root.id,
            leader_ids=[replacement.id],
            max_children_per_leader=3,
        )
    )

    assert report.summary["failed_parent_nodes"] == 1
    assert report.assignments[0].coordinator_id == failed_leader.id
    assert report.assignments[0].leader_id == replacement.id
    assert report.assignments[0].child_ids == [child.id for child in children]


def test_leadership_recovery_plan_respects_existing_leader_capacity(tmp_path):
    """Existing leader children should consume capacity so recovery does not snowball onto one run."""
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="orchestrate", plan=["split"], role="coordinator")
    coordinator, children = _stale_coordinator(manager, root.id, child_count=3)
    leader = _leader(manager, root.id, "leader")
    _make_child(manager, leader, root.id, "existing one")
    _make_child(manager, leader, root.id, "existing two")

    report = manager.plan_leadership_recovery(
        params=SubAgentLeadershipRecoveryPlanOptions(
            config=CapabilityConfig(subagent_heartbeat_timeout=1),
            root_id=root.id,
            leader_ids=[leader.id],
            max_children_per_leader=3,
        )
    )

    assert report.summary["assigned_children"] == 1
    assert report.summary["unassigned_children"] == 2
    assert report.assignments[0].coordinator_id == coordinator.id
    assert report.assignments[0].child_ids == [children[0].id]
    assert report.unassigned[0].child_ids == [children[1].id, children[2].id]


def test_leadership_recovery_apply_large_tree_1_4_16_48(tmp_path):
    """Recovery apply should handle a 1 main / 4 child / 16 grandchild / 48 great-grandchild tree."""
    manager, root, coordinators, grandchildren, great_grandchildren, leaders = _large_recovery_tree(tmp_path)
    plan = manager.plan_leadership_recovery(
        params=SubAgentLeadershipRecoveryPlanOptions(
            config=CapabilityConfig(subagent_heartbeat_timeout=1),
            root_id=root.id,
            leader_ids=[leader.id for leader in leaders],
            max_children_per_leader=4,
        )
    )
    _apply_plan_assignments(manager, root.id, plan)

    assert plan.summary["assigned_children"] == 16
    assert plan.summary["unassigned_children"] == 0
    assert sum(len(manager.load(leader.id).child_ids) for leader in leaders) == 16
    assert all(manager.load(coordinator.id).status == "TAKEN_OVER" for coordinator in coordinators)
    assert len(great_grandchildren) == 48
    _assert_depths_follow_parents(manager, grandchildren)
    _assert_depths_follow_parents(manager, great_grandchildren)


# LLM: _large_recovery_tree creates the required 1/4/16/48 hierarchy fixture.
# 函数用途: 构造大树测试现场，保持测试主体短小并降低嵌套深度。
def _large_recovery_tree(tmp_path):
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="orchestrate", plan=["split"], role="coordinator")
    coordinators = [_make_coordinator_child(manager, root, root.id, f"coord {index}") for index in range(4)]
    grandchildren = []
    great_grandchildren = []
    for coordinator in coordinators:
        group_grandchildren, group_greats = _grandchild_group(manager, root.id, coordinator)
        grandchildren.extend(group_grandchildren)
        great_grandchildren.extend(group_greats)
        _mark_stale(manager, coordinator.id)
    leaders = [_leader(manager, root.id, f"leader {index}") for index in range(4)]
    return manager, root, coordinators, grandchildren, great_grandchildren, leaders


# LLM: _grandchild_group adds four grandchildren and three great-grandchildren under each.
# 函数用途: 构造大树的一组 4/12 后代，避免大型测试函数出现三层循环。
def _grandchild_group(manager, root_id: str, coordinator):
    grandchildren = []
    great_grandchildren = []
    for grand_index in range(4):
        grandchild = _make_child(manager, coordinator, root_id, f"{coordinator.id} grand {grand_index}")
        grandchildren.append(grandchild)
        great_grandchildren.extend(
            _make_child(manager, grandchild, root_id, f"{grandchild.id} great {great_index}")
            for great_index in range(3)
        )
    return grandchildren, great_grandchildren


# LLM: _mark_stale makes a coordinator eligible for leadership recovery planning.
# 函数用途: 将 coordinator 设置成无 active attempt 且 heartbeat 超时的 PLANNING 父节点。
def _mark_stale(manager: SubAgentManager, run_id: str) -> None:
    stale = manager.load(run_id)
    stale.status = "PLANNING"
    stale.runner_active_attempt_id = ""
    stale.heartbeat_at = time.time() - 120
    manager.save_hierarchy_links(stale)


# LLM: _apply_plan_assignments applies every planned child batch through the public manager bundle API.
# 函数用途: 用计划报告驱动真实分批 apply，模拟人工逐批确认后的执行路径。
def _apply_plan_assignments(manager: SubAgentManager, root_id: str, plan) -> None:
    for assignment in plan.assignments:
        manager.apply_leadership_recovery(
            params=SubAgentLeadershipRecoveryApplyOptions(
                root_id=root_id,
                coordinator_id=assignment.coordinator_id,
                leader_id=assignment.leader_id,
                child_ids=assignment.child_ids,
                apply=True,
            )
        )


# LLM: _assert_depths_follow_parents verifies recursive depth refresh without caring about exact leader ids.
# 函数用途: 校验每个节点 depth 都等于父节点 depth + 1。
def _assert_depths_follow_parents(manager: SubAgentManager, tasks: list) -> None:
    for task in tasks:
        loaded = manager.load(task.id)
        assert loaded.depth == manager.load(loaded.parent_id).depth + 1
