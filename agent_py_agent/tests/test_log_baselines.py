"""基线管理测试 - baselines.py 基线建立、偏差检测、异常报警。"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from agent_py_agent.agent.log_analysis.analytics.baselines import (
    SecurityBaselines,
    ensure_baselines,
    _norm,
    _as_set,
    _is_new_value,
    _set_map,
    _int_set_map,
    _sorted_map,
    _sorted_int_map,
)


class TestNorm:
    """_norm 规范化函数测试。"""

    def test_norm_none(self):
        """验证 None 返回空字符串。"""
        assert _norm(None) == ""

    def test_norm_empty_string(self):
        """验证空字符串返回空字符串。"""
        assert _norm("") == ""

    def test_norm_whitespace(self):
        """验证空白被去除。"""
        assert _norm("  hello  ") == "hello"

    def test_norm_lowercase(self):
        """验证转小写。"""
        assert _norm("HELLO") == "hello"

    def test_norm_numeric(self):
        """验证数字被转成字符串。"""
        assert _norm(42) == "42"


class TestAsSet:
    """_as_set 集合转换测试。"""

    def test_as_set_none(self):
        """验证 None 返回空集合。"""
        assert _as_set(None) == set()

    def test_as_set_string(self):
        """验证字符串转单元素集合。"""
        result = _as_set("hello")
        assert result == {"hello"}

    def test_as_set_string_empty(self):
        """验证空字符串返回空集合。"""
        assert _as_set("   ") == set()

    def test_as_set_iterable(self):
        """验证可迭代对象转集合。"""
        result = _as_set(["a", "b", "c"])
        assert result == {"a", "b", "c"}

    def test_as_set_iterable_with_empty(self):
        """验证可迭代对象中空值被过滤。"""
        result = _as_set(["a", "", "b", None])
        assert result == {"a", "b"}


class TestSetMap:
    """_set_map 字典集合映射测试。"""

    def test_set_map_none(self):
        """验证 None 输入返回空字典。"""
        assert _set_map(None) == {}

    def test_set_map_non_dict(self):
        """验证非字典输入返回空字典。"""
        assert _set_map("not a dict") == {}

    def test_set_map_basic(self):
        """验证基本字典转换。"""
        payload = {"user1": ["country1", "country2"], "user2": "single"}
        result = _set_map(payload)
        assert result["user1"] == {"country1", "country2"}
        assert result["user2"] == {"single"}

    def test_set_map_key_normalization(self):
        """验证键名被规范化。"""
        payload = {"User 1": ["val1"], "USER 1": ["val2"]}
        result = _set_map(payload)
        assert "user 1" in result


class TestIntSetMap:
    """_int_set_map 整数集合映射测试。"""

    def test_int_set_map_none(self):
        """验证 None 输入返回空字典。"""
        assert _int_set_map(None) == {}

    def test_int_set_map_non_dict(self):
        """验证非字典输入返回空字典。"""
        assert _int_set_map("not a dict") == {}

    def test_int_set_map_basic(self):
        """验证基本整数集合转换。"""
        payload = {"user1": [80, 443], "user2": 22}
        result = _int_set_map(payload)
        assert result["user1"] == {80, 443}
        assert result["user2"] == {22}

    def test_int_set_map_invalid_values_skipped(self):
        """验证无效整数值被跳过。"""
        payload = {"user1": [80, "invalid", 443]}
        result = _int_set_map(payload)
        assert result["user1"] == {80, 443}


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
        bl = SecurityBaselines(
            known_countries_by_user={"alice": {"CN"}},
        )
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

    def test_is_new_value_empty_mapping(self):
        """验证空映射返回 False。"""
        assert _is_new_value({}, "user", "value") is False

    def test_is_new_value_no_key(self):
        """验证键不存在返回 False。"""
        mapping = {"other_user": {"val1"}}
        assert _is_new_value(mapping, "user", "value") is False

    def test_is_new_value_not_new(self):
        """验证已知值返回 False。"""
        mapping = {"user": {"known_value"}}
        assert _is_new_value(mapping, "user", "known_value") is False

    def test_is_new_value_is_new(self):
        """验证新值返回 True。"""
        mapping = {"user": {"known"}}
        assert _is_new_value(mapping, "user", "unknown") is True


class TestBaselinesIsNewMethods:
    """SecurityBaselines 的 is_new_* 方法测试。"""

    def test_is_new_country_known(self):
        """验证已知国家返回 False。"""
        bl = SecurityBaselines(known_countries_by_user={"alice": {"cn"}})
        assert bl.is_new_country("alice", "cn") is False

    def test_is_new_country_new(self):
        """验证新国家返回 True。"""
        bl = SecurityBaselines(known_countries_by_user={"alice": {"cn"}})
        assert bl.is_new_country("alice", "us") is True

    def test_is_new_asn_known(self):
        """验证已知 ASN 返回 False。"""
        bl = SecurityBaselines(known_asns_by_user={"bob": {"as12345"}})
        assert bl.is_new_asn("bob", "as12345") is False

    def test_is_new_device_known(self):
        """验证已知设备返回 False。"""
        bl = SecurityBaselines(known_devices_by_user={"charlie": {"device1"}})
        assert bl.is_new_device("charlie", "device1") is False

    def test_is_unusual_login_hour_normal(self):
        """验证正常登录时间返回 False。"""
        bl = SecurityBaselines(known_login_hours_by_user={"alice": {9, 10, 11}})
        dt = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        assert bl.is_unusual_login_hour("alice", dt) is False

    def test_is_unusual_login_hour_unusual(self):
        """验证异常登录时间返回 True。"""
        bl = SecurityBaselines(known_login_hours_by_user={"alice": {9, 10, 11}})
        dt = datetime(2024, 1, 1, 3, 0, 0, tzinfo=timezone.utc)
        assert bl.is_unusual_login_hour("alice", dt) is True

    def test_is_unusual_login_hour_no_baseline(self):
        """验证无基线时返回 False。"""
        bl = SecurityBaselines()
        dt = datetime(2024, 1, 1, 3, 0, 0, tzinfo=timezone.utc)
        assert bl.is_unusual_login_hour("alice", dt) is False

    def test_is_rare_egress_destination_known(self):
        """验证已知出口目标返回 False。"""
        bl = SecurityBaselines(known_egress_destinations_by_asset={"server1": {"1.2.3.4"}})
        assert bl.is_rare_egress_destination("server1", "1.2.3.4") is False

    def test_is_rare_egress_destination_new(self):
        """验证新出口目标返回 True。"""
        bl = SecurityBaselines(known_egress_destinations_by_asset={"server1": {"1.2.3.4"}})
        assert bl.is_rare_egress_destination("server1", "5.6.7.8") is True

    def test_is_rare_egress_port_known(self):
        """验证已知端口返回 False。"""
        bl = SecurityBaselines(known_egress_ports_by_asset={"server1": {80, 443}})
        assert bl.is_rare_egress_port("server1", 80) is False

    def test_is_rare_egress_port_invalid(self):
        """验证无效端口值返回 False。"""
        bl = SecurityBaselines(known_egress_ports_by_asset={"server1": {80}})
        assert bl.is_rare_egress_port("server1", "invalid") is False


class TestEnsureBaselines:
    """ensure_baselines 函数测试。"""

    def test_ensure_baselines_already_baselines(self):
        """验证已经是 SecurityBaselines 类型直接返回。"""
        bl = SecurityBaselines()
        result = ensure_baselines(bl)
        assert result is bl

    def test_ensure_baselines_dict(self):
        """验证字典输入转换为 SecurityBaselines。"""
        payload = {"known_countries_by_user": {"alice": ["CN"]}}
        result = ensure_baselines(payload)
        assert isinstance(result, SecurityBaselines)

    def test_ensure_baselines_none(self):
        """验证 None 输入返回空基线。"""
        result = ensure_baselines(None)
        assert isinstance(result, SecurityBaselines)
        assert result.known_countries_by_user == {}
