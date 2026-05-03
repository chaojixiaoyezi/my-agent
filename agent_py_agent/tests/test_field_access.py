"""字段访问控制测试 - field_access.py 字段访问控制、权限校验。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_py_agent.agent.log_analysis.analytics.detectors.field_access import (
    SUSPICIOUS_CHILD_PROCESSES,
    WEB_PARENT_PROCESSES,
    _basename,
    _canonical_time,
    _clamp_float,
    _event_dict,
    _event_time,
    _field,
    _parse_time,
    _path_value,
    _present,
    _sort_time,
    _text,
    _time_bucket,
    _to_float,
    _to_int,
    _truthy,
    _window_for_events,
    _within_after,
    _within_before,
)


class TestEventDict:
    """_event_dict 事件字典转换测试。"""

    def test_event_dict_mapping(self):
        """验证映射类型直接转换。"""
        event = {"key": "value"}
        result = _event_dict(event)
        assert result == {"key": "value"}

    def test_event_dict_dataclass(self):
        """验证 dataclass 转换。"""
        from dataclasses import dataclass

        @dataclass
        class SimpleEvent:
            id: str
            value: int

        event = SimpleEvent(id="e1", value=42)
        result = _event_dict(event)
        assert result["id"] == "e1"
        assert result["value"] == 42

    def test_event_dict_object_with_to_dict(self):
        """验证带 to_dict 方法的对象。"""
        class ObjWithDict:
            def to_dict(self):
                return {"custom": "dict"}

        result = _event_dict(ObjWithDict())
        assert result == {"custom": "dict"}

    def test_event_dict_unknown_object(self):
        """验证未知类型返回默认值。"""
        result = _event_dict("not a dict")
        assert result == {"value": "not a dict"}


class TestField:
    """_field 通用字段访问测试。"""

    def test_field_direct(self):
        """验证直接字段访问。"""
        event = {"source": "value"}
        assert _field(event, "source") == "value"

    def test_field_alias_fallback(self):
        """验证嵌套 bag 中查找字段。"""
        event = {"attributes": {"source": "id-1"}}
        assert _field(event, "source") == "id-1"

    def test_field_nested_bag(self):
        """验证嵌套包查找。"""
        event = {"attributes": {"user": "admin"}}
        assert _field(event, "user") == "admin"

    def test_field_not_found(self):
        """验证字段不存在返回 None。"""
        event = {"other": "value"}
        assert _field(event, "notexists") is None


class TestPathValue:
    """_path_value 路径访问测试。"""

    def test_path_value_direct(self):
        """验证直接键访问。"""
        event = {"source": "value"}
        assert _path_value(event, "source") == "value"

    def test_path_value_case_insensitive(self):
        """验证大小写不敏感。"""
        event = {"SourceIP": "1.2.3.4"}
        assert _path_value(event, "sourceip") == "1.2.3.4"

    def test_path_value_dotted(self):
        """验证点号路径访问。"""
        event = {"network": {"src_ip": "10.0.0.1"}}
        assert _path_value(event, "network.src_ip") == "10.0.0.1"

    def test_path_value_not_found(self):
        """验证路径不存在返回 None。"""
        event = {"other": "value"}
        assert _path_value(event, "nonexistent.path") is None


class TestPresent:
    """_present 值存在性检测测试。"""

    def test_present_none(self):
        """验证 None 返回 False。"""
        assert _present(None) is False

    def test_present_empty_string(self):
        """验证空字符串返回 False。"""
        assert _present("") is False

    def test_present_empty_list(self):
        """验证空列表返回 False。"""
        assert _present([]) is False

    def test_present_empty_dict(self):
        """验证空字典返回 False。"""
        assert _present({}) is False

    def test_present_valid_string(self):
        """验证有效字符串返回 True。"""
        assert _present("hello") is True

    def test_present_valid_list(self):
        """验证有效列表返回 True。"""
        assert _present(["item"]) is True


class TestText:
    """_text 文本转换测试。"""

    def test_text_none(self):
        """验证 None 返回空字符串。"""
        assert _text(None) == ""

    def test_text_string(self):
        """验证字符串去空白。"""
        assert _text("  hello  ") == "hello"

    def test_text_bool_true(self):
        """验证 True 转 "true"。"""
        assert _text(True) == "true"

    def test_text_bool_false(self):
        """验证 False 转 "false"。"""
        assert _text(False) == "false"

    def test_text_numeric(self):
        """验证数字转字符串。"""
        assert _text(42) == "42"


class TestTruthy:
    """_truthy 真值判断测试。"""

    def test_truthy_bool(self):
        """验证布尔值直接返回。"""
        assert _truthy(True) is True
        assert _truthy(False) is False

    def test_truthy_int(self):
        """验证整数判断。"""
        assert _truthy(1) is True
        assert _truthy(0) is False

    def test_truthy_string_true_values(self):
        """验证字符串真值。"""
        for val in ["1", "true", "yes", "y", "new", "rare", "unusual"]:
            assert _truthy(val) is True

    def test_truthy_string_false_values(self):
        """验证字符串假值。"""
        assert _truthy("false") is False
        assert _truthy("no") is False
        assert _truthy("n") is False
        assert _truthy("0") is False
        assert _truthy("off") is False
        assert _truthy("disabled") is False

    def test_truthy_string_non_empty(self):
        """非空字符串（不是明确假值）返回 True。"""
        assert _truthy("unknown") is True
        assert _truthy("random") is True
        assert _truthy("sql_injection") is True


class TestToInt:
    """_to_int 整数转换测试。"""

    def test_to_int_valid_int(self):
        """验证有效整数转换。"""
        assert _to_int(42) == 42

    def test_to_int_valid_string(self):
        """验证字符串整数转换。"""
        assert _to_int("123") == 123

    def test_to_int_bool_returns_none(self):
        """验证布尔值返回 None。"""
        assert _to_int(True) is None
        assert _to_int(False) is None

    def test_to_int_none_returns_none(self):
        """验证 None 返回 None。"""
        assert _to_int(None) is None

    def test_to_int_invalid_string(self):
        """验证无效字符串返回 None。"""
        assert _to_int("not a number") is None


class TestToFloat:
    """_to_float 浮点数转换测试。"""

    def test_to_float_valid_float(self):
        """验证有效浮点数转换。"""
        assert _to_float(3.14) == 3.14

    def test_to_float_valid_int(self):
        """验证整数转浮点数。"""
        assert _to_float(42) == 42.0

    def test_to_float_valid_string(self):
        """验证字符串浮点数转换。"""
        assert _to_float("2.718") == 2.718

    def test_to_float_bool_returns_none(self):
        """验证布尔值返回 None。"""
        assert _to_float(True) is None

    def test_to_float_invalid_string(self):
        """验证无效字符串返回 None。"""
        assert _to_float("not_a_number") is None


class TestClampFloat:
    """_clamp_float 浮点数范围限制测试。"""

    def test_clamp_float_in_range(self):
        """验证范围内值保持不变。"""
        assert _clamp_float(0.5) == 0.5

    def test_clamp_float_below_zero(self):
        """验证低于零的值限制到零。"""
        assert _clamp_float(-0.5) == 0.0

    def test_clamp_float_above_one(self):
        """验证高于一的值限制到一。"""
        assert _clamp_float(1.5) == 1.0

    def test_clamp_float_invalid(self):
        """验证无效值返回零。"""
        assert _clamp_float("invalid") == 0.0


class TestEventTime:
    """_event_time 事件时间提取测试。"""

    def test_event_time_iso_string(self):
        """验证 ISO 时间字符串解析。"""
        event = {"event_time": "2024-01-01T10:00:00Z"}
        result = _event_time(event)
        assert result is not None
        assert result.year == 2024

    def test_event_time_missing(self):
        """验证缺失时间戳返回 None。"""
        event = {}
        assert _event_time(event) is None


class TestParseTime:
    """_parse_time 时间解析测试。"""

    def test_parse_time_datetime(self):
        """验证 datetime 对象直接返回。"""
        dt = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = _parse_time(dt)
        assert result == dt

    def test_parse_time_iso_string(self):
        """验证 ISO 字符串解析。"""
        result = _parse_time("2024-06-15T08:30:00Z")
        assert result is not None
        assert result.year == 2024

    def test_parse_time_epoch(self):
        """验证 Unix 时间戳解析。"""
        result = _parse_time(1718448000)
        assert result is not None

    def test_parse_time_invalid(self):
        """验证无效输入返回 None。"""
        assert _parse_time("invalid") is None
        assert _parse_time(True) is None


class TestCanonicalTime:
    """_canonical_time 规范时间格式测试。"""

    def test_canonical_time_valid(self):
        """验证 datetime 转 ISO 字符串。"""
        dt = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = _canonical_time(dt)
        assert result.endswith("Z")

    def test_canonical_time_none(self):
        """验证 None 返回空字符串。"""
        assert _canonical_time(None) == ""


class TestSortTime:
    """_sort_time 事件排序键测试。"""

    def test_sort_time_with_timestamp(self):
        """验证带时间戳的事件排序键。"""
        event = {"event_time": "2024-01-01T00:00:00Z"}
        key = _sort_time(event)
        assert key[0] == 0  # timed flag
        assert key[1] != ""

    def test_sort_time_without_timestamp(self):
        """验证无时间戳的事件排序键。"""
        event = {}
        key = _sort_time(event)
        assert key[0] == 1  # untimed flag


class TestWithinAfter:
    """_within_after 时间窗口检测测试。"""

    def test_within_after_in_window(self):
        """验证窗口内的候选事件。"""
        start = {"event_time": "2024-01-01T10:00:00Z"}
        candidate = {"event_time": "2024-01-01T10:15:00Z"}
        assert _within_after(start, candidate, minutes=30) is True

    def test_within_after_outside_window(self):
        """验证窗口外的候选事件。"""
        start = {"event_time": "2024-01-01T10:00:00Z"}
        candidate = {"event_time": "2024-01-01T12:00:00Z"}
        assert _within_after(start, candidate, minutes=30) is False


class TestWithinBefore:
    """_within_before 时间窗口检测测试。"""

    def test_within_before_in_window(self):
        """验证窗口内的候选事件。"""
        end = {"event_time": "2024-01-01T10:00:00Z"}
        candidate = {"event_time": "2024-01-01T09:45:00Z"}
        assert _within_before(candidate, end, minutes=30) is True


class TestWindowForEvents:
    """_window_for_events 时间窗口计算测试。"""

    def test_window_for_events_normal(self):
        """验证正常事件窗口计算。"""
        events = [
            {"event_time": "2024-01-01T10:00:00Z"},
            {"event_time": "2024-01-01T12:00:00Z"},
        ]
        start, end = _window_for_events(events)
        assert start != ""
        assert end != ""

    def test_window_for_events_empty(self):
        """验证空事件列表返回当前时间。"""
        start, end = _window_for_events([])
        assert start != ""
        assert end != ""


class TestTimeBucket:
    """_time_bucket 时间桶化测试。"""

    def test_time_bucket_normal(self):
        """验证正常时间桶化。"""
        dt = datetime(2024, 1, 1, 10, 25, 0, tzinfo=timezone.utc)
        result = _time_bucket(dt, minutes=15)
        assert result == "2024-01-01T10:15:00Z"

    def test_time_bucket_none(self):
        """验证 None 输入返回 unknown。"""
        assert _time_bucket(None, minutes=15) == "unknown-time"


class TestBasename:
    """_basename 路径基名提取测试。"""

    def test_basename_simple(self):
        """验证简单文件名。"""
        assert _basename("file.txt") == "file.txt"

    def test_basename_unix_path(self):
        """验证 Unix 路径。"""
        assert _basename("/usr/local/bin/python") == "python"

    def test_basename_windows_path(self):
        """验证 Windows 路径。"""
        assert _basename("C:\\Windows\\System32\\cmd.exe") == "cmd.exe"

    def test_basename_lowercase(self):
        """验证结果转小写。"""
        assert _basename("Python.EXE") == "python.exe"


class TestProcessSets:
    """WEB_PARENT_PROCESSES 和 SUSPICIOUS_CHILD_PROCESSES 常量测试。"""

    def test_web_parent_processes_not_empty(self):
        """验证 Web 父进程集合非空。"""
        assert len(WEB_PARENT_PROCESSES) > 0
        assert "nginx" in WEB_PARENT_PROCESSES
        assert "apache" in WEB_PARENT_PROCESSES

    def test_suspicious_child_processes_not_empty(self):
        """验证可疑子进程集合非空。"""
        assert len(SUSPICIOUS_CHILD_PROCESSES) > 0
        assert "cmd.exe" in SUSPICIOUS_CHILD_PROCESSES
        assert "powershell.exe" in SUSPICIOUS_CHILD_PROCESSES
        assert "bash" in SUSPICIOUS_CHILD_PROCESSES
