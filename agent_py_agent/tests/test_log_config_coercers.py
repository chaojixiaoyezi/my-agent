"""日志分析配置强制转换器测试 - config_coercers.py 哨兵值、查找、警告和类型转换。"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.log_analysis.config.config_coercers import (
    _MISSING,
    _lookup,
    _warn,
    _coerce_bool,
    _coerce_choice,
    _coerce_int,
    _coerce_path_string,
)
from agent_py_agent.agent.log_analysis.config.config_model import LogAnalysisConfigWarning


class TestMissing:
    """_MISSING 哨兵值测试。"""

    def test_missing_is_unique_sentinel(self):
        """验证 _MISSING 是唯一对象。"""
        assert _MISSING is not None
        assert _MISSING is not ""


class TestLookup:
    """_lookup 函数测试。"""

    def test_lookup_from_mapping_found(self):
        """从 Mapping 中找到字段。"""
        source = {"key": "value"}
        result = _lookup(source, "key")
        assert result == "value"

    def test_lookup_from_mapping_missing(self):
        """从 Mapping 中未找到字段返回 _MISSING。"""
        source = {"key": "value"}
        result = _lookup(source, "nonexistent")
        assert result is _MISSING

    def test_lookup_from_object_attribute(self):
        """从对象中读取属性。"""
        class Obj:
            attr = "value"
        result = _lookup(Obj(), "attr")
        assert result == "value"

    def test_lookup_from_object_missing(self):
        """从对象中未找到属性返回 _MISSING。"""
        class Obj:
            attr = "value"
        result = _lookup(Obj(), "nonexistent")
        assert result is _MISSING


class TestWarn:
    """_warn 函数测试。"""

    def test_warn_appends_to_list(self):
        """验证追加 warning 到列表。"""
        warnings: list[LogAnalysisConfigWarning] = []
        _warn(warnings, "field1", "bad_value", "default", "expected integer")
        assert len(warnings) == 1
        assert warnings[0].field_name == "field1"
        assert warnings[0].raw_value == "bad_value"
        assert warnings[0].fallback_value == "default"
        assert warnings[0].reason == "expected integer"

    def test_warn_multiple(self):
        """验证追加多条 warning。"""
        warnings: list[LogAnalysisConfigWarning] = []
        _warn(warnings, "a", 1, 0, "reason a")
        _warn(warnings, "b", 2, 0, "reason b")
        assert len(warnings) == 2


class TestCoerceBool:
    """_coerce_bool 函数测试。"""

    def test_missing_returns_default(self):
        """缺失值返回默认值。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_bool("field", _MISSING, default=True, warnings=warnings)
        assert result is True
        assert len(warnings) == 0

    def test_bool_true(self):
        """布尔值 True。"""
        warnings: list[LogAnalysisConfigWarning] = []
        assert _coerce_bool("field", True, default=False, warnings=warnings) is True

    def test_bool_false(self):
        """布尔值 False。"""
        warnings: list[LogAnalysisConfigWarning] = []
        assert _coerce_bool("field", False, default=True, warnings=warnings) is False

    def test_int_1(self):
        """整数 1 转为 True。"""
        warnings: list[LogAnalysisConfigWarning] = []
        assert _coerce_bool("field", 1, default=False, warnings=warnings) is True

    def test_int_0(self):
        """整数 0 转为 False。"""
        warnings: list[LogAnalysisConfigWarning] = []
        assert _coerce_bool("field", 0, default=True, warnings=warnings) is False

    def test_string_true_values(self):
        """字符串 true/yes/on 返回 True。"""
        warnings: list[LogAnalysisConfigWarning] = []
        for val in ["true", "True", "TRUE", "yes", "Yes", "on", "ON", "1"]:
            assert _coerce_bool("field", val, default=False, warnings=warnings) is True

    def test_string_false_values(self):
        """字符串 false/no/off 返回 False。"""
        warnings: list[LogAnalysisConfigWarning] = []
        for val in ["false", "False", "FALSE", "no", "No", "off", "OFF", "0"]:
            assert _coerce_bool("field", val, default=True, warnings=warnings) is False

    def test_invalid_value_warns(self):
        """无效值回退默认值并警告。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_bool("field", "maybe", default=False, warnings=warnings)
        assert result is False
        assert len(warnings) == 1
        assert warnings[0].field_name == "field"

    def test_whitespace_stripped(self):
        """空白被去除。"""
        warnings: list[LogAnalysisConfigWarning] = []
        assert _coerce_bool("field", "  true  ", default=False, warnings=warnings) is True


class TestCoerceChoice:
    """_coerce_choice 函数测试。"""

    def test_missing_returns_default(self):
        """缺失值返回默认值。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_choice("field", _MISSING, default="a", choices={"a", "b"}, warnings=warnings)
        assert result == "a"

    def test_valid_choice(self):
        """有效选项返回。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_choice("field", "a", default="b", choices={"a", "b"}, warnings=warnings)
        assert result == "a"

    def test_invalid_choice_warns(self):
        """无效选项回退并警告。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_choice("field", "c", default="a", choices={"a", "b"}, warnings=warnings)
        assert result == "a"
        assert len(warnings) == 1

    def test_uppercase_mode(self):
        """大写模式转换。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_choice("field", "L2", default="L0", choices={"L0", "L1", "L2"}, warnings=warnings, uppercase=True)
        assert result == "L2"

    def test_whitespace_stripped(self):
        """空白被去除。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_choice("field", "  a  ", default="b", choices={"a", "b"}, warnings=warnings)
        assert result == "a"


