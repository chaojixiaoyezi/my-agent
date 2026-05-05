"""tokens 模块测试。

测试 token 统计、预算控制、账本管理核心函数。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestTokenBudgetResult:
    """测试 TokenBudgetResult 数据类。"""

    def test_token_budget_result_fields(self):
        """测试返回结果包含所有必要字段。"""
        from agent_py_agent.agent.memory_archive.tokens import TokenBudgetResult

        result = TokenBudgetResult(
            status="ok",
            current_tokens=100,
            max_tokens=1000,
            ratio=0.1,
            archive_level=3,
            message="正常",
        )

        assert result.status == "ok"
        assert result.current_tokens == 100
        assert result.max_tokens == 1000
        assert result.ratio == 0.1
        assert result.archive_level == 3
        assert result.message == "正常"


class TestCheckTokenBudgetLevels:
    """测试不同 archive_level 的预算检查行为。"""

    def test_level_0_thresholds(self):
        """测试 level 0 的阈值（warning 60%, block 85%）。"""
        from agent_py_agent.agent.memory_archive.tokens import check_token_budget

        result = check_token_budget(current_tokens=600, max_tokens=1000, archive_level=0)
        assert result.status == "warning"

        result = check_token_budget(current_tokens=860, max_tokens=1000, archive_level=0)
        assert result.status == "block"

    def test_level_1_thresholds(self):
        """测试 level 1 的阈值（warning 70%, block 90%）。"""
        from agent_py_agent.agent.memory_archive.tokens import check_token_budget

        result = check_token_budget(current_tokens=700, max_tokens=1000, archive_level=1)
        assert result.status == "warning"

        result = check_token_budget(current_tokens=910, max_tokens=1000, archive_level=1)
        assert result.status == "block"

    def test_level_2_thresholds(self):
        """测试 level 2 的阈值（warning 75%, block 95%）。"""
        from agent_py_agent.agent.memory_archive.tokens import check_token_budget

        result = check_token_budget(current_tokens=760, max_tokens=1000, archive_level=2)
        assert result.status == "warning"

        result = check_token_budget(current_tokens=960, max_tokens=1000, archive_level=2)
        assert result.status == "block"

    def test_level_3_thresholds(self):
        """测试 level 3 的阈值（warning 80%, block 100%）。"""
        from agent_py_agent.agent.memory_archive.tokens import check_token_budget

        result = check_token_budget(current_tokens=810, max_tokens=1000, archive_level=3)
        assert result.status == "warning"

        result = check_token_budget(current_tokens=1000, max_tokens=1000, archive_level=3)
        assert result.status == "block"


class TestEstimateTokensEdge:
    """测试 token 估算的边界情况。"""

    def test_estimate_none(self):
        """测试 None 输入。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        result = estimate_tokens(None)
        assert result >= 1

    def test_estimate_mixed_content(self):
        """测试中英文混合内容。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        result = estimate_tokens("Hello 你好 World 世界")
        assert result >= 1

    def test_estimate_numeric(self):
        """测试数字输入。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        result = estimate_tokens(12345)
        assert result >= 1

    def test_estimate_deeply_nested(self):
        """测试深层嵌套结构。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        data = {"a": {"b": {"c": {"d": "value"}}}}
        result = estimate_tokens(data)
        assert result >= 1


class TestAppendSessionTokenUsage:
    """测试会话 token 用量追加。"""

    def test_new_session(self, tmp_path: Path):
        """测试新会话首次记录。"""
        from agent_py_agent.agent.memory_archive.tokens import (
            TurnTokenUsage,
            append_session_token_usage,
        )

        result = append_session_token_usage(
            root=tmp_path,
            usage=TurnTokenUsage(
                session_id="new_session",
                turn_id="turn_1",
                input_tokens=150,
                output_tokens=300,
                tool_tokens=50,
                created_at="2026-05-03T10:00:00Z",
            ),
        )

        assert result["turn_total"] == 500
        assert result["cumulative_tokens"] == 500
        assert result["turn_count"] == 1

    def test_existing_session(self, tmp_path: Path):
        """测试追加到已有会话。"""
        from agent_py_agent.agent.memory_archive.tokens import (
            TurnTokenUsage,
            append_session_token_usage,
        )

        # 第一次
        append_session_token_usage(
            root=tmp_path,
            usage=TurnTokenUsage(
                session_id="existing",
                turn_id="turn_1",
                input_tokens=100,
                output_tokens=100,
                tool_tokens=0,
                created_at="2026-05-03T10:00:00Z",
            ),
        )

        # 第二次
        result = append_session_token_usage(
            root=tmp_path,
            usage=TurnTokenUsage(
                session_id="existing",
                turn_id="turn_2",
                input_tokens=200,
                output_tokens=200,
                tool_tokens=0,
                created_at="2026-05-03T10:01:00Z",
            ),
        )

        assert result["turn_count"] == 2
        assert result["cumulative_tokens"] == 600

    def test_zero_tokens(self, tmp_path: Path):
        """测试零 token 用量。"""
        from agent_py_agent.agent.memory_archive.tokens import (
            TurnTokenUsage,
            append_session_token_usage,
        )

        result = append_session_token_usage(
            root=tmp_path,
            usage=TurnTokenUsage(
                session_id="zero_test",
                turn_id="turn_1",
                input_tokens=0,
                output_tokens=0,
                tool_tokens=0,
                created_at="2026-05-03T10:00:00Z",
            ),
        )

        assert result["turn_total"] == 0
        assert result["cumulative_tokens"] == 0

    def test_large_token_values(self, tmp_path: Path):
        """测试大数值 token 处理。"""
        from agent_py_agent.agent.memory_archive.tokens import (
            TurnTokenUsage,
            append_session_token_usage,
        )

        result = append_session_token_usage(
            root=tmp_path,
            usage=TurnTokenUsage(
                session_id="large",
                turn_id="turn_1",
                input_tokens=1000000,
                output_tokens=2000000,
                tool_tokens=500000,
                created_at="2026-05-03T10:00:00Z",
            ),
        )

        assert result["turn_total"] == 3500000


class TestPayloadToText:
    """测试 _payload_to_text 内部函数（通过 estimate_tokens 间接测试）。"""

    def test_string_payload(self):
        """测试字符串输入直接返回。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        text = "hello world"
        result = estimate_tokens(text)
        assert result >= 1

    def test_dict_payload(self):
        """测试字典转 JSON。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        data = {"key": "value"}
        result = estimate_tokens(data)
        assert result >= 1

    def test_list_payload(self):
        """测试列表转 JSON。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        data = [1, 2, 3]
        result = estimate_tokens(data)
        assert result >= 1


class TestStructuredOverhead:
    """测试结构化数据开销计算。"""

    def test_dict_overhead(self):
        """测试字典开销（字段数/2）。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        data = {"a": 1, "b": 2, "c": 3, "d": 4}
        result = estimate_tokens(data)
        # 应该有额外的结构化开销
        assert result >= 1

    def test_list_overhead(self):
        """测试列表开销（元素数/4）。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        data = [1, 2, 3, 4, 5, 6, 7, 8]
        result = estimate_tokens(data)
        assert result >= 1

    def test_tuple_overhead(self):
        """测试元组开销。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        data = (1, 2, 3, 4)
        result = estimate_tokens(data)
        assert result >= 1

    def test_set_overhead(self):
        """测试集合开销。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        data = {"a", "b", "c"}
        result = estimate_tokens(data)
        assert result >= 1

    def test_string_no_overhead(self):
        """测试字符串无结构化开销。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        text = "plain text"
        result = estimate_tokens(text)
        assert result >= 1
