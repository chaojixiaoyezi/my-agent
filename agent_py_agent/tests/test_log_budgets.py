"""日志分析调度预算测试 - budgets.py 预算决策、并发控制、小时限额。"""
from __future__ import annotations

import pytest
from agent_py_agent.agent.log_analysis.dispatch.budgets import (
    BudgetDecision,
    DispatchBudget,
)


class TestBudgetDecision:
    """BudgetDecision 决策结果测试。"""

    def test_budget_decision_allowed(self):
        """验证允许决策。"""
        decision = BudgetDecision(allowed=True, reason="test reason")
        assert decision.allowed is True
        assert decision.reason == "test reason"

    def test_budget_decision_denied(self):
        """验证拒绝决策。"""
        decision = BudgetDecision(allowed=False, reason="budget exhausted")
        assert decision.allowed is False
        assert decision.reason == "budget exhausted"

    def test_budget_decision_to_dict(self):
        """验证 to_dict 方法。"""
        decision = BudgetDecision(allowed=True, reason="ok")
        d = decision.to_dict()
        assert d["allowed"] is True
        assert d["reason"] == "ok"


class TestDispatchBudgetDefaults:
    """DispatchBudget 默认值测试。"""

    def test_default_values(self):
        """验证所有默认值。"""
        budget = DispatchBudget()
        assert budget.case_auto_dispatch_enabled is False
        assert budget.max_parallel_analyst_agents == 0
        assert budget.analyst_agent_budget_per_hour == 0
        assert budget.analyst_agent_timeout_seconds == 0
        assert budget.p0_auto_dispatch_enabled is False
        assert budget.p1_auto_dispatch_enabled is False
        assert budget.max_case_rounds == 1

    def test_to_dict(self):
        """验证 to_dict 包含所有字段。"""
        d = DispatchBudget().to_dict()
        assert "case_auto_dispatch_enabled" in d
        assert "max_parallel_analyst_agents" in d


class TestDispatchBudgetFromMapping:
    """DispatchBudget.from_mapping 测试。"""

    def test_none_payload(self):
        """验证 None 输入返回默认值。"""
        budget = DispatchBudget.from_mapping(None)
        assert budget.case_auto_dispatch_enabled is False

    def test_empty_dict(self):
        """验证空字典返回默认值。"""
        budget = DispatchBudget.from_mapping({})
        assert budget.max_parallel_analyst_agents == 0

    def test_full_payload(self):
        """验证完整负载解析。"""
        payload = {
            "case_auto_dispatch_enabled": True,
            "max_parallel_analyst_agents": 5,
            "analyst_agent_budget_per_hour": 100,
            "analyst_agent_timeout_seconds": 300,
            "p0_auto_dispatch_enabled": True,
            "p1_auto_dispatch_enabled": True,
            "max_case_rounds": 3,
        }
        budget = DispatchBudget.from_mapping(payload)
        assert budget.case_auto_dispatch_enabled is True
        assert budget.max_parallel_analyst_agents == 5
        assert budget.analyst_agent_budget_per_hour == 100
        assert budget.analyst_agent_timeout_seconds == 300
        assert budget.p0_auto_dispatch_enabled is True
        assert budget.p1_auto_dispatch_enabled is True
        assert budget.max_case_rounds == 3

    def test_dispatch_budget_per_hour_alias(self):
        """验证 dispatch_budget_per_hour 别名。"""
        payload = {"dispatch_budget_per_hour": 50}
        budget = DispatchBudget.from_mapping(payload)
        assert budget.analyst_agent_budget_per_hour == 50

    def test_auto_dispatch_enabled_alias(self):
        """验证 auto_dispatch_enabled 别名。"""
        payload = {"auto_dispatch_enabled": True}
        budget = DispatchBudget.from_mapping(payload)
        assert budget.case_auto_dispatch_enabled is True

    def test_negative_values_clamped_to_zero(self):
        """验证负数值被限制为 0。"""
        payload = {
            "max_parallel_analyst_agents": -5,
            "analyst_agent_budget_per_hour": -10,
            "analyst_agent_timeout_seconds": -30,
            "max_case_rounds": -2,
        }
        budget = DispatchBudget.from_mapping(payload)
        assert budget.max_parallel_analyst_agents == 0
        assert budget.analyst_agent_budget_per_hour == 0
        assert budget.analyst_agent_timeout_seconds == 0
        assert budget.max_case_rounds == 0


