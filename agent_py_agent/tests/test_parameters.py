"""Tests for agent_core/parameters.py: parameter parsing, defaults, type coercion, and boundary values.

给人看的解释：
测试参数解析模块：工具参数解析、默认值、类型转换、边界值处理。
"""
import tempfile
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.parameters import (
    _bool_param,
    _non_negative_int,
    _one_shot_tool_call_key,
    _positive_int,
    _sleep_with_stop,
    _string_list,
)


class TestOneShotToolCallKey:
    """测试一次性工具调用去重 key 生成。"""

    def test_one_shot_tool_call_key_forbidden_tools(self):
        """验证非一次性工具返回空字符串。"""
        key = _one_shot_tool_call_key({"tool": "read_file", "id": "1"})
        assert key == ""

    def test_one_shot_tool_call_key_allowed_tools(self):
        """验证一次性工具生成唯一 key。"""
        payload = {"tool": "create_subagents", "id": "1", "goal": "test"}
        key = _one_shot_tool_call_key(payload)
        assert key.startswith("create_subagents:")
        assert "test" in key

    def test_one_shot_tool_call_key_identical_calls_same_key(self):
        """验证相同调用生成相同 key。"""
        payload1 = {"tool": "dispatch_subagents", "id": "1"}
        payload2 = {"tool": "dispatch_subagents", "id": "1"}
        assert _one_shot_tool_call_key(payload1) == _one_shot_tool_call_key(payload2)

    def test_one_shot_tool_call_key_different_calls_different_keys(self):
        """验证不同调用生成不同 key。"""
        payload1 = {"tool": "create_subagents", "id": "1"}
        payload2 = {"tool": "create_subagents", "id": "2"}
        assert _one_shot_tool_call_key(payload1) != _one_shot_tool_call_key(payload2)


class TestStringList:
    """测试 _string_list 参数解析。"""

    def test_string_list_from_list(self):
        """验证从列表解析。"""
        result = _string_list(["item1", "item2", "item3"])
        assert result == ["item1", "item2", "item3"]

    def test_string_list_from_tuple(self):
        """验证从元组解析。"""
        result = _string_list(("a", "b", "c"))
        assert result == ["a", "b", "c"]

    def test_string_list_from_json_string(self):
        """验证从 JSON 数组字符串解析。"""
        result = _string_list('["read", "write"]')
        assert result == ["read", "write"]

    def test_string_list_from_newline_string(self):
        """验证从多行文本解析。"""
        result = _string_list("item1\nitem2\nitem3")
        assert result == ["item1", "item2", "item3"]

    def test_string_list_from_comma_string(self):
        """验证从逗号分隔字符串解析。"""
        result = _string_list("a, b, c")
        assert result == ["a", "b", "c"]

    def test_string_list_from_single_value(self):
        """验证从单个值解析。"""
        result = _string_list("single")
        assert result == ["single"]

    def test_string_list_from_none(self):
        """验证 None 返回空列表。"""
        result = _string_list(None)
        assert result == []

    def test_string_list_strips_whitespace(self):
        """验证去除空白字符。"""
        result = _string_list(["  item1  ", "  item2  "])
        assert result == ["item1", "item2"]


class TestBoolParam:
    """测试 _bool_param 布尔参数解析。"""

    def test_bool_param_true_values(self):
        """验证真值解析。"""
        for val in [True, "true", "yes", "on", "1", "y", "apply"]:
            assert _bool_param(val) is True

    def test_bool_param_false_values(self):
        """验证假值解析。"""
        for val in [False, "false", "no", "off", "0", "n", "dry-run", "dry_run"]:
            assert _bool_param(val) is False

    def test_bool_param_default(self):
        """验证默认值。"""
        assert _bool_param(None) is False
        assert _bool_param(None, default=True) is True

    def test_bool_param_unknown_value(self):
        """验证未知值返回默认值。"""
        assert _bool_param("unknown") is False
        assert _bool_param("unknown", default=True) is True


class TestPositiveInt:
    """测试 _positive_int 正整数解析。"""

    def test_positive_int_from_int(self):
        """验证从整数解析。"""
        assert _positive_int(5, default=0) == 5

    def test_positive_int_from_string(self):
        """验证从字符串解析。"""
        assert _positive_int("10", default=0) == 10

    def test_positive_int_negative_clamped_to_zero(self):
        """验证负数被钳制为 0。"""
        assert _positive_int(-5, default=0) == 0

    def test_positive_int_none_uses_default(self):
        """验证 None 使用默认值。"""
        assert _positive_int(None, default=0) == 0
        assert _positive_int(None, default=5) == 5

    def test_positive_int_invalid_value(self):
        """验证无效值使用默认值。"""
        assert _positive_int("invalid", default=0) == 0


class TestNonNegativeInt:
    """测试 _non_negative_int 非负整数解析。"""

    def test_non_negative_int_from_int(self):
        """验证从整数解析。"""
        assert _non_negative_int(0, default=0) == 0
        assert _non_negative_int(5, default=0) == 5

    def test_non_negative_int_from_string(self):
        """验证从字符串解析。"""
        assert _non_negative_int("10", default=0) == 10

    def test_non_negative_int_none_uses_default(self):
        """验证 None 使用默认值。"""
        assert _non_negative_int(None, default=0) == 0


class TestSleepWithStop:
    """测试 _sleep_with_stop 带停止检查的睡眠。"""

    def test_sleep_with_stop_no_stop_file(self):
        """验证无停止文件时正常返回 False。"""
        with tempfile.TemporaryDirectory() as td:
            stop_path = Path(td) / "stop"
            result = _sleep_with_stop(0.01, stop_path)
            assert result is False

    def test_sleep_with_stop_with_stop_file(self):
        """验证有停止文件时立即返回 True。"""
        with tempfile.TemporaryDirectory() as td:
            stop_path = Path(td) / "stop"
            stop_path.write_text("")
            result = _sleep_with_stop(10, stop_path)
            assert result is True

    def test_sleep_with_stop_zero_interval(self):
        """验证零间隔只检查一次。"""
        with tempfile.TemporaryDirectory() as td:
            stop_path = Path(td) / "stop"
            result = _sleep_with_stop(0, stop_path)
            assert result is False
