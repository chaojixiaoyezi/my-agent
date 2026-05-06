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
    PARENT_FINAL_GATE,
    PLAN_NOT_READY_ISSUE,
    LogAnalysisWorkOrderPlan,
    SubagentWorkOrder,
)

# ============================================================
# 测试用例：SubagentWorkOrderCreationResult 数据结构
# ============================================================

class TestSubagentWorkOrderCreationResult:
    """测试 SubagentWorkOrderCreationResult 数据结构"""

    def test_default_values(self):
        """测试默认值"""
        result = SubagentWorkOrderCreationResult(
            case_id="case-001",
            ready=True,
            apply=False,
            dry_run=True,
            mode="dry_run",
        )
        assert result.created == []
        assert result.task_ids == []
        assert result.issues == []
        assert result.risks == []

    def test_with_created_tasks(self):
        """测试包含创建的任务"""
        result = SubagentWorkOrderCreationResult(
            case_id="case-002",
            ready=True,
            apply=True,
            dry_run=False,
            mode="apply",
            created=[
                {"task_id": "task-001", "role": "analyst"},
                {"task_id": "task-002", "role": "reviewer"},
            ],
            task_ids=["task-001", "task-002"],
        )
        assert len(result.created) == 2
        assert len(result.task_ids) == 2

    def test_to_dict(self):
        """测试 to_dict 方法"""
        result = SubagentWorkOrderCreationResult(
            case_id="case-003",
            ready=True,
            apply=False,
            dry_run=True,
            mode="dry_run",
        )
        d = result.to_dict()
        assert isinstance(d, dict)
        assert d["case_id"] == "case-003"
        assert d["ready"] is True
        assert d["dry_run"] is True


# ============================================================
# 测试用例：_work_order_quality_contract
# ============================================================

class TestWorkOrderQualityContract:
    """测试 _work_order_quality_contract 函数"""

    def test_inherits_from_source(self):
        """测试从 source 继承"""
        order = SubagentWorkOrder(
            role="analyst",
            case_id="q-contract-001",
            goal="Test goal",
            evidence_refs=["ev-001"],
            context={"quality_contract": {"user_visible_goal": "Inherited goal"}},
        )
        contract = _work_order_quality_contract(order)
        assert contract["user_visible_goal"] == "Inherited goal"

    def test_merges_evidence_refs(self):
        """测试合并证据引用"""
        order = SubagentWorkOrder(
            role="analyst",
            case_id="q-evidence-001",
            goal="Test goal",
            evidence_refs=["ev-001", "ev-002"],
            context={"quality_contract": {}},
        )
        contract = _work_order_quality_contract(order)
        assert "ev-001" in contract["evidence_required"]
        assert "ev-002" in contract["evidence_required"]

    def test_sets_cannot_self_accept(self):
        """测试设置不能自验收"""
        order = SubagentWorkOrder(
            role="analyst",
            case_id="q-self-001",
            goal="Test goal",
            evidence_refs=["ev-001"],
        )
        contract = _work_order_quality_contract(order)
        assert contract["cannot_self_accept"] is True

    def test_sets_parent_final_gate(self):
        """测试设置父级最终门"""
        order = SubagentWorkOrder(
            role="analyst",
            case_id="q-gate-001",
            goal="Test goal",
            evidence_refs=["ev-001"],
        )
        contract = _work_order_quality_contract(order)
        assert contract["parent_final_gate"] is True
        assert contract["final_judge"] == "parent_final_gate"

    def test_uses_order_goal_as_fallback(self):
        """测试使用 order goal 作为降级"""
        order = SubagentWorkOrder(
            role="analyst",
            case_id="q-goal-001",
            goal="Custom order goal",
            evidence_refs=["ev-001"],
            context={"quality_contract": {}},
        )
        contract = _work_order_quality_contract(order)
        assert contract["user_visible_goal"] == "Custom order goal"

    def test_preserves_quality_bar(self):
        """测试保留 quality_bar"""
        order = SubagentWorkOrder(
            role="analyst",
            case_id="q-bar-001",
            goal="Test goal",
            evidence_refs=["ev-001"],
            context={"quality_contract": {"quality_bar": "High standard"}},
        )
        contract = _work_order_quality_contract(order)
        assert contract["quality_bar"] == "High standard"

    def test_must_check_from_acceptance_checks(self):
        """测试 must_check 包含 acceptance_checks"""
        order = SubagentWorkOrder(
            role="analyst",
            case_id="q-must-001",
            goal="Test goal",
            evidence_refs=["ev-001"],
            acceptance_checks=["Check 1", "Check 2"],
            context={"quality_contract": {}},
        )
        contract = _work_order_quality_contract(order)
        assert "Check 1" in contract["must_check"]
        assert "Check 2" in contract["must_check"]


