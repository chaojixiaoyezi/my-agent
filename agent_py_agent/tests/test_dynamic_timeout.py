from __future__ import annotations

from agent_py_agent.agent.settings import AgentConfig

"""LLM: tests for dynamic timeout calculation.

给人看的解释：
测试动态超时计算的各种输入大小和边界情况。
"""

import pytest

from agent_py_agent.agent.agent_core.dynamic_timeout import (
    calculate_dynamic_timeout,
    estimate_task_tokens,
    estimate_tokens_from_text,
)
from agent_py_agent.agent.model_speed import SpeedProfile, SpeedSample


class TestEstimateTokens:
    """测试 token 估算函数。"""

    def test_estimate_tokens_empty(self) -> None:
        """测试空字符串。"""
        assert estimate_tokens_from_text("") == 0

    def test_estimate_tokens_english(self) -> None:
        """测试英文文本。"""
        text = "Hello, world! This is a test."
        # 约 32 字符，约 8 token（按 4 字符/token）
        tokens = estimate_tokens_from_text(text)
        assert 6 <= tokens <= 12

    def test_estimate_tokens_chinese(self) -> None:
        """测试中文文本。"""
        text = "你好世界，这是一个测试。"
        # 约 11 字符，约 7-8 token（按 1.5 字符/token）
        tokens = estimate_tokens_from_text(text)
        assert 5 <= tokens <= 10

    def test_estimate_tokens_mixed(self) -> None:
        """测试中英混合文本。"""
        text = "Hello世界！This is a test这是一个测试。"
        tokens = estimate_tokens_from_text(text)
        assert 10 <= tokens <= 25


class TestEstimateTaskTokens:
    """测试任务 token 估算函数。"""

    def test_estimate_task_tokens_simple(self) -> None:
        """测试简单任务。"""
        goal = "查看文件内容"
        plan = ["读取文件", "分析内容"]
        input_tokens, output_tokens = estimate_task_tokens(goal, plan)
        assert input_tokens > 0
        assert output_tokens > 0
        assert output_tokens >= 500  # 最小输出

    def test_estimate_task_tokens_complex(self) -> None:
        """测试复杂任务。"""
        goal = "翻译整个文档并重构代码"
        plan = ["读取文档", "翻译内容", "分析代码结构", "重构代码", "编写测试"]
        input_tokens, output_tokens = estimate_task_tokens(goal, plan)
        assert input_tokens > 0
        assert output_tokens > 0
        assert output_tokens >= 500

    def test_estimate_task_tokens_no_plan(self) -> None:
        """测试没有 plan 的情况。"""
        goal = "简单任务"
        input_tokens, output_tokens = estimate_task_tokens(goal, None)
        assert input_tokens > 0
        assert output_tokens > 0


class TestCalculateDynamicTimeout:
    """测试动态超时计算函数。"""

    @pytest.fixture
    def config(self) -> dict:
        """创建测试配置。"""
        from agent_py_agent.agent.settings import AgentConfig

        return AgentConfig(
            dynamic_timeout_safety_margin=2.0,
            dynamic_timeout_min=30,
            dynamic_timeout_max=600,
            model_speed_profile_path="/tmp/test_profile.json",
        )

    def test_calculate_timeout_no_profile(self, config: AgentConfig) -> None:
        """测试没有速度模型时的情况。"""
        timeout = calculate_dynamic_timeout(
            config,
            estimated_input_tokens=1000,
            estimated_output_tokens=500,
        )
        # 使用经验公式：(1500 / 1000) * 10 * 2.0 = 30秒
        # 会被 min 限制为 30 秒
        assert timeout >= 30
        assert timeout <= 600

    def test_calculate_timeout_with_profile(self, config: AgentConfig, tmp_path) -> None:
        """测试有速度模型时的情况。"""
        # 创建测试速度模型
        profile = SpeedProfile(
            backend="test",
            model="test-model",
            tested_at="2026-05-02T00:00:00Z",
            samples=[
                SpeedSample(input_tokens=1000, output_tokens=500, latency_seconds=10.0),
                SpeedSample(input_tokens=5000, output_tokens=500, latency_seconds=20.0),
            ],
            interpolation_method="log_linear",
        )

        # 保存速度模型
        from agent_py_agent.agent.model_speed import save_speed_profile

        profile_path = tmp_path / "test_profile.json"
        save_speed_profile(profile, profile_path)
        config.model_speed_profile_path = str(profile_path)

        # 测试插值
        timeout = calculate_dynamic_timeout(
            config,
            estimated_input_tokens=2000,
            estimated_output_tokens=500,
        )
        # 2000 token 应该插值到约 10-20 秒之间，加上安全边际 2.0
        assert timeout >= 20
        assert timeout <= 40

    def test_calculate_timeout_min_limit(self, config: AgentConfig) -> None:
        """测试最小超时限制。"""
        timeout = calculate_dynamic_timeout(
            config,
            estimated_input_tokens=10,
            estimated_output_tokens=10,
        )
        # 即使很小的任务，也应该至少返回 30 秒
        assert timeout >= 30

    def test_calculate_timeout_max_limit(self, config: AgentConfig) -> None:
        """测试最大超时限制。"""
        timeout = calculate_dynamic_timeout(
            config,
            estimated_input_tokens=1000000,
            estimated_output_tokens=1000000,
        )
        # 即使很大的任务，也应该最多返回 600 秒
        assert timeout <= 600

    def test_calculate_timeout_custom_safety_margin(self, config: AgentConfig) -> None:
        """测试自定义安全边际。"""
        timeout_default = calculate_dynamic_timeout(
            config,
            estimated_input_tokens=2000,
            estimated_output_tokens=500,
        )

        timeout_custom = calculate_dynamic_timeout(
            config,
            estimated_input_tokens=2000,
            estimated_output_tokens=500,
            safety_margin=3.0,
        )

        # 安全边际越大，超时应该越长
        assert timeout_custom > timeout_default

    def test_calculate_timeout_custom_limits(self, config: AgentConfig) -> None:
        """测试自定义上下限。"""
        timeout = calculate_dynamic_timeout(
            config,
            estimated_input_tokens=2000,
            estimated_output_tokens=500,
            min_timeout=10,
            max_timeout=100,
        )

        # 使用自定义限制
        assert timeout >= 10
        assert timeout <= 100


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
