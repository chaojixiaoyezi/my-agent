"""字段提取器测试 - field_extractors.py 字段提取、正则匹配、结构化解析。"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.log_analysis.analytics.detectors.field_extractors import (
    _asset_ip,
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


class TestSourceProduct:
    """_source_product 字段提取测试。"""

    def test_source_product_normal(self):
        """验证正常字段提取。"""
        event = {"source_product": "WAF"}
        assert _source_product(event) == "waf"

    def test_source_product_alias(self):
        """验证别名映射。"""
        event = {"product": "EDR"}
        assert _source_product(event) == "edr"

    def test_source_product_missing(self):
        """验证缺失字段返回空字符串。"""
        event = {}
        assert _source_product(event) == ""


class TestEventClass:
    """_event_class 字段提取测试。"""

    def test_event_class_normal(self):
        """验证正常字段提取。"""
        event = {"event_class": "Alert"}
        assert _event_class(event) == "alert"

    def test_event_class_alias(self):
        """验证别名映射。"""
        event = {"category": "Network"}
        assert _event_class(event) == "network"

    def test_event_class_missing(self):
        """验证缺失字段返回空字符串。"""
        event = {}
        assert _event_class(event) == ""


class TestEventAction:
    """_event_action 字段提取测试。"""

    def test_event_action_normal(self):
        """验证正常字段提取。"""
        event = {"event_action": "EXECUTE"}
        assert _event_action(event) == "execute"

    def test_event_action_alias(self):
        """验证别名映射。"""
        event = {"action": "Connect"}
        assert _event_action(event) == "connect"

    def test_event_action_missing(self):
        """验证缺失字段返回空字符串。"""
        event = {}
        assert _event_action(event) == ""


class TestOutcome:
    """_outcome 字段提取测试。"""

    def test_outcome_normal(self):
        """验证正常字段提取。"""
        event = {"event_outcome": "SUCCESS"}
        assert _outcome(event) == "success"

    def test_outcome_alias(self):
        """验证别名映射。"""
        event = {"login_result": "failure"}
        assert _outcome(event) == "failure"

    def test_outcome_missing(self):
        """验证缺失字段返回空字符串。"""
        event = {}
        assert _outcome(event) == ""


class TestSeverity:
    """_severity 字段提取测试。"""

    def test_severity_normal(self):
        """验证正常字段提取。"""
        event = {"severity": "HIGH"}
        assert _severity(event) == "high"

    def test_severity_alias(self):
        """验证别名映射。"""
        event = {"severity_hint": "critical"}
        assert _severity(event) == "critical"

    def test_severity_missing(self):
        """验证缺失字段返回空字符串。"""
        event = {}
        assert _severity(event) == ""


class TestSourceIP:
    """_source_ip 字段提取测试。"""

    def test_source_ip_attacker(self):
        """验证攻击者 IP 提取。"""
        event = {"attacker_ip": "192.168.1.100"}
        assert _source_ip(event) == "192.168.1.100"

    def test_source_ip_src(self):
        """验证 src_ip 别名映射。"""
        event = {"src_ip": "10.0.0.1"}
        assert _source_ip(event) == "10.0.0.1"

    def test_source_ip_missing(self):
        """验证缺失字段返回空字符串。"""
        event = {}
        assert _source_ip(event) == ""


class TestDestinationIP:
    """_destination_ip 字段提取测试。"""

    def test_destination_ip_normal(self):
        """验证正常字段提取。"""
        event = {"dst_ip": "8.8.8.8"}
        assert _destination_ip(event) == "8.8.8.8"

    def test_destination_ip_alias(self):
        """验证别名映射。"""
        event = {"destination_ip": "1.2.3.4"}
        assert _destination_ip(event) == "1.2.3.4"

    def test_destination_ip_missing(self):
        """验证缺失字段返回空字符串。"""
        event = {}
        assert _destination_ip(event) == ""


class TestVictimIP:
    """_victim_ip 字段提取测试。"""

    def test_victim_ip_normal(self):
        """验证正常字段提取。"""
        event = {"victim_ip": "192.168.1.1"}
        assert _victim_ip(event) == "192.168.1.1"

    def test_victim_ip_alias(self):
        """验证别名映射。"""
        event = {"asset_ip": "10.10.10.10"}
        assert _victim_ip(event) == "10.10.10.10"

    def test_victim_ip_missing(self):
        """验证缺失字段返回空字符串。"""
        event = {}
        assert _victim_ip(event) == ""


class TestAssetIP:
    """_asset_ip 字段提取测试。"""

    def test_asset_ip_victim(self):
        """验证优先取 victim_ip。"""
        event = {"victim_ip": "192.168.1.1", "src_ip": "10.0.0.1"}
        assert _asset_ip(event) == "192.168.1.1"

    def test_asset_ip_src_fallback(self):
        """验证 victim_ip 缺失时回退到 src_ip。"""
        event = {"src_ip": "10.0.0.1"}
        assert _asset_ip(event) == "10.0.0.1"

    def test_asset_ip_missing(self):
        """验证缺失字段返回空字符串。"""
        event = {}
        assert _asset_ip(event) == ""


class TestHost:
    """_host 字段提取测试。"""

    def test_host_normal(self):
        """验证正常字段提取。"""
        event = {"host": "server-01"}
        assert _host(event) == "server-01"

    def test_host_alias(self):
        """验证别名映射。"""
        event = {"hostname": "web-server"}
        assert _host(event) == "web-server"

    def test_host_missing(self):
        """验证缺失字段返回空字符串。"""
        event = {}
        assert _host(event) == ""


class TestUser:
    """_user 字段提取测试。"""

    def test_user_normal(self):
        """验证正常字段提取。"""
        event = {"user": "admin"}
        assert _user(event) == "admin"

    def test_user_alias(self):
        """验证别名映射。"""
        event = {"username": "root"}
        assert _user(event) == "root"

    def test_user_missing(self):
        """验证缺失字段返回空字符串。"""
        event = {}
        assert _user(event) == ""


class TestDomain:
    """_domain 字段提取测试。"""

    def test_domain_normal(self):
        """验证正常字段提取。"""
        event = {"domain": "evil.com"}
        assert _domain(event) == "evil.com"

    def test_domain_alias(self):
        """验证别名映射。"""
        event = {"dns_query": "malware.com"}
        assert _domain(event) == "malware.com"

    def test_domain_missing(self):
        """验证缺失字段返回空字符串。"""
        event = {}
        assert _domain(event) == ""


class TestDstPort:
    """_dst_port 字段提取测试。"""

    def test_dst_port_int(self):
        """验证整数端口提取。"""
        event = {"dst_port": 443}
        assert _dst_port(event) == 443

    def test_dst_port_string(self):
        """验证字符串端口转换。"""
        event = {"dst_port": "8080"}
        assert _dst_port(event) == 8080

    def test_dst_port_invalid(self):
        """验证无效端口返回 None。"""
        event = {"dst_port": "not_a_port"}
        assert _dst_port(event) is None

    def test_dst_port_missing(self):
        """验证缺失字段返回 None。"""
        event = {}
        assert _dst_port(event) is None


class TestProcessName:
    """_process_name 字段提取测试。"""

    def test_process_name_normal(self):
        """验证正常进程名提取。"""
        event = {"process_name": "python.exe"}
        assert _process_name(event) == "python.exe"

    def test_process_name_path(self):
        """验证路径自动提取 basename。"""
        event = {"process_name": "C:\\Windows\\System32\\cmd.exe"}
        assert _process_name(event) == "cmd.exe"

    def test_process_name_missing(self):
        """验证缺失字段返回空字符串。"""
        event = {}
        assert _process_name(event) == ""


class TestParentProcessName:
    """_parent_process_name 字段提取测试。"""

    def test_parent_process_name_normal(self):
        """验证正常父进程名提取。"""
        event = {"process.parent_name": "nginx"}
        assert _parent_process_name(event) == "nginx"

    def test_parent_process_name_alias(self):
        """验证别名映射。"""
        event = {"parent_process_name": "apache2"}
        assert _parent_process_name(event) == "apache2"

    def test_parent_process_name_missing(self):
        """验证缺失字段返回空字符串。"""
        event = {}
        assert _parent_process_name(event) == ""


class TestCmdline:
    """_cmdline 字段提取测试。"""

    def test_cmdline_normal(self):
        """验证正常命令行提取。"""
        event = {"cmdline": "curl http://evil.com"}
        assert _cmdline(event) == "curl http://evil.com"

    def test_cmdline_nested(self):
        """验证嵌套字段提取。"""
        event = {"process": {"cmdline": "powershell -enc ..."}}
        assert _cmdline(event) == "powershell -enc ..."

    def test_cmdline_missing(self):
        """验证缺失字段返回空字符串。"""
        event = {}
        assert _cmdline(event) == ""