# ============================================================
# 测试用例：_work_order_context_pack
# ====================================

class TestWorkOrderContextPack:
    """测试 _work_order_context_pack 函数"""

    def test_basic_pack(self):
        """测试基本打包"""
        order = SubagentWorkOrder(
            role="analyst",
            case_id="pack-001",
            goal="Test goal",
            evidence_refs=["ev-001", "ev-002"],
            cannot_self_accept=True,
            parent_final_gate=PARENT_FINAL_GATE,
        )
        pack = _work_order_context_pack(order)
        assert pack["name"] == "log-analysis-work-order"
        assert pack["case_id"] == "pack-001"
        assert pack["role"] == "analyst"
        assert pack["evidence_refs"] == ["ev-001", "ev-002"]
        assert pack["cannot_self_accept"] is True
        assert pack["parent_final_gate"] == PARENT_FINAL_GATE

    def test_includes_route_summary(self):
        """测试包含路由摘要"""
        order = SubagentWorkOrder(
            role="reviewer",
            case_id="pack-route-001",
            goal="Review goal",
            evidence_refs=["ev-001"],
            context={"route_summary": {"entry_candidates": ["EC1"]}},
        )
        pack = _work_order_context_pack(order)
        assert pack["route_summary"]["entry_candidates"] == ["EC1"]

    def test_includes_quality_contract(self):
        """测试包含质量契约"""
        order = SubagentWorkOrder(
            role="analyst",
            case_id="pack-qc-001",
            goal="Test goal",
            evidence_refs=["ev-001"],
            context={"quality_contract": {"min_confidence": 0.8}},
        )
        pack = _work_order_context_pack(order)
        assert "quality_contract" in pack
        assert pack["quality_contract"]["min_confidence"] == 0.8

    def test_uses_case_summary_from_context(self):
        """测试从 context 使用 case_summary"""
        order = SubagentWorkOrder(
            role="analyst",
            case_id="pack-summary-001",
            goal="Test goal",
            evidence_refs=["ev-001"],
            context={"case_summary": "Test case summary text"},
        )
        pack = _work_order_context_pack(order)
        assert pack["case_summary"] == "Test case summary text"


# ============================================================
# 测试用例：_work_order_plan_steps
# ====================================

class TestWorkOrderPlanSteps:
    """测试 _work_order_plan_steps 函数"""

    def test_analyst_steps(self):
        """测试 analyst 步骤"""
        order = SubagentWorkOrder(
            role="analyst",
            case_id="steps-001",
            goal="Analyst goal",
            evidence_refs=["ev-001"],
        )
        steps = _work_order_plan_steps(order)
        assert len(steps) > 0
        assert any("Read the focused context pack" in s for s in steps)
        assert any("allowed tools" in s.lower() for s in steps)

    def test_reviewer_steps_includes_evidence_check(self):
        """测试 reviewer 步骤包含证据检查"""
        order = SubagentWorkOrder(
            role="reviewer",
            case_id="steps-review-001",
            goal="Review goal",
            evidence_refs=["ev-001"],
        )
        steps = _work_order_plan_steps(order)
        # reviewer 应该有额外的证据边界检查步骤
        assert len(steps) > 3  # 比 analyst 多一步
        assert any("evidence boundary" in s.lower() for s in steps)

    def test_steps_reference_case_id(self):
        """测试步骤引用 case_id"""
        order = SubagentWorkOrder(
            role="analyst",
            case_id="steps-case-001",
            goal="Test goal",
            evidence_refs=["ev-001"],
        )
        steps = _work_order_plan_steps(order)
        assert any("steps-case-001" in s for s in steps)


