"""Coordinator due-check behavior for multi-level subagent trees."""

from __future__ import annotations

import time
from pathlib import Path

from agent_py_agent.agent.capability_config import CapabilityConfig
from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin
from agent_py_agent.agent.subagents.models import SubAgentTask, WorkOrderValidation


# LLM: _BoardTestMixin provides an in-memory run list for coordinator due-check tests.
# 类用途: 测试用 manager 替身，只保存任务列表并返回通过的工单校验。
class _BoardTestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
    def __init__(self, workspace: Path):
        SubAgentBaseMixin.__init__(self, workspace=workspace)
        self._tasks = []

    def list_runs(self):
        return self._tasks

    def validate_work_order(self, run_id):
        return WorkOrderValidation(run_id=run_id, ok=True, missing=[], warnings=[])


# LLM: _coordinator_task creates a stale planning parent with child ownership.
# 函数用途: 构造带 child_ids 的失联 coordinator，用于验证不会被普通 runner timeout 误伤。
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


# LLM: _running_child_task creates a stale active child under the coordinator.
# 函数用途: 构造真正 RUNNING 的子任务，验证普通 heartbeat/run timeout 仍然生效。
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


def test_due_check_reports_stale_planning_coordinator_without_runner_timeout(tmp_path: Path):
    """有子任务的 PLANNING coordinator 心跳停滞时报告领导权问题，而不是 runner timeout。"""
    mixin = _BoardTestMixin(workspace=tmp_path)
    old = time.time() - 120
    mixin._tasks = [_coordinator_task(mixin, old=old), _running_child_task(mixin, old=old)]

    report = mixin.due_check(CapabilityConfig(subagent_heartbeat_timeout=1, subagent_run_timeout=1))

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


def test_plan_actions_reports_stale_coordinator_leadership_recovery(tmp_path: Path):
    """动作计划把失联 coordinator 转成领导权恢复建议。"""
    mixin = _BoardTestMixin(workspace=tmp_path)
    mixin._tasks = [_coordinator_task(mixin, old=time.time() - 120)]

    report = mixin.plan_actions(CapabilityConfig(subagent_heartbeat_timeout=1))

    actions = {(action.run_id, action.action): action for action in report.actions}
    assert ("root_coord", "recover_coordinator_leadership") in actions
    assert actions[("root_coord", "recover_coordinator_leadership")].would_change_status_to == ""
