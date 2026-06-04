"""检测器底层辅助函数测试。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import (
    _is_alert_event,
    _is_auth_event,
    _is_egress_event,
    _is_failure,
    _is_http_success_or_error,
    _is_internal_ip,
    _is_success,
    _is_suspicious_file_write,
    _is_suspicious_web_process_event,
    _is_vpn_event,
    _is_waf_event,
    _normalize_entities,
    _primary_asset,
    _same_asset,
    _same_auth_scope,
    _same_source,
    _same_user,
    _unique_json_values,
    _unique_texts,
    _weak_signal,
)
from agent_py_agent.agent.log_analysis.analytics.detectors.field_access import (
    SUSPICIOUS_CHILD_PROCESSES,
    WEB_PARENT_PROCESSES,
    _canonical_time,
    _clamp_float,
    _event_dict,
    _event_time,
    _field,
    _parse_time,
    _path_value,
    _present,
    _text,
    _time_bucket,
    _to_float,
    _to_int,
    _truthy,
    _window_for_events,
    _within_after,
    _within_before,
)
from agent_py_agent.agent.log_analysis.analytics.detectors.field_extractors import (
    _asset_ip,
    _basename,
    _cmdline,
    _destination_ip,
    _domain,
    _dst_port,
    _event_action,
    _event_class,
    _host,
    _outcome,
    _parent_process_name,
    _process_name,
    _severity,
    _source_ip,
    _source_product,
    _user,
    _victim_ip,
)


class TestEventDict:
    """_event_dict 事件字典转换测试。"""

    def test_event_dict_from_mapping(self):
        """验证字典输入直接转换。"""
        event = {"key": "value"}
        result = _event_dict(event)
        assert result == {"key": "value"}

    def test_event_dict_from_object(self):
        """验证带 to_dict 方法的对象。"""
        class Obj:
            def to_dict(self):
                return {"custom": "dict"}
        result = _event_dict(Obj())
        assert result == {"custom": "dict"}

    def test_event_dict_none_fallback(self):
        """验证无法转换时返回默认值。"""
        result = _event_dict(42)
        assert result == {"value": 42}


class TestFieldAccess:
    """字段访问函数测试。"""

    def test_field_direct(self):
        """验证直接字段访问。"""
        event = {"source": "value"}
        assert _field(event, "source") == "value"

    def test_field_nested_bag(self):
        """验证嵌套 bag 查找。"""
        event = {"attributes": {"user": "admin"}}
        assert _field(event, "user") == "admin"

    def test_path_value_dotted(self):
        """验证点号路径。"""
        event = {"network": {"src_ip": "10.0.0.1"}}
        assert _path_value(event, "network.src_ip") == "10.0.0.1"

    def test_path_value_case_insensitive(self):
        """验证大小写不敏感。"""
        event = {"SourceIP": "1.2.3.4"}
        assert _path_value(event, "sourceip") == "1.2.3.4"


class TestTypeConversion:
    """类型转换函数测试。"""

    def test_to_int_valid(self):
        """验证有效整数转换。"""
        assert _to_int("123") == 123
        assert _to_int(42) == 42

    def test_to_int_invalid(self):
        """验证无效输入返回 None。"""
        assert _to_int("not_number") is None
        assert _to_int(None) is None
        assert _to_int(True) is None

    def test_to_float_valid(self):
        """验证有效浮点数转换。"""
        assert _to_float("3.14") == 3.14

    def test_to_float_invalid(self):
        """验证无效输入返回 None。"""
        assert _to_float("not_a_number") is None
        assert _to_float(None) is None

    def test_clamp_float_in_range(self):
        """验证范围限制。"""
        assert _clamp_float(0.5) == 0.5
        assert _clamp_float(-0.5) == 0.0
        assert _clamp_float(1.5) == 1.0


class TestTimeHelpers:
    """时间处理函数测试。"""

    def test_parse_time_iso(self):
        """验证 ISO 字符串解析。"""
        result = _parse_time("2024-01-01T10:00:00Z")
        assert result is not None
        assert result.year == 2024

    def test_parse_time_epoch(self):
        """验证时间戳解析。"""
        result = _parse_time(1704067200)
        assert result is not None

    def test_parse_time_invalid(self):
        """验证无效输入返回 None。"""
        assert _parse_time("invalid") is None
        assert _parse_time(None) is None

    def test_canonical_time(self):
        """验证规范时间格式。"""
        dt = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = _canonical_time(dt)
        assert result.endswith("Z")

    def test_event_time_missing(self):
        """验证缺失时间戳返回 None。"""
        assert _event_time({}) is None

    def test_time_bucket(self):
        """验证时间桶化。"""
        dt = datetime(2024, 1, 1, 10, 25, 0, tzinfo=timezone.utc)
        result = _time_bucket(dt, minutes=15)
        assert "10:15" in result or "10:00" in result


class TestWithinWindow:
    """时间窗口检测测试。"""

    def test_within_after_in_window(self):
        """验证窗口内事件。"""
        start = {"event_time": "2024-01-01T10:00:00Z"}
        candidate = {"event_time": "2024-01-01T10:10:00Z"}
        assert _within_after(start, candidate, minutes=15) is True

    def test_within_after_outside_window(self):
        """验证窗口外事件。"""
        start = {"event_time": "2024-01-01T10:00:00Z"}
        candidate = {"event_time": "2024-01-01T12:00:00Z"}
        assert _within_after(start, candidate, minutes=15) is False

    def test_within_before_in_window(self):
        """验证 before 窗口检测。"""
        end = {"event_time": "2024-01-01T10:00:00Z"}
        candidate = {"event_time": "2024-01-01T09:50:00Z"}
        assert _within_before(candidate, end, minutes=15) is True


class TestFieldExtractors:
    """字段提取器测试。"""

    def test_source_ip(self):
        """验证源 IP 提取。"""
        assert _source_ip({"attacker_ip": "1.2.3.4"}) == "1.2.3.4"
        assert _source_ip({"src_ip": "5.6.7.8"}) == "5.6.7.8"
        assert _source_ip({}) == ""

    def test_destination_ip(self):
        """验证目标 IP 提取。"""
        assert _destination_ip({"dst_ip": "8.8.8.8"}) == "8.8.8.8"
        assert _destination_ip({}) == ""

    def test_user_extraction(self):
        """验证用户提取。"""
        assert _user({"user": "admin"}) == "admin"
        assert _user({"username": "root"}) == "root"
        assert _user({}) == ""

    def test_domain_extraction(self):
        """验证域名提取。"""
        assert _domain({"domain": "evil.com"}) == "evil.com"
        assert _domain({"dns_query": "malware.com"}) == "malware.com"

    def test_process_name_with_path(self):
        """验证进程名从路径提取。"""
        assert _process_name({"process_name": "/usr/bin/python"}) == "python"

    def test_dst_port(self):
        """验证端口提取。"""
        assert _dst_port({"dst_port": 443}) == 443
        assert _dst_port({"dst_port": "8080"}) == 8080
        assert _dst_port({}) is None


class TestClassifiers:
    """事件分类器测试。"""

    def test_is_waf_event(self):
        """验证 WAF 事件分类。"""
        assert _is_waf_event({"source_product": "waf", "uri": "/test"}) is True
        assert _is_waf_event({"source_product": "edr"}) is False

    def test_is_vpn_event(self):
        """验证 VPN 事件分类。"""
        assert _is_vpn_event({"source_product": "vpn", "event_action": "login"}) is True
        assert _is_vpn_event({"source_product": "edr"}) is False

    def test_is_auth_event(self):
        """验证认证事件分类。"""
        assert _is_auth_event({"event_class": "auth", "event_action": "login"}) is True
        assert _is_auth_event({"event_action": "login"}) is True

    def test_is_success(self):
        """验证成功判断。"""
        assert _is_success({"event_outcome": "success"}) is True
        assert _is_success({"event_outcome": "failure"}) is False

    def test_is_failure(self):
        """验证失败判断。"""
        assert _is_failure({"event_outcome": "failed"}) is True
        assert _is_failure({"event_outcome": "success"}) is False

    def test_is_http_success_or_error(self):
        """验证 HTTP 状态码判断。"""
        assert _is_http_success_or_error({"status_code": 200}) is True
        assert _is_http_success_or_error({"status_code": 500}) is True
        assert _is_http_success_or_error({"status_code": 404}) is False

    def test_is_internal_ip(self):
        """验证内网 IP 判断。"""
        assert _is_internal_ip("10.0.0.1") is True
        assert _is_internal_ip("192.168.1.1") is True
        assert _is_internal_ip("8.8.8.8") is False

    def test_is_alert_event(self):
        """验证告警事件分类。"""
        assert _is_alert_event({"event_class": "alert"}) is True
        assert _is_alert_event({"severity": "high"}) is True
        assert _is_alert_event({"alert_type": "sql_injection"}) is True


class TestEntityComparison:
    """实体比较函数测试。"""

    def test_same_user(self):
        """验证同用户判断。"""
        assert _same_user({"user": "alice"}, {"user": "alice"}) is True
        assert _same_user({"user": "alice"}, {"user": "bob"}) is False

    def test_same_source(self):
        """验证同源 IP 判断。"""
        assert _same_source({"src_ip": "1.2.3.4"}, {"src_ip": "1.2.3.4"}) is True
        assert _same_source({"src_ip": "1.2.3.4"}, {"src_ip": "5.6.7.8"}) is False

    def test_same_asset(self):
        """验证同资产判断。"""
        assert _same_asset({"victim_ip": "10.0.0.1"}, {"victim_ip": "10.0.0.1"}) is True

    def test_same_auth_scope(self):
        """验证同认证范围判断。"""
        left = {"user": "alice", "src_ip": "1.2.3.4"}
        right = {"user": "alice", "src_ip": "1.2.3.4"}
        assert _same_auth_scope(left, right) is True


class TestWeakSignal:
    """弱信号检测测试。"""

    def test_weak_signal_waf(self):
        """验证 WAF 弱信号。"""
        event = {"source_product": "waf", "uri": "/test", "event_class": "alert"}
        result = _weak_signal(event)
        assert result is not None
        assert result["signal_type"] == "waf_web_alert"

    def test_weak_signal_auth_failure(self):
        """验证认证失败弱信号。"""
        event = {"source_product": "vpn", "event_action": "login", "event_outcome": "failure", "event_class": "auth"}
        result = _weak_signal(event)
        assert result is not None
        assert result["signal_type"] == "auth_failure"

    def test_weak_signal_none(self):
        """验证普通事件返回 None。"""
        event = {"source_product": "web", "event_action": "request"}
        result = _weak_signal(event)
        assert result is None


class TestUniqueHelpers:
    """去重辅助函数测试。"""

    def test_unique_texts(self):
        """验证文本去重。"""
        result = _unique_texts(["a", "b", "a", "c"])
        assert result == ["a", "b", "c"]

    def test_unique_texts_with_empty(self):
        """验证空值被过滤。"""
        result = _unique_texts(["a", "", None, "b"])
        assert result == ["a", "b"]

    def test_normalize_entities(self):
        """验证实体规范化。"""
        entities = {"ip": ["1.2.3.4", "1.2.3.4", "5.6.7.8"]}
        result = _normalize_entities(entities)
        assert len(result.get("ip", [])) == 2


class TestProcessConstants:
    """进程常量测试。"""

    def test_web_parent_processes_not_empty(self):
        """验证 Web 父进程集合非空。"""
        assert len(WEB_PARENT_PROCESSES) > 0
        assert "nginx" in WEB_PARENT_PROCESSES

    def test_suspicious_child_processes_not_empty(self):
        """验证可疑子进程集合非空。"""
        assert len(SUSPICIOUS_CHILD_PROCESSES) > 0
        assert "bash" in SUSPICIOUS_CHILD_PROCESSES
