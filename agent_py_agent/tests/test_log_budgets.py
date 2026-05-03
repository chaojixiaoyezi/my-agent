"""日志分析调度预算测试 - budgets.py 预算决策、并发控制、小时限额。"""
from __future__ import annotations

import pytest
from agent_py_agent.agent.log_analysis.dispatch.budgets import (
    BudgetDecision,
    DispatchBudget,
)


class TestBudgetDecision:
    """BudgetDecision 决策结果测试。"""

    @pytest.mark.parametrize("allowed,reason", [
        (True, "test reason"),
        (False, "budget exhausted"),
    ], ids=["allowed", "denied"])
    def test_budget_decision_fields(self, allowed, reason):
        """验证决策字段。"""
        decision = BudgetDecision(allowed=allowed, reason=reason)
        assert decision.allowed is allowed
        assert decision.reason == reason

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

    @pytest.mark.parametrize("input_val,expected_attr,expected_val", [
        (None, "case_auto_dispatch_enabled", False),
        ({}, "max_parallel_analyst_agents", 0),
    ], ids=["none_payload", "empty_dict"])
    def test_from_mapping_defaults(self, input_val, expected_attr, expected_val):
        """验证默认值。"""
        budget = DispatchBudget.from_mapping(input_val)
        assert getattr(budget, expected_attr) == expected_val

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

    @pytest.mark.parametrize("payload,expected_attr,expected_val", [
        ({"dispatch_budget_per_hour": 50}, "analyst_agent_budget_per_hour", 50),
        ({"auto_dispatch_enabled": True}, "case_auto_dispatch_enabled", True),
    ], ids=["budget_alias", "dispatch_alias"])
    def test_aliases(self, payload, expected_attr, expected_val):
        """验证别名解析。"""
        budget = DispatchBudget.from_mapping(payload)
        assert getattr(budget, expected_attr) == expected_val

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

    @pytest.mark.parametrize("priority,budget_kwargs,expected", [
        ("", {"case_auto_dispatch_enabled": False}, False),
        ("", {"case_auto_dispatch_enabled": True}, True),
        ("P0", {"p0_auto_dispatch_enabled": True}, True),
        ("p0", {"p0_auto_dispatch_enabled": True}, True),
        ("P1", {"p1_auto_dispatch_enabled": True}, True),
        ("P2", {"case_auto_dispatch_enabled": True, "p0_auto_dispatch_enabled": False}, True),
    ], ids=["disabled_no_flags", "enabled_empty_priority", "p0_upper", "p0_lower", "p1", "other_priority"])
    def test_auto_dispatch_for_priority(self, priority, budget_kwargs, expected):
        """验证优先级自动派工。"""
        budget = DispatchBudget(**budget_kwargs)
        assert budget.auto_dispatch_enabled_for_priority(priority) is expected


class TestCanDispatchAnalyst:
    """can_dispatch_analyst 调度预算判断测试。"""

    @pytest.mark.parametrize("budget_kwargs,call_kwargs,expected_allowed,expected_reason_contains", [
        ({"max_parallel_analyst_agents": 0}, {}, False, "max_parallel_analyst_agents=0"),
        ({"max_parallel_analyst_agents": 3, "analyst_agent_budget_per_hour": 0, "case_auto_dispatch_enabled": True}, {}, False, "analyst_agent_budget_per_hour=0"),
        ({"case_auto_dispatch_enabled": False, "max_parallel_analyst_agents": 3, "analyst_agent_budget_per_hour": 10}, {"priority": "P0"}, False, "auto dispatch is disabled"),
        ({"case_auto_dispatch_enabled": True, "max_parallel_analyst_agents": 2, "analyst_agent_budget_per_hour": 10}, {"active_analyst_agents": 2}, False, "max parallel analyst budget exhausted"),
        ({"case_auto_dispatch_enabled": True, "max_parallel_analyst_agents": 5, "analyst_agent_budget_per_hour": 5}, {"analyst_dispatches_last_hour": 5}, False, "hourly analyst dispatch budget exhausted"),
        ({"case_auto_dispatch_enabled": True, "max_parallel_analyst_agents": 3, "analyst_agent_budget_per_hour": 10}, {"priority": "P2", "active_analyst_agents": 1, "analyst_dispatches_last_hour": 3}, True, "dispatch allowed"),
        ({"p0_auto_dispatch_enabled": True, "max_parallel_analyst_agents": 3, "analyst_agent_budget_per_hour": 10}, {"priority": "P0"}, True, "dispatch allowed"),
    ], ids=["max_agents_zero", "budget_zero", "priority_disabled", "active_agents_exhausted", "hourly_budget_exhausted", "all_conditions_met", "p0_allowed"])
    def test_can_dispatch_analyst(self, budget_kwargs, call_kwargs, expected_allowed, expected_reason_contains):
        """验证派工判断。"""
        budget = DispatchBudget(**budget_kwargs)
        decision = budget.can_dispatch_analyst(**call_kwargs)
        assert decision.allowed is expected_allowed
        assert expected_reason_contains in decision.reason