class TestCoerceInt:
    """_coerce_int 函数测试。"""

    def test_missing_returns_default(self):
        """缺失值返回默认值。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_int("field", _MISSING, default=100, min_value=0, max_value=1000, warnings=warnings)
        assert result == 100

    def test_bool_rejected(self):
        """布尔值被拒绝。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_int("field", True, default=0, min_value=0, max_value=100, warnings=warnings)
        assert result == 0
        assert len(warnings) == 1

    def test_valid_int(self):
        """有效整数。"""
        warnings: list[LogAnalysisConfigWarning] = []
        assert _coerce_int("field", 50, default=0, min_value=0, max_value=100, warnings=warnings) == 50

    def test_valid_string_int(self):
        """有效数字字符串。"""
        warnings: list[LogAnalysisConfigWarning] = []
        assert _coerce_int("field", "42", default=0, min_value=0, max_value=100, warnings=warnings) == 42

    def test_negative_int(self):
        """负数在范围内。"""
        warnings: list[LogAnalysisConfigWarning] = []
        assert _coerce_int("field", -5, default=0, min_value=-10, max_value=100, warnings=warnings) == -5

    def test_below_min_warns(self):
        """低于最小值回退。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_int("field", -100, default=0, min_value=-10, max_value=100, warnings=warnings)
        assert result == 0
        assert len(warnings) == 1

    def test_above_max_warns(self):
        """高于最大值回退。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_int("field", 200, default=100, min_value=0, max_value=150, warnings=warnings)
        assert result == 100
        assert len(warnings) == 1

    def test_invalid_string_warns(self):
        """无效字符串回退。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_int("field", "not_a_number", default=0, min_value=0, max_value=100, warnings=warnings)
        assert result == 0

    def test_unbounded_max(self):
        """无上限时接受大数。"""
        warnings: list[LogAnalysisConfigWarning] = []
        assert _coerce_int("field", 999999, default=0, min_value=0, max_value=None, warnings=warnings) == 999999


class TestCoercePathString:
    """_coerce_path_string 函数测试。"""

    def test_missing_returns_default(self):
        """缺失值返回默认值。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_path_string("field", _MISSING, default="/default/path", warnings=warnings)
        assert result == "/default/path"

    def test_valid_path(self):
        """有效路径字符串。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_path_string("field", "/valid/path", default="/default", warnings=warnings)
        assert result == "/valid/path"

    def test_empty_string_warns(self):
        """空字符串回退。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_path_string("field", "", default="/default", warnings=warnings)
        assert result == "/default"
        assert len(warnings) == 1

    def test_whitespace_only_warns(self):
        """纯空白回退。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_path_string("field", "   ", default="/default", warnings=warnings)
        assert result == "/default"

    def test_null_char_warns(self):
        """包含 NUL 字符回退。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_path_string("field", "/path/with\x00null", default="/default", warnings=warnings)
        assert result == "/default"

    def test_newline_warns(self):
        """包含换行符回退。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_path_string("field", "/path/with\nnewline", default="/default", warnings=warnings)
        assert result == "/default"

    def test_carriage_return_warns(self):
        """包含回车符回退。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_path_string("field", "/path/with\rreturn", default="/default", warnings=warnings)
        assert result == "/default"

    def test_whitespace_trimmed(self):
        """路径空白被去除。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_path_string("field", "  /trimmed/path  ", default="/default", warnings=warnings)
        assert result == "/trimmed/path"
