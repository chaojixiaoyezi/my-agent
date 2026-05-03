"""日志分析配置强制转换器测试 - config_coercers.py 哨兵值、查找、警告和类型转换。"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.log_analysis.config.config_coercers import (
    _MISSING,
    _coerce_bool,
    _coerce_choice,
    _coerce_int,
    _coerce_path_string,
    _lookup,
    _warn,
)
from agent_py_agent.agent.log_analysis.config.config_model import LogAnalysisConfigWarning


class TestMissing:
    """_MISSING 哨兵值测试。"""

    def test_missing_is_unique_sentinel(self):
        """验证 _MISSING 是唯一对象。"""
        assert _MISSING is not None
        assert _MISSING != ""


class TestLookup:
    """_lookup 函数测试。"""

    @pytest.mark.parametrize("source,key,expected", [
        ({"key": "value"}, "key", "value"),
        ({"key": "value"}, "nonexistent", _MISSING),
    ], ids=["found", "missing"])
    def test_lookup_from_mapping(self, source, key, expected):
        """从 Mapping 中查找字段。"""
        result = _lookup(source, key)
        assert result is expected

    @pytest.mark.parametrize("attr,expected", [
        ("attr", "value"),
        ("nonexistent", _MISSING),
    ], ids=["attribute_found", "attribute_missing"])
    def test_lookup_from_object(self, attr, expected):
        """从对象中读取属性。"""
        class Obj:
            attr = "value"
        result = _lookup(Obj(), attr)
        assert result is expected


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

    @pytest.mark.parametrize("value,expected", [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
    ], ids=["bool_true", "bool_false", "int_1", "int_0"])
    def test_bool_int_conversion(self, value, expected):
        """布尔值和整数转换。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_bool("field", value, default=not expected, warnings=warnings)
        assert result is expected

    @pytest.mark.parametrize("value", ["true", "True", "TRUE", "yes", "Yes", "on", "ON", "1"], ids=["true", "True", "TRUE", "yes", "Yes", "on", "ON", "1"])
    def test_string_true_values(self, value):
        """字符串 true/yes/on 返回 True。"""
        warnings: list[LogAnalysisConfigWarning] = []
        assert _coerce_bool("field", value, default=False, warnings=warnings) is True

    @pytest.mark.parametrize("value", ["false", "False", "FALSE", "no", "No", "off", "OFF", "0"], ids=["false", "False", "FALSE", "no", "No", "off", "OFF", "0"])
    def test_string_false_values(self, value):
        """字符串 false/no/off 返回 False。"""
        warnings: list[LogAnalysisConfigWarning] = []
        assert _coerce_bool("field", value, default=True, warnings=warnings) is False

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

    @pytest.mark.parametrize("value,expected", [
        ("a", "a"),
        ("c", "a"),
    ], ids=["valid_choice", "invalid_choice_warns"])
    def test_choice_conversion(self, value, expected):
        """选项转换测试。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_choice("field", value, default="a", choices={"a", "b"}, warnings=warnings)
        assert result == expected
        if value == "c":
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

    @pytest.mark.parametrize("value,expected,warns", [
        (True, 0, True),
        (50, 50, False),
        ("42", 42, False),
        (-5, -5, False),
    ], ids=["bool_rejected", "valid_int", "valid_string_int", "negative_int"])
    def test_int_conversion(self, value, expected, warns):
        """整数转换测试。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_int("field", value, default=0, min_value=-10, max_value=100, warnings=warnings)
        assert result == expected
        if warns:
            assert len(warnings) == 1

    @pytest.mark.parametrize("value,expected,warns", [
        (-100, 0, True),
        (200, 0, True),  # 越界返回 default，不是 clamp
        ("not_a_number", 0, True),
    ], ids=["below_min", "above_max", "invalid_string"])
    def test_int_bounds(self, value, expected, warns):
        """整数边界测试。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_int("field", value, default=0, min_value=-10, max_value=100, warnings=warnings)
        assert result == expected
        if warns:
            assert len(warnings) == 1

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

    @pytest.mark.parametrize("value,expected,warns", [
        ("/valid/path", "/valid/path", False),
        ("", "/default", True),
        ("   ", "/default", True),
    ], ids=["valid_path", "empty_string", "whitespace_only"])
    def test_path_string_conversion(self, value, expected, warns):
        """路径字符串转换测试。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_path_string("field", value, default="/default", warnings=warnings)
        assert result == expected
        if warns:
            assert len(warnings) == 1

    @pytest.mark.parametrize("value", [
        "/path/with\x00null",
        "/path/with\nnewline",
        "/path/with\rreturn",
    ], ids=["null_char", "newline", "carriage_return"])
    def test_invalid_chars_warn(self, value):
        """无效字符回退。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_path_string("field", value, default="/default", warnings=warnings)
        assert result == "/default"

    def test_whitespace_trimmed(self):
        """路径空白被去除。"""
        warnings: list[LogAnalysisConfigWarning] = []
        result = _coerce_path_string("field", "  /trimmed/path  ", default="/default", warnings=warnings)
        assert result == "/trimmed/path"
