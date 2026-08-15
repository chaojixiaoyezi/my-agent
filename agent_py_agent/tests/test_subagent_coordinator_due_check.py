"""Coordinator due-check behavior for multi-level subagent trees."""

from __future__ import annotations

import time
from pathlib import Path

from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import SubAgentTask, WorkOrderValidation
from agent_py_agent.agent.subagents.services.board.service import SubAgentBoardService


class _BoardTestMixin(SubAgentManager, SubAgentBoardService):
    def __init__(self, workspace: Path):
        SubAgentManager.__init__(self, workspace=workspace)
        self._tasks = []

    def list_runs(self):
        return self._tasks

    def validate_work_order(self, run_id):
        return WorkOrderValidation(run_id=run_id, ok=True, missing=[], warnings=[])


def _coordinator_task(mixin: _BoardTestMixin, *, old: float) -> SubAgentTask:
    return SubAgentTask(
        id="root_coord",
        root_id="root_coord",
        goal="协调两个子任务",
        thought="waiting for children",
        plan=["dispatch", "collect"],
        status="PLANNING",
        verification_status="PENDING",
        channel_status="OK",
        depth=0,
        child_ids=["run_child"],
        created_at=old,
        updated_at=old,
        heartbeat_at=old,
        **mixin._build_work_order_paths("root_coord"),
    )


def _running_child_task(mixin: _BoardTestMixin, *, old: float) -> SubAgentTask:
    return SubAgentTask(
        id="run_child",
        root_id="root_coord",
        parent_id="root_coord",
        goal="真实执行子任务",
        thought="running",
        plan=["work"],
        status="RUNNING",
        verification_status="PENDING",
        channel_status="OK",
        depth=1,
        created_at=old,
        updated_at=old,
        heartbeat_at=old,
        **mixin._build_work_order_paths("run_child"),
    )


def _timed_out_parent_task(mixin: _BoardTestMixin, *, old: float) -> SubAgentTask:
    return SubAgentTask(
        id="root_timeout",
        root_id="root_timeout",
        goal="协调子任务后汇总",
        thought="parent runner timed out before finalizing child",
        plan=["dispatch child", "collect result"],
        status="TIMEOUT",
        verification_status="PENDING",
        channel_status="OK",
        depth=0,
        child_ids=["run_child"],
        created_at=old,
        updated_at=old,
        heartbeat_at=old,
        **mixin._build_work_order_paths("root_timeout"),
    )


def _timed_out_coordinator_task(mixin: _BoardTestMixin, *, old: float) -> SubAgentTask:
    return SubAgentTask(
        id="root_timeout_coord",
        root_id="root_timeout_coord",
        goal="协调子任务后汇总",
        thought="coordinator runner timed out before handing off children",
        plan=["dispatch child", "collect result"],
        role="coordinator",
        status="TIMEOUT",
        failure_type="runner_timeout",
        verification_status="PENDING",
        channel_status="OK",
        depth=0,
        child_ids=["run_child"],
        created_at=old,
        updated_at=old,
        heartbeat_at=old,
        **mixin._build_work_order_paths("root_timeout_coord"),
    )


def _planning_child_after_parent_timeout(mixin: _BoardTestMixin, *, old: float) -> SubAgentTask:
    return SubAgentTask(
        id="run_child",
        root_id="root_timeout",
        parent_id="root_timeout",
        goal="等待 leaf 执行 add(a,b)",
        thought="child was left in planning",
        plan=["dispatch leaf"],
        status="PLANNING",
        verification_status="PENDING",
        channel_status="OK",
        depth=1,
        created_at=old,
        updated_at=old,
        heartbeat_at=old,
        **mixin._build_work_order_paths("run_child"),
    )


def test_due_check_reports_stale_planning_coordinator_without_runner_timeout(tmp_path: Path):
    """有子任务的 PLANNING coordinator 心跳停滞时报告领导权问题，而不是 runner timeout。"""
    mixin = _BoardTestMixin(workspace=tmp_path)
    old = time.time() - 120
    mixin._tasks = [_coordinator_task(mixin, old=old), _running_child_task(mixin, old=old)]

    report = mixin.board.due_check(CapabilityConfig(subagent_heartbeat_timeout=1, subagent_run_timeout=1))

    timeout_issues = {
        (issue.run_id, issue.kind)
        for issue in report.issues
        if issue.kind in {"heartbeat_stale", "run_timeout"}
    }
    assert ("root_coord", "heartbeat_stale") not in timeout_issues
    assert ("root_coord", "run_timeout") not in timeout_issues
    assert ("run_child", "heartbeat_stale") in timeout_issues
    assert ("run_child", "run_timeout") in timeout_issues
    coordinator_issues = {
        (issue.run_id, issue.kind, issue.suggested_action)
        for issue in report.issues
        if issue.kind == "coordinator_heartbeat_stale"
    }
    assert coordinator_issues == {
        ("root_coord", "coordinator_heartbeat_stale", "recover_coordinator_leadership")
    }


