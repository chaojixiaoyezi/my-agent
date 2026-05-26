"""测试 log_analysis dispatch/work_orders/creation.py

测试工作单创建逻辑：
- SubAgentTaskCreator Protocol: 接口定义
- SubagentWorkOrderCreationResult: 创建结果数据结构
- create_subagent_tasks_from_work_order_plan: 工单转 SubAgentTask
- _work_order_quality_contract: 质量契约继承
- _work_order_context_pack: 上下文打包
- _work_order_plan_steps: 步骤生成
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.log_analysis.dispatch.work_orders.creation import (
    SubAgentTaskCreator,
    SubagentWorkOrderCreationResult,
    _work_order_context_pack,
    _work_order_plan_steps,
    _work_order_quality_contract,
    create_subagent_tasks_from_work_order_plan,
)
from agent_py_agent.agent.log_analysis.dispatch.work_orders.planning import (
    PLAN_NOT_READY_ISSUE,
    LogAnalysisWorkOrderPlan,
    SubagentWorkOrder,
)

# ============================================================
# 测试用例：SubagentWorkOrderCreationResult 数据结构
# ============================================================

class TestCreateSubagentTasksFromWorkOrderPlan:
    """测试 create_subagent_tasks_from_work_order_plan 函数"""

    def test_dry_run_returns_empty_task_ids(self):
        """测试 dry_run 不创建任务"""
        mock_subagents = MagicMock(spec=SubAgentTaskCreator)
        plan = LogAnalysisWorkOrderPlan(
            case_id="dry-run-001",
            ready=True,
            dry_run=True,
            work_orders=[
                SubagentWorkOrder(
                    role="analyst",
                    case_id="dry-run-001",
                    goal="Analyst goal",
                    ready=True,
                    evidence_refs=["ev-001"],
                ),
            ],
        )
        result = create_subagent_tasks_from_work_order_plan(mock_subagents, plan)
        assert result.dry_run is True
        assert result.task_ids == []
        assert result.created == []
        mock_subagents.create_run.assert_not_called()

    def test_not_ready_plan_returns_issue(self):
        """测试 not ready 的计划返回问题"""
        plan = LogAnalysisWorkOrderPlan(
            case_id="not-ready-001",
            ready=False,
            dry_run=True,
            issues=["missing evidence"],
            work_orders=[],
        )
        result = create_subagent_tasks_from_work_order_plan(MagicMock(), plan)
        assert PLAN_NOT_READY_ISSUE in result.issues

    def test_apply_creates_tasks(self):
        """测试 apply=True 创建任务"""
        mock_task = MagicMock()
        mock_task.id = "created-task-001"
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_subagents = MagicMock(spec=SubAgentTaskCreator)
        mock_subagents.create_run.return_value = mock_task
        plan = LogAnalysisWorkOrderPlan(
            case_id="apply-001",
            ready=True,
            dry_run=False,
            work_orders=[
                SubagentWorkOrder(
                    role="analyst",
                    case_id="apply-001",
                    goal="Analyst goal",
                    ready=True,
                    evidence_refs=["ev-001"],
                    allowed_tools=["security_query"],
                    acceptance_checks=["Check 1"],
                ),
            ],
        )
        result = create_subagent_tasks_from_work_order_plan(mock_subagents, plan, apply=True)
        assert result.apply is True
        assert result.dry_run is False
        assert len(result.task_ids) == 1
        mock_subagents.create_run.assert_called_once()

    def test_skips_not_ready_work_orders(self):
        """测试跳过 not ready 的工单"""
        mock_subagents = MagicMock(spec=SubAgentTaskCreator)
        mock_subagents.create_run.return_value = MagicMock(id="task-001")
        plan = LogAnalysisWorkOrderPlan(
            case_id="skip-001",
            ready=True,
            work_orders=[
                SubagentWorkOrder(
                    role="analyst",
                    case_id="skip-001",
                    goal="Ready goal",
                    ready=True,
                    evidence_refs=["ev-001"],
                ),
                SubagentWorkOrder(
                    role="reviewer",
                    case_id="skip-001",
                    goal="Not ready goal",
                    ready=False,  # not ready
                    evidence_refs=["ev-001"],
                ),
            ],
        )
        result = create_subagent_tasks_from_work_order_plan(mock_subagents, plan, apply=True)
        # 只有 analyst 被创建
        assert len(result.task_ids) == 1
        # reviewer skip issue 应该在 issues 里
        assert any("reviewer" in issue.lower() and "not ready" in issue.lower() for issue in result.issues)

    def test_uses_parent_id_and_root_id(self):
        """测试使用 parent_id 和 root_id"""
        mock_subagents = MagicMock(spec=SubAgentTaskCreator)
        mock_subagents.create_run.return_value = MagicMock(id="task-param-001")
        plan = LogAnalysisWorkOrderPlan(
            case_id="param-001",
            ready=True,
            work_orders=[
                SubagentWorkOrder(
                    role="analyst",
                    case_id="param-001",
                    goal="Test goal",
                    ready=True,
                    evidence_refs=["ev-001"],
                ),
            ],
        )
        create_subagent_tasks_from_work_order_plan(
            mock_subagents,
            plan,
            apply=True,
            parent_id="parent-001",
            root_id="root-001",
        )
        call_kwargs = mock_subagents.create_run.call_args[1]
        params = call_kwargs["params"]
        assert params.parent_id == "parent-001"
        assert params.root_id == "root-001"

    def test_sets_final_owner(self):
        """测试设置 final_owner"""
        mock_subagents = MagicMock(spec=SubAgentTaskCreator)
        mock_subagents.create_run.return_value = MagicMock(id="task-owner-001")
        plan = LogAnalysisWorkOrderPlan(
            case_id="owner-001",
            ready=True,
            work_orders=[
                SubagentWorkOrder(
                    role="analyst",
                    case_id="owner-001",
                    goal="Test goal",
                    ready=True,
                    evidence_refs=["ev-001"],
                ),
            ],
        )
        create_subagent_tasks_from_work_order_plan(
            mock_subagents,
            plan,
            apply=True,
            final_owner="security_team",
        )
        call_kwargs = mock_subagents.create_run.call_args[1]
        assert call_kwargs["params"].final_owner == "security_team"

    def test_work_orders_get_correct_role(self):
        """测试工单获得正确 role"""
        mock_subagents = MagicMock(spec=SubAgentTaskCreator)
        mock_subagents.create_run.return_value = MagicMock(id="task-role-001")
        plan = LogAnalysisWorkOrderPlan(
            case_id="role-001",
            ready=True,
            work_orders=[
                SubagentWorkOrder(
                    role="analyst",
                    case_id="role-001",
                    goal="Analyst goal",
                    ready=True,
                    evidence_refs=["ev-001"],
                ),
            ],
        )
        create_subagent_tasks_from_work_order_plan(mock_subagents, plan, apply=True)
        call_kwargs = mock_subagents.create_run.call_args[1]
        params = call_kwargs["params"]
        assert params.role == "analyst"
        assert params.agent_name == "log-analyst"

    def test_reviewer_work_order_sets_correct_agent_name(self):
        """测试 reviewer 工单设置正确的 agent_name"""
        mock_subagents = MagicMock(spec=SubAgentTaskCreator)
        mock_subagents.create_run.return_value = MagicMock(id="task-review-001")
        plan = LogAnalysisWorkOrderPlan(
            case_id="review-agent-001",
            ready=True,
            work_orders=[
                SubagentWorkOrder(
                    role="reviewer",
                    case_id="review-agent-001",
                    goal="Review goal",
                    ready=True,
                    evidence_refs=["ev-001"],
                ),
            ],
        )
        create_subagent_tasks_from_work_order_plan(mock_subagents, plan, apply=True)
        call_kwargs = mock_subagents.create_run.call_args[1]
        params = call_kwargs["params"]
        assert params.role == "reviewer"
        assert params.agent_name == "log-reviewer"
