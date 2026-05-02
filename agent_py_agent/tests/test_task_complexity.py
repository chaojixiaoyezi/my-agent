from __future__ import annotations

import pytest

from agent_py_agent.agent.agent_core.task_complexity import (
    TaskComplexityEstimate,
    estimate_task_complexity,
)


class TestEstimateTaskComplexity:
    def test_empty_goal_with_no_tools(self):
        result = estimate_task_complexity("", [], [])
        assert result.estimated_rounds == 1
        assert result.estimated_input_tokens == 2000
        assert result.estimated_output_tokens == 500
        assert result.confidence == 0.6

    def test_simple_goal(self):
        result = estimate_task_complexity("查看文件内容", [], ["read_file"])
        assert result.estimated_rounds == 1
        assert "plan_steps" in result.factors

    def test_high_complexity_keyword_translate(self):
        result = estimate_task_complexity("翻译这篇英文文档", [], [])
        assert result.estimated_rounds >= 4
        assert result.factors["keyword_bonus"] >= 3

    def test_high_complexity_keyword_refactor(self):
        result = estimate_task_complexity("重构这个模块的代码", [], [])
        assert result.estimated_rounds >= 4
        assert result.factors["keyword_bonus"] >= 3

    def test_medium_complexity_keyword(self):
        result = estimate_task_complexity("修改配置文件", [], [])
        assert result.estimated_rounds >= 2
        assert result.factors["keyword_bonus"] >= 1

    def test_plan_steps_count(self):
        result = estimate_task_complexity("做点什么", ["步骤1", "步骤2", "步骤3"], [])
        assert result.estimated_rounds >= 3
        assert result.factors["plan_steps"] == 3

    def test_tool_bonus(self):
        result = estimate_task_complexity("多工具任务", [], ["tool1", "tool2", "tool3", "tool4"])
        assert result.factors["tool_bonus"] == 3

    def test_combined_factors(self):
        result = estimate_task_complexity(
            "翻译并重构这个代码库",
            ["理解代码", "翻译", "验证"],
            ["read_file", "write_file", "search_text"],
        )
        assert result.estimated_rounds > 1
        assert "keyword_bonus" in result.factors
        assert "tool_bonus" in result.factors
        assert "plan_steps" in result.factors


class TestTaskComplexityEstimate:
    def test_estimated_tokens(self):
        result = estimate_task_complexity("测试任务", [], ["tool1", "tool2"])
        assert result.estimated_input_tokens == result.estimated_rounds * 2000
        assert result.estimated_output_tokens == result.estimated_rounds * 500

    def test_confidence_range(self):
        result = estimate_task_complexity("测试", [], [])
        assert 0.0 <= result.confidence <= 1.0
