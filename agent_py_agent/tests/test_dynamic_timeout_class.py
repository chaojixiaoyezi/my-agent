"""动态超时计算测试 - dynamic_timeout.py 超时计算、参数调整。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestCalculateDynamicTimeout:
    """测试 calculate_dynamic_timeout 函数。"""

    def test_fallback_calculation_without_profile(self, tmp_path: Path):
        """无速度模型时使用经验公式。"""
        from agent_py_agent.agent.agent_core.dynamic_timeout import calculate_dynamic_timeout

        class MockConfig:
            dynamic_timeout_safety_margin = 1.2
            dynamic_timeout_min = 10.0
            dynamic_timeout_max = 300.0
            model_speed_profile_path = ""

        config = MockConfig()

        result = calculate_dynamic_timeout(config, 1000, 500)

        # 应该使用经验公式：total_tokens/1000 * 10 * safety_margin
        # total_tokens = 1500, base = 15, with margin = 18
        assert result >= 10.0
        assert result <= 300.0

    def test_min_timeout_limit(self, tmp_path: Path):
        """最小超时限制。"""
        from agent_py_agent.agent.agent_core.dynamic_timeout import calculate_dynamic_timeout

        class MockConfig:
            dynamic_timeout_safety_margin = 1.0
            dynamic_timeout_min = 30.0
            dynamic_timeout_max = 300.0
            model_speed_profile_path = ""

        config = MockConfig()

        # 很小的输入也应该至少使用 min_timeout
        result = calculate_dynamic_timeout(config, 10, 5)

        assert result >= 30.0

    def test_max_timeout_limit(self, tmp_path: Path):
        """最大超时限制。"""
        from agent_py_agent.agent.agent_core.dynamic_timeout import calculate_dynamic_timeout

        class MockConfig:
            dynamic_timeout_safety_margin = 1.0
            dynamic_timeout_min = 10.0
            dynamic_timeout_max = 100.0
            model_speed_profile_path = ""

        config = MockConfig()

        # 很大的输入不应该超过 max_timeout
        result = calculate_dynamic_timeout(config, 1000000, 500000)

        assert result <= 100.0

    def test_zero_tokens_default(self, tmp_path: Path):
        """零 token 时使用默认值。"""
        from agent_py_agent.agent.agent_core.dynamic_timeout import calculate_dynamic_timeout

        class MockConfig:
            dynamic_timeout_safety_margin = 1.2
            dynamic_timeout_min = 10.0
            dynamic_timeout_max = 300.0
            model_speed_profile_path = ""

        config = MockConfig()

        result = calculate_dynamic_timeout(config, 0, 0)

        # 零 token 时使用默认值 2000
        # base = 2000/1000 * 10 = 20, * 1.2 = 24
        assert result >= 10.0

    def test_custom_safety_margin(self, tmp_path: Path):
        """自定义安全边际。"""
        from agent_py_agent.agent.agent_core.dynamic_timeout import calculate_dynamic_timeout

        class MockConfig:
            dynamic_timeout_safety_margin = 2.0
            dynamic_timeout_min = 10.0
            dynamic_timeout_max = 300.0
            model_speed_profile_path = ""

        config = MockConfig()

        result1 = calculate_dynamic_timeout(config, 1000, 0)
        result2 = calculate_dynamic_timeout(config, 1000, 0, safety_margin=1.0)

        # 更高的安全边际应该导致更长的超时
        assert result1 > result2


class TestEstimateTokensFromText:
    """测试 estimate_tokens_from_text 函数。"""

    def test_english_text_estimation(self, tmp_path: Path):
        """英文文本 token 估算。"""
        from agent_py_agent.agent.agent_core.dynamic_timeout import estimate_tokens_from_text

        text = "a" * 40  # 40 个字符
        result = estimate_tokens_from_text(text)

        # 英文大约 4 字符 = 1 token
        assert result == 10  # 40 / 4 = 10

    def test_chinese_text_estimation(self, tmp_path: Path):
        """中文文本 token 估算。"""
        from agent_py_agent.agent.agent_core.dynamic_timeout import estimate_tokens_from_text

        text = "中" * 30  # 30 个中文字符
        result = estimate_tokens_from_text(text)

        # 中文大约 1.5 字符 = 1 token
        assert result == 20  # 30 / 1.5 = 20

    def test_mixed_text_estimation(self, tmp_path: Path):
        """中英文混合文本估算。"""
        from agent_py_agent.agent.agent_core.dynamic_timeout import estimate_tokens_from_text

        text = "Hello World 你好世界"  # 英文 + 中文
        result = estimate_tokens_from_text(text)

        # 应该同时考虑中英文
        assert result > 0

    def test_empty_text(self, tmp_path: Path):
        """空文本返回 0。"""
        from agent_py_agent.agent.agent_core.dynamic_timeout import estimate_tokens_from_text

        result = estimate_tokens_from_text("")
        assert result == 0


class TestEstimateTaskTokens:
    """测试 estimate_task_tokens 函数。"""

    def test_basic_token_estimation(self, tmp_path: Path):
        """基本 token 估算。"""
        from agent_py_agent.agent.agent_core.dynamic_timeout import estimate_task_tokens

        input_tokens, output_tokens = estimate_task_tokens("测试任务", ["步骤1", "步骤2"])

        assert input_tokens > 0
        assert output_tokens > 0

    def test_output_ratio(self, tmp_path: Path):
        """输出 token 是输入的一定比例。"""
        from agent_py_agent.agent.agent_core.dynamic_timeout import estimate_task_tokens

        input_tokens, output_tokens = estimate_task_tokens("测试任务", ["步骤1"])

        # 输出应该是输入的约 30%，且最小 500
        assert output_tokens >= 500

    def test_empty_plan(self, tmp_path: Path):
        """空计划时的处理。"""
        from agent_py_agent.agent.agent_core.dynamic_timeout import estimate_task_tokens

        input_tokens, output_tokens = estimate_task_tokens("测试任务", None)

        assert input_tokens > 0
        assert output_tokens >= 500

    def test_long_plan(self, tmp_path: Path):
        """长计划时的处理。"""
        from agent_py_agent.agent.agent_core.dynamic_timeout import estimate_task_tokens

        plan = [f"步骤{i}" for i in range(20)]
        input_tokens, output_tokens = estimate_task_tokens("复杂任务", plan)

        assert input_tokens > 0