class TestAutoDispatchEnabled:
    """auto_dispatch_enabled_for_priority 测试。"""

    def test_disabled_when_no_flags(self):
        """验证所有标志关闭时返回 False。"""
        budget = DispatchBudget(case_auto_dispatch_enabled=False)
        assert budget.auto_dispatch_enabled_for_priority("") is False

    def test_enabled_for_empty_priority(self):
        """验证 case_auto_dispatch_enabled 启用时，空优先级返回 True。"""
        budget = DispatchBudget(case_auto_dispatch_enabled=True)
        assert budget.auto_dispatch_enabled_for_priority("") is True

    def test_p0_priority(self):
        """验证 P0 优先级。"""
        budget = DispatchBudget(p0_auto_dispatch_enabled=True)
        assert budget.auto_dispatch_enabled_for_priority("P0") is True
        assert budget.auto_dispatch_enabled_for_priority("p0") is True

    def test_p1_priority(self):
        """验证 P1 优先级。"""
        budget = DispatchBudget(p1_auto_dispatch_enabled=True)
        assert budget.auto_dispatch_enabled_for_priority("P1") is True

    def test_other_priority_uses_case_flag(self):
        """验证其他优先级回退到 case 标志。"""
        budget = DispatchBudget(case_auto_dispatch_enabled=True, p0_auto_dispatch_enabled=False)
        assert budget.auto_dispatch_enabled_for_priority("P2") is True


class TestCanDispatchAnalyst:
    """can_dispatch_analyst 调度预算判断测试。"""

    def test_disabled_by_max_agents_zero(self):
        """验证 max_parallel_agents=0 时拒绝。"""
        budget = DispatchBudget(max_parallel_analyst_agents=0)
        decision = budget.can_dispatch_analyst()
        assert decision.allowed is False
        assert "max_parallel_analyst_agents=0" in decision.reason

    def test_disabled_by_budget_zero(self):
        """验证 hourly budget=0 时拒绝。"""
        budget = DispatchBudget(
            max_parallel_analyst_agents=3,
            analyst_agent_budget_per_hour=0,
            case_auto_dispatch_enabled=True,
        )
        decision = budget.can_dispatch_analyst()
        assert decision.allowed is False
        assert "analyst_agent_budget_per_hour=0" in decision.reason

    def test_disabled_by_priority(self):
        """验证优先级不满足时拒绝。"""
        budget = DispatchBudget(
            case_auto_dispatch_enabled=False,
            max_parallel_analyst_agents=3,
            analyst_agent_budget_per_hour=10,
        )
        decision = budget.can_dispatch_analyst(priority="P0")
        assert decision.allowed is False
        assert "auto dispatch is disabled" in decision.reason

    def test_disabled_by_active_agents_exhausted(self):
        """验证活跃 agent 达到上限时拒绝。"""
        budget = DispatchBudget(
            case_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=2,
            analyst_agent_budget_per_hour=10,
        )
        decision = budget.can_dispatch_analyst(active_analyst_agents=2)
        assert decision.allowed is False
        assert "max parallel analyst budget exhausted" in decision.reason

    def test_disabled_by_hourly_budget_exhausted(self):
        """验证小时预算耗尽时拒绝。"""
        budget = DispatchBudget(
            case_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=5,
            analyst_agent_budget_per_hour=5,
        )
        decision = budget.can_dispatch_analyst(analyst_dispatches_last_hour=5)
        assert decision.allowed is False
        assert "hourly analyst dispatch budget exhausted" in decision.reason

    def test_allowed_all_conditions_met(self):
        """验证所有条件满足时允许。"""
        budget = DispatchBudget(
            case_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=3,
            analyst_agent_budget_per_hour=10,
        )
        decision = budget.can_dispatch_analyst(
            priority="P2",
            active_analyst_agents=1,
            analyst_dispatches_last_hour=3,
        )
        assert decision.allowed is True
        assert "dispatch allowed" in decision.reason

    def test_allowed_with_p0_priority(self):
        """验证 P0 优先级且相应标志启用时允许。"""
        budget = DispatchBudget(
            p0_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=3,
            analyst_agent_budget_per_hour=10,
        )
        decision = budget.can_dispatch_analyst(priority="P0")
        assert decision.allowed is True