# ============================================================
# 测试用例：create_subagent_tasks_from_work_order_plan
# ====================================



# ============================================================
# 测试用例：边界场景
# ====================================

class TestBoundaryCases:
    """测试边界场景"""

    def test_empty_work_orders_list(self):
        """测试空工单列表"""
        mock_subagents = MagicMock(spec=SubAgentTaskCreator)
        plan = LogAnalysisWorkOrderPlan(
            case_id="empty-001",
            ready=True,
            work_orders=[],
        )
        result = create_subagent_tasks_from_work_order_plan(mock_subagents, plan, apply=True)
        assert result.task_ids == []

    def test_plan_inherits_issues(self):
        """测试计划继承 issues"""
        mock_subagents = MagicMock(spec=SubAgentTaskCreator)
        plan = LogAnalysisWorkOrderPlan(
            case_id="inherit-001",
            ready=True,
            issues=["pre-existing issue"],
        )
        result = create_subagent_tasks_from_work_order_plan(mock_subagents, plan)
        assert "pre-existing issue" in result.issues

    def test_multiple_work_orders_all_ready(self):
        """测试多个工单都 ready 时全部创建"""
        mock_subagents = MagicMock(spec=SubAgentTaskCreator)
        mock_subagents.create_run.side_effect = [
            MagicMock(id="task-a-001"),
            MagicMock(id="task-r-001"),
        ]
        plan = LogAnalysisWorkOrderPlan(
            case_id="multi-001",
            ready=True,
            work_orders=[
                SubagentWorkOrder(
                    role="analyst",
                    case_id="multi-001",
                    goal="Analyst goal",
                    ready=True,
                    evidence_refs=["ev-001"],
                ),
                SubagentWorkOrder(
                    role="reviewer",
                    case_id="multi-001",
                    goal="Review goal",
                    ready=True,
                    evidence_refs=["ev-001"],
                ),
            ],
        )
        result = create_subagent_tasks_from_work_order_plan(mock_subagents, plan, apply=True)
        assert len(result.task_ids) == 2

    def test_context_pack_includes_expected_io(self):
        """测试上下文包包含期望的输入输出"""
        mock_subagents = MagicMock(spec=SubAgentTaskCreator)
        mock_subagents.create_run.return_value = MagicMock(id="task-io-001")
        plan = LogAnalysisWorkOrderPlan(
            case_id="io-001",
            ready=True,
            work_orders=[
                SubagentWorkOrder(
                    role="analyst",
                    case_id="io-001",
                    goal="Test goal",
                    ready=True,
                    evidence_refs=["ev-001"],
                ),
            ],
        )
        create_subagent_tasks_from_work_order_plan(mock_subagents, plan, apply=True)
        call_kwargs = mock_subagents.create_run.call_args[1]
        # context_packs 应该被传递
        assert "context_packs" in call_kwargs
        assert len(call_kwargs["context_packs"]) == 1

    def test_quality_contract_in_context_manifest(self):
        """测试 context_manifest 包含质量契约引用"""
        mock_subagents = MagicMock(spec=SubAgentTaskCreator)
        mock_subagents.create_run.return_value = MagicMock(id="task-manifest-001")
        plan = LogAnalysisWorkOrderPlan(
            case_id="manifest-001",
            ready=True,
            work_orders=[
                SubagentWorkOrder(
                    role="analyst",
                    case_id="manifest-001",
                    goal="Test goal",
                    ready=True,
                    evidence_refs=["ev-001"],
                ),
            ],
        )
        create_subagent_tasks_from_work_order_plan(mock_subagents, plan, apply=True)
        call_kwargs = mock_subagents.create_run.call_args[1]
        manifest = call_kwargs["context_manifest"]
        assert "quality_contract_ref" in manifest