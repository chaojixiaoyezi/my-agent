"""任务复杂度评估测试 - task_complexity.py 复杂度评估、token估算。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestEstimateTaskComplexity:
    """测试 estimate_task_complexity 函数。"""

    def test_basic_complexity_estimation(self, tmp_path: Path):
        """基本复杂度估算。"""
        from agent_py_agent.agent.agent_core.task_complexity import estimate_task_complexity

        result = estimate_task_complexity(
            goal="修改一个配置文件",
            plan=["步骤1", "步骤2"],
            allowed_tools=["read", "write"],
        )

        assert result.estimated_rounds >= 1
        assert result.confidence == 0.6
        assert "plan_steps" in result.factors

    def test_high_complexity_keywords(self, tmp_path: Path):
        """高复杂度关键词加成。"""
        from agent_py_agent.agent.agent_core.task_complexity import estimate_task_complexity

        result = estimate_task_complexity(
            goal="重构大型代码库并部署到生产环境",
            plan=["分析代码结构", "制定重构计划", "执行重构", "测试", "部署"],
            allowed_tools=["read", "write", "shell", "git"],
        )

        # 高复杂度关键词（重构、部署）应该增加估算轮数
        # base_rounds = 5, keyword_bonus >= 6 (重构=3, 部署=3), tool_bonus = 3
        assert result.estimated_rounds > result.factors["plan_steps"]

    def test_medium_complexity_keywords(self, tmp_path: Path):
        """中等复杂度关键词加成。"""
        from agent_py_agent.agent.agent_core.task_complexity import estimate_task_complexity

        result = estimate_task_complexity(
            goal="修改配置文件",
            plan=["读取配置", "更新值"],
            allowed_tools=["read", "write"],
        )

        # 中等复杂度关键词（修改）应该增加估算轮数
        assert result.estimated_rounds >= 1

    def test_no_plan_steps(self, tmp_path: Path):
        """无计划步骤时的处理。"""
        from agent_py_agent.agent.agent_core.task_complexity import estimate_task_complexity

        result = estimate_task_complexity(
            goal="简单任务",
            plan=[],
            allowed_tools=[],
        )

        # 无 plan 时，最小为 1
        assert result.estimated_rounds == 1

    def test_token_estimation(self, tmp_path: Path):
        """token 数量估算。"""
        from agent_py_agent.agent.agent_core.task_complexity import estimate_task_complexity

        result = estimate_task_complexity(
            goal="分析日志并生成报告",
            plan=["收集日志", "分析数据", "生成图表"],
            allowed_tools=["read", "shell", "write"],
        )

        assert result.estimated_input_tokens > 0
        assert result.estimated_output_tokens > 0

    def test_empty_goal(self, tmp_path: Path):
        """空目标时的处理。"""
        from agent_py_agent.agent.agent_core.task_complexity import estimate_task_complexity

        result = estimate_task_complexity(
            goal="",
            plan=["步骤1"],
            allowed_tools=[],
        )

        assert result.estimated_rounds >= 1

    def test_many_tools(self, tmp_path: Path):
        """多工具时的加权。"""
        from agent_py_agent.agent.agent_core.task_complexity import estimate_task_complexity

        result = estimate_task_complexity(
            goal="测试任务",
            plan=["步骤1"],
            allowed_tools=["tool1", "tool2", "tool3", "tool4", "tool5"],
        )

        # 工具数量超过 1 时应该有 bonus
        assert result.factors["tool_bonus"] > 0

    def test_factors_included(self, tmp_path: Path):
        """factors 包含必要的分析因子。"""
        from agent_py_agent.agent.agent_core.task_complexity import estimate_task_complexity

        result = estimate_task_complexity(
            goal="翻译文档",
            plan=["翻译第一部分", "翻译第二部分"],
            allowed_tools=["read", "write"],
        )

        assert "plan_steps" in result.factors
        assert "keyword_bonus" in result.factors
        assert "tool_bonus" in result.factors


class TestTaskComplexityEstimate:
    """测试 TaskComplexityEstimate 数据类。"""

    def test_default_values(self, tmp_path: Path):
        """默认值测试。"""
        from agent_py_agent.agent.agent_core.task_complexity import TaskComplexityEstimate

        estimate = TaskComplexityEstimate(
            estimated_rounds=5,
            estimated_input_tokens=10000,
            estimated_output_tokens=2500,
            confidence=0.7,
            factors={"test": "value"},
        )

        assert estimate.estimated_rounds == 5
        assert estimate.confidence == 0.7