def test_due_check_reports_unfinished_children_after_parent_timeout(tmp_path: Path):
    """父节点 TIMEOUT 但子节点未完成时，due-check 给出 refs-only 恢复提示。"""
    mixin = _BoardTestMixin(workspace=tmp_path)
    old = time.time() - 30
    mixin._tasks = [
        _timed_out_parent_task(mixin, old=old),
        _planning_child_after_parent_timeout(mixin, old=old),
    ]

    report = mixin.board.due_check(CapabilityConfig(subagent_heartbeat_timeout=3600, subagent_run_timeout=3600))

    issues = {(issue.run_id, issue.kind): issue for issue in report.issues}
    issue = issues[("root_timeout", "parent_timeout_with_unfinished_children")]
    assert issue.suggested_action == "recover_child_after_parent_timeout"
    assert "run_child:PLANNING" in issue.message


def test_due_check_dead_coordinator_prefers_leadership_recovery_over_status_timeout(tmp_path: Path):
    """死掉的 coordinator 带孩子时，不能降级成普通 status_timeout takeover。"""
    mixin = _BoardTestMixin(workspace=tmp_path)
    old = time.time() - 30
    mixin._tasks = [
        _timed_out_coordinator_task(mixin, old=old),
        _planning_child_after_parent_timeout(mixin, old=old),
    ]

    report = mixin.board.due_check(CapabilityConfig(subagent_heartbeat_timeout=3600, subagent_run_timeout=3600))

    issues = {(issue.run_id, issue.kind): issue for issue in report.issues}
    assert ("root_timeout_coord", "coordinator_needs_leadership_recovery") in issues
    assert ("root_timeout_coord", "status_timeout") not in issues
    issue = issues[("root_timeout_coord", "coordinator_needs_leadership_recovery")]
    assert issue.suggested_action == "recover_coordinator_leadership"
    assert "child_run:run_child" in issue.related_refs


def test_plan_actions_reports_stale_coordinator_leadership_recovery(tmp_path: Path):
    """动作计划把失联 coordinator 转成领导权恢复建议。"""
    mixin = _BoardTestMixin(workspace=tmp_path)
    mixin._tasks = [_coordinator_task(mixin, old=time.time() - 120)]

    report = mixin.board.plan_actions(CapabilityConfig(subagent_heartbeat_timeout=1))

    actions = {(action.run_id, action.action): action for action in report.actions}
    assert ("root_coord", "recover_coordinator_leadership") in actions
    assert actions[("root_coord", "recover_coordinator_leadership")].would_change_status_to == ""


def test_plan_actions_dead_coordinator_uses_leadership_recovery_not_takeover(tmp_path: Path):
    """动作计划必须给死 coordinator 生成 leadership recovery，而不是普通 takeover。"""
    mixin = _BoardTestMixin(workspace=tmp_path)
    old = time.time() - 30
    mixin._tasks = [
        _timed_out_coordinator_task(mixin, old=old),
        _planning_child_after_parent_timeout(mixin, old=old),
    ]

    report = mixin.board.plan_actions(CapabilityConfig(subagent_heartbeat_timeout=3600, subagent_run_timeout=3600))

    actions = {(action.run_id, action.action): action for action in report.actions}
    assert ("root_timeout_coord", "recover_coordinator_leadership") in actions
    assert ("root_timeout_coord", "takeover_or_reassign") not in actions
    assert actions[("root_timeout_coord", "recover_coordinator_leadership")].priority == 930


def test_plan_actions_reports_parent_timeout_child_recovery(tmp_path: Path):
    """动作计划把父超时、子未完成转成只读恢复建议，不直接改状态。"""
    mixin = _BoardTestMixin(workspace=tmp_path)
    old = time.time() - 30
    mixin._tasks = [
        _timed_out_parent_task(mixin, old=old),
        _planning_child_after_parent_timeout(mixin, old=old),
    ]

    report = mixin.board.plan_actions(CapabilityConfig(subagent_heartbeat_timeout=3600, subagent_run_timeout=3600))

    actions = {(action.run_id, action.action): action for action in report.actions}
    action = actions[("root_timeout", "recover_child_after_parent_timeout")]
    assert action.would_change_status_to == ""
    assert "parent_timeout_with_unfinished_children" in action.source_issue_kinds
    assert "unfinished_child:run_child:PLANNING" in action.rescue_context_refs
    assert "unfinished_child:run_child:PLANNING" in action.rescue_packet["recovery_entrypoints"]
    assert action.rescue_packet["auto_execute"] is False
