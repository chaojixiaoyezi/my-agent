"""基线管理测试 - baselines.py 基线建立、偏差检测、异常报警。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_py_agent.agent.log_analysis.analytics.baselines import (
    SecurityBaselines,
    _as_set,
    _int_set_map,
    _is_new_value,
    _norm,
    _set_map,
    _sorted_int_map,
    _sorted_map,
    ensure_baselines,
)


class TestNorm:
    """_norm 规范化函数测试。"""

    @pytest.mark.parametrize("input_val,expected", [
        (None, ""),
        ("", ""),
        ("  hello  ", "hello"),
        ("HELLO", "hello"),
        (42, "42"),
    ], ids=["none", "empty_string", "whitespace", "lowercase", "numeric"])
    def test_norm(self, input_val, expected):
        """验证规范化函数。"""
        assert _norm(input_val) == expected


class TestAsSet:
    """_as_set 集合转换测试。"""

    @pytest.mark.parametrize("input_val,expected", [
        (None, set()),
        ("hello", {"hello"}),
        ("   ", set()),
        (["a", "b", "c"], {"a", "b", "c"}),
        (["a", "", "b", None], {"a", "b"}),
    ], ids=["none", "string", "string_empty", "iterable", "iterable_with_empty"])
    def test_as_set(self, input_val, expected):
        """验证集合转换。"""
        assert _as_set(input_val) == expected


class TestSetMap:
    """_set_map 字典集合映射测试。"""

    @pytest.mark.parametrize("input_val,expected", [
        (None, {}),
        ("not a dict", {}),
        ({"user1": ["country1", "country2"], "user2": "single"}, {"user1": {"country1", "country2"}, "user2": {"single"}}),
        ({"User 1": ["val1"], "USER 1": ["val2"]}, {"user 1": {"val2"}}),
    ], ids=["none", "non_dict", "basic", "key_normalization"])
    def test_set_map(self, input_val, expected):
        """验证字典集合映射。"""
        result = _set_map(input_val)
        assert result == expected


class TestIntSetMap:
    """_int_set_map 整数集合映射测试。"""

    @pytest.mark.parametrize("input_val,expected", [
        (None, {}),
        ("not a dict", {}),
        ({"user1": [80, 443], "user2": 22}, {"user1": {80, 443}, "user2": {22}}),
        ({"user1": [80, "invalid", 443]}, {"user1": {80, 443}}),
    ], ids=["none", "non_dict", "basic", "invalid_values_skipped"])
    def test_int_set_map(self, input_val, expected):
        """验证整数集合映射。"""
        assert _int_set_map(input_val) == expected


class TestSortedMaps:
    """_sorted_map 和 _sorted_int_map 测试。"""

    def test_sorted_map(self):
        """验证字典值被排序。"""
        payload = {"b": {"2", "1"}, "a": {"4", "3"}}
        result = _sorted_map(payload)
        assert result["a"] == ["3", "4"]
        assert result["b"] == ["1", "2"]

    def test_sorted_int_map(self):
        """验证整数字典值被排序。"""
        payload = {"port": {443, 80}, "ip": {8080, 3000}}
        result = _sorted_int_map(payload)
        assert result["port"] == [80, 443]
        assert result["ip"] == [3000, 8080]


class TestSecurityBaselines:
    """SecurityBaselines 数据类测试。"""

    def test_baselines_empty(self):
        """验证空基线初始化。"""
        bl = SecurityBaselines()
        assert bl.known_countries_by_user == {}
        assert bl.known_asns_by_user == {}
        assert bl.known_devices_by_user == {}

    def test_baselines_with_data(self):
        """验证带数据的基线。"""
        bl = SecurityBaselines(
            known_countries_by_user={"alice": {"CN", "US"}},
            known_asns_by_user={"bob": {"AS12345"}},
        )
        assert bl.known_countries_by_user["alice"] == {"CN", "US"}
        assert bl.known_asns_by_user["bob"] == {"AS12345"}

    def test_baselines_to_dict(self):
        """验证 to_dict 方法。"""
        bl = SecurityBaselines(known_countries_by_user={"alice": {"CN"}})
        d = bl.to_dict()
        assert isinstance(d, dict)
        assert "known_countries_by_user" in d

    def test_baselines_from_dict(self):
        """验证 from_dict 类方法。"""
        payload = {
            "known_countries_by_user": {"alice": ["CN", "US"]},
            "known_asns_by_user": {"bob": ["AS12345"]},
        }
        bl = SecurityBaselines.from_dict(payload)
        assert bl.known_countries_by_user["alice"] == {"cn", "us"}


class TestIsNewValue:
    """_is_new_value 新值检测测试。"""

    @pytest.mark.parametrize("mapping,user,value,expected", [
        ({}, "user", "value", False),
        ({"other_user": {"val1"}}, "user", "value", False),
        ({"user": {"known_value"}}, "user", "known_value", False),
        ({"user": {"known"}}, "user", "unknown", True),
    ], ids=["empty_mapping", "no_key", "not_new", "is_new"])
    def test_is_new_value(self, mapping, user, value, expected):
        """验证新值检测。"""
        assert _is_new_value(mapping, user, value) is expected


class TestBaselinesIsNewMethods:
    """SecurityBaselines 的 is_new_* 方法测试。"""

    @pytest.mark.parametrize("method,attr,user,value,baseline,expected", [
        ("is_new_country", "known_countries_by_user", "alice", "cn", {"alice": {"cn"}}, False),
        ("is_new_country", "known_countries_by_user", "alice", "us", {"alice": {"cn"}}, True),
        ("is_new_asn", "known_asns_by_user", "bob", "as12345", {"bob": {"as12345"}}, False),
        ("is_new_device", "known_devices_by_user", "charlie", "device1", {"charlie": {"device1"}}, False),
    ], ids=["country_known", "country_new", "asn_known", "device_known"])
    def test_is_new_methods(self, method, attr, user, value, baseline, expected):
        """验证 is_new_* 方法。"""
        bl = SecurityBaselines(**{attr: baseline})
        result = getattr(bl, method)(user, value)
        assert result is expected

    @pytest.mark.parametrize("hour,expected", [
        (10, False),
        (3, True),
    ], ids=["normal_hour", "unusual_hour"])
    def test_is_unusual_login_hour(self, hour, expected):
        """验证异常登录时间检测。"""
        bl = SecurityBaselines(known_login_hours_by_user={"alice": {9, 10, 11}})
        dt = datetime(2024, 1, 1, hour, 0, 0, tzinfo=timezone.utc)
        assert bl.is_unusual_login_hour("alice", dt) is expected

    def test_is_unusual_login_hour_no_baseline(self):
        """验证无基线时返回 False。"""
        bl = SecurityBaselines()
        dt = datetime(2024, 1, 1, 3, 0, 0, tzinfo=timezone.utc)
        assert bl.is_unusual_login_hour("alice", dt) is False

    @pytest.mark.parametrize("ip,expected", [
        ("1.2.3.4", False),
        ("5.6.7.8", True),
    ], ids=["known_destination", "new_destination"])
    def test_is_rare_egress_destination(self, ip, expected):
        """验证出口目标检测。"""
        bl = SecurityBaselines(known_egress_destinations_by_asset={"server1": {"1.2.3.4"}})
        assert bl.is_rare_egress_destination("server1", ip) is expected

    @pytest.mark.parametrize("port,expected", [
        (80, False),
        ("invalid", False),
    ], ids=["known_port", "invalid_port"])
    def test_is_rare_egress_port(self, port, expected):
        """验证出口端口检测。"""
        bl = SecurityBaselines(known_egress_ports_by_asset={"server1": {80, 443}})
        assert bl.is_rare_egress_port("server1", port) is expected


class TestEnsureBaselines:
    """ensure_baselines 函数测试。"""

    @pytest.mark.parametrize("input_val,expected_type", [
        (SecurityBaselines(), SecurityBaselines),
        ({"known_countries_by_user": {"alice": ["CN"]}}, SecurityBaselines),
        (None, SecurityBaselines),
    ], ids=["already_baselines", "dict", "none"])
    def test_ensure_baselines(self, input_val, expected_type):
        """验证基线确保函数。"""
        result = ensure_baselines(input_val)
        assert isinstance(result, expected_type)
        if input_val is None:
            assert result.known_countries_by_user == {}
