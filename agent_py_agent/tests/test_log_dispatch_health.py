"""测试 log_analysis dispatch/health.py

测试健康状态汇总逻辑：
- DispatchHealthSummary: 健康摘要数据结构
- build_health_summary: 构建健康摘要
- render_health_summary: 渲染健康摘要为文本
- _case_backlog_from_cases: 从案例列表统计
- _get: 通用属性获取辅助函数
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest

from agent_py_agent.agent.log_analysis.dispatch.health import (
    DispatchHealthSummary,
    build_health_summary,
    render_health_summary,
    _case_backlog_from_cases,
    _get,
)


# ============================================================
# 测试用例：DispatchHealthSummary 数据结构
# ============================================================

class TestDispatchHealthSummary:
    """测试 DispatchHealthSummary 数据结构"""

    def test_default_init(self):
        """测试默认初始化"""
        summary = DispatchHealthSummary()
        assert summary.case_backlog == {}
        assert summary.agent_backlog == {}
        assert summary.prompt_switch == {}
        assert summary.dispatch_budget == {}

    def test_init_with_values(self):
        """测试带值初始化"""
        summary = DispatchHealthSummary(
            case_backlog={"OPEN": 5, "CLOSED": 3},
            agent_backlog={"pending": 2},
            prompt_switch={"enabled": True},
            dispatch_budget={"max_agents": 10},
        )
        assert summary.case_backlog["OPEN"] == 5
        assert summary.agent_backlog["pending"] == 2

    def test_to_dict(self):
        """测试 to_dict 方法"""
        summary = DispatchHealthSummary(case_backlog={"total": 10})
        d = summary.to_dict()
        assert isinstance(d, dict)
        assert d["case_backlog"]["total"] == 10


# ============================================================
# 测试用例：_get 辅助函数
# ============================================================

class TestGetHelper:
    """测试 _get 辅助函数"""

    def test_get_from_mapping(self):
        """测试从 Mapping 获取"""
        source = {"key": "value", "nested": {"inner": "data"}}
        assert _get(source, "key") == "value"
        assert _get(source, "missing", "default") == "default"

    def test_get_from_object(self):
        """测试从对象获取"""
        class Obj:
            attr = "value"
        assert _get(Obj(), "attr") == "value"

    def test_get_with_default(self):
        """测试带默认值"""
        assert _get({}, "missing", "default") == "default"
        assert _get(None, "missing", None) is None


# ============================================================
# 测试用例：_case_backlog_from_cases
# ============================================================

class TestCaseBacklogFromCases:
    """测试 _case_backlog_from_cases 函数"""

    def test_empty_cases(self):
        """测试空案例列表"""
        result = _case_backlog_from_cases([])
        assert result == {"total": 0}

    def test_none_cases(self):
        """测试 None 输入"""
        result = _case_backlog_from_cases(None)
        assert result == {}

    def test_single_case(self):
        """测试单个案例"""
        cases = [{"case_id": "c1", "status": "OPEN"}]
        result = _case_backlog_from_cases(cases)
        assert result["OPEN"] == 1
        assert result["total"] == 1

    def test_multiple_cases_same_status(self):
        """测试多案例同状态"""
        cases = [
            {"case_id": "c1", "status": "OPEN"},
            {"case_id": "c2", "status": "OPEN"},
            {"case_id": "c3", "status": "OPEN"},
        ]
        result = _case_backlog_from_cases(cases)
        assert result["OPEN"] == 3
        assert result["total"] == 3

    def test_multiple_cases_different_statuses(self):
        """测试多案例不同状态"""
        cases = [
            {"case_id": "c1", "status": "OPEN"},
            {"case_id": "c2", "status": "CLOSED"},
            {"case_id": "c3", "status": "OPEN"},
        ]
        result = _case_backlog_from_cases(cases)
        assert result["OPEN"] == 2
        assert result["CLOSED"] == 1
        assert result["total"] == 3

    def test_missing_status_treated_as_unknown(self):
        """测试缺失状态当作 unknown"""
        cases = [
            {"case_id": "c1"},
            {"case_id": "c2", "status": "OPEN"},
        ]
        result = _case_backlog_from_cases(cases)
        assert result.get("unknown") == 1
        assert result["OPEN"] == 1


# ============================================================
# 测试用例：build_health_summary 基本功能
# ============================================================

class TestBuildHealthSummary:
    """测试 build_health_summary 函数"""

    def test_build_with_no_args(self):
        """测试无参数调用"""
        result = build_health_summary()
        assert isinstance(result, DispatchHealthSummary)
        assert result.case_backlog == {}
        assert result.agent_backlog == {}

    def test_build_with_cases(self):
        """测试传入案例列表"""
        cases = [
            {"case_id": "c1", "status": "OPEN"},
            {"case_id": "c2", "status": "OPEN"},
        ]
        result = build_health_summary(cases=cases)
        assert result.case_backlog["OPEN"] == 2
        assert result.case_backlog["total"] == 2

    def test_build_with_case_backlog_mapping(self):
        """测试传入 case_backlog 映射"""
        backlog = {"OPEN": 5, "CLOSED": 3}
        result = build_health_summary(case_backlog=backlog)
        assert result.case_backlog["OPEN"] == 5

    def test_build_with_queue(self):
        """测试传入队列"""
        from agent_py_agent.agent.log_analysis.dispatch.queue import InvestigationQueue, DispatchRequest
        queue = InvestigationQueue()
        request = DispatchRequest(case_id="queued-case")
        queue.add(request)
        result = build_health_summary(queue=queue)
        assert result.agent_backlog.get("total", 0) >= 1

    def test_build_with_budget(self):
        """测试传入预算"""
        from agent_py_agent.agent.log_analysis.dispatch.budgets import DispatchBudget
        budget = DispatchBudget(
            case_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=5,
        )
        result = build_health_summary(budget=budget)
        assert result.dispatch_budget["case_auto_dispatch_enabled"] is True

    def test_build_with_budget_mapping(self):
        """测试传入预算字典"""
        budget_dict = {"case_auto_dispatch_enabled": True, "max_parallel_analyst_agents": 3}
        result = build_health_summary(budget=budget_dict)
        assert result.dispatch_budget["max_parallel_analyst_agents"] == 3

    def test_build_with_prompt_config(self):
        """测试传入提示配置"""
        from agent_py_agent.agent.log_analysis.agents.prompts import SecurityPromptConfig
        config = SecurityPromptConfig(security_prompt_enabled=True, security_prompt_mode="analyst")
        result = build_health_summary(prompt_config=config)
        assert result.prompt_switch["security_prompt_enabled"] is True
        assert result.prompt_switch["security_prompt_mode"] == "analyst"


# ============================================================
# 测试用例：render_health_summary
# ============================================================

class TestRenderHealthSummary:
    """测试 render_health_summary 函数"""

    def test_render_summary_object(self):
        """测试渲染 DispatchHealthSummary 对象"""
        summary = DispatchHealthSummary(
            case_backlog={"OPEN": 5, "total": 10},
            agent_backlog={"pending": 2},
            dispatch_budget={"case_auto_dispatch_enabled": True},
        )
        text = render_health_summary(summary)
        assert "log_analysis_health:" in text
        assert "case_backlog" in text
        assert "agent_backlog" in text

    def test_render_mapping(self):
        """测试渲染字典"""
        mapping = {
            "case_backlog": {"OPEN": 3},
            "agent_backlog": {},
            "prompt_switch": {},
            "dispatch_budget": {},
        }
        text = render_health_summary(mapping)
        assert "log_analysis_health:" in text

    def test_render_includes_budget_info(self):
        """测试渲染包含预算信息"""
        summary = DispatchHealthSummary(
            dispatch_budget={
                "case_auto_dispatch_enabled": True,
                "max_parallel_analyst_agents": 5,
                "analyst_agent_budget_per_hour": 10,
            }
        )
        text = render_health_summary(summary)
        assert "max_parallel_analyst_agents" in text
        assert "analyst_agent_budget_per_hour" in text

    def test_render_includes_prompt_switch_info(self):
        """测试渲染包含提示开关信息"""
        summary = DispatchHealthSummary(
            prompt_switch={
                "security_prompt_enabled": True,
                "security_prompt_mode": "incident",
            }
        )
        text = render_health_summary(summary)
        assert "security_prompt_enabled=True" in text
        assert "security_prompt_mode=incident" in text

    def test_render_empty_summary(self):
        """测试渲染空摘要"""
        summary = DispatchHealthSummary()
        text = render_health_summary(summary)
        assert "log_analysis_health:" in text


# ============================================================
# 测试用例：边界场景
# ============================================================

class TestBoundaryCases:
    """测试边界场景"""

    def test_very_large_backlog(self):
        """测试超大积压"""
        large_backlog = {f"status_{i}": i * 10 for i in range(100)}
        summary = DispatchHealthSummary(case_backlog=large_backlog)
        assert len(summary.case_backlog) == 100

    def test_special_characters_in_status(self):
        """测试状态中的特殊字符"""
        cases = [
            {"case_id": "c1", "status": "OPEN"},
            {"case_id": "c2", "status": "IN_PROGRESS"},
            {"case_id": "c3", "status": "CUSTOM_STATUS"},
        ]
        result = _case_backlog_from_cases(cases)
        assert result["OPEN"] == 1
        assert result["IN_PROGRESS"] == 1

    def test_none_values_in_cases(self):
        """测试案例中的 None 值"""
        cases = [
            {"case_id": "c1", "status": "OPEN"},
            None,
            {"case_id": "c2", "status": None},
        ]
        # 应该跳过 None 项
        result = _case_backlog_from_cases([c for c in cases if c])

    def test_render_with_missing_keys(self):
        """测试渲染缺少键的映射"""
        partial = {
            "case_backlog": {"total": 5},
            # 缺少其他键
        }
        text = render_health_summary(partial)
        assert "log_analysis_health:" in text

    def test_build_with_all_none_args(self):
        """测试全部参数为 None"""
        result = build_health_summary(
            cases=None,
            case_backlog=None,
            queue=None,
            budget=None,
            prompt_config=None,
        )
        assert result.case_backlog == {}
        assert result.agent_backlog == {}

    def test_empty_case_backlog_takes_precedence(self):
        """测试显式空 case_backlog 优先于 cases"""
        cases = [{"case_id": "c1", "status": "OPEN"}]
        explicit_backlog = {}
        result = build_health_summary(cases=cases, case_backlog=explicit_backlog)
        assert result.case_backlog == {}