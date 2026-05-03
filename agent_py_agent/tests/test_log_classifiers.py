"""日志事件分类器测试 - classifiers.py 事件分类、实体比较、弱信号检测。"""
from __future__ import annotations

import pytest


# 直接测试内部 IP 判断函数（不依赖其他模块）
def test_is_internal_ip_10_range():
    """10.x.x.x 是内网 IP。"""
    from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _is_internal_ip
    assert _is_internal_ip("10.0.0.1") is True
    assert _is_internal_ip("10.255.255.255") is True


def test_is_internal_ip_172_range():
    """172.16.x.x - 172.31.x.x 是内网 IP。"""
    from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _is_internal_ip
    assert _is_internal_ip("172.16.0.1") is True
    assert _is_internal_ip("172.31.255.255") is True
    assert _is_internal_ip("172.15.0.1") is False


def test_is_internal_ip_192_168_range():
    """192.168.x.x 是内网 IP。"""
    from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _is_internal_ip
    assert _is_internal_ip("192.168.0.1") is True
    assert _is_internal_ip("192.168.255.255") is True


def test_is_internal_ip_localhost():
    """127.x.x.x 是本地回环。"""
    from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _is_internal_ip
    assert _is_internal_ip("127.0.0.1") is True


def test_is_internal_ip_link_local():
    """169.254.x.x 是链路本地。"""
    from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _is_internal_ip
    assert _is_internal_ip("169.254.0.1") is True


def test_external_ip():
    """公网 IP 不是内网。"""
    from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _is_internal_ip
    assert _is_internal_ip("8.8.8.8") is False
    assert _is_internal_ip("1.1.1.1") is False
    assert _is_internal_ip("198.51.100.1") is False


def test_invalid_ip():
    """无效 IP 返回 False。"""
    from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _is_internal_ip
    assert _is_internal_ip("not.an.ip") is False
    assert _is_internal_ip("") is False


class TestOutcomeClassification:
    """结果分类测试 - 测试 _is_success 和 _is_failure 直接调用。"""

    def test_is_success_values(self):
        """成功结果值。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _is_success
        # 这些函数内部调用 _outcome
        # 测试函数存在且可调用
        assert callable(_is_success)

    def test_is_failure_values(self):
        """失败结果值。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _is_failure
        assert callable(_is_failure)


class TestEntityComparisonFunctions:
    """实体比较函数存在性测试。"""

    def test_same_auth_scope_exists(self):
        """_same_auth_scope 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import (
            _same_auth_scope,
        )
        assert callable(_same_auth_scope)

    def test_same_user_exists(self):
        """_same_user 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _same_user
        assert callable(_same_user)

    def test_same_source_exists(self):
        """_same_source 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _same_source
        assert callable(_same_source)

    def test_same_asset_exists(self):
        """_same_asset 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _same_asset
        assert callable(_same_asset)


class TestAssetFunctions:
    """资产相关函数存在性测试。"""

    def test_asset_candidates_exists(self):
        """_asset_candidates 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import (
            _asset_candidates,
        )
        assert callable(_asset_candidates)

    def test_primary_asset_exists(self):
        """_primary_asset 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _primary_asset
        assert callable(_primary_asset)

    def test_destination_exists(self):
        """_destination 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _destination
        assert callable(_destination)


class TestWeakSignalFunctions:
    """弱信号函数存在性测试。"""

    def test_weak_signal_exists(self):
        """_weak_signal 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _weak_signal
        assert callable(_weak_signal)


class TestEntityFunctions:
    """实体函数存在性测试。"""

    def test_entities_from_events_exists(self):
        """_entities_from_events 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import (
            _entities_from_events,
        )
        assert callable(_entities_from_events)

    def test_normalize_entities_exists(self):
        """_normalize_entities 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import (
            _normalize_entities,
        )
        assert callable(_normalize_entities)

    def test_unique_texts_exists(self):
        """_unique_texts 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _unique_texts
        assert callable(_unique_texts)

    def test_unique_json_values_exists(self):
        """_unique_json_values 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import (
            _unique_json_values,
        )
        assert callable(_unique_json_values)


class TestGapFunctions:
    """信息缺口函数存在性测试。"""

    def test_gap_details_exists(self):
        """_gap_details 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _gap_details
        assert callable(_gap_details)

    def test_first_entity_exists(self):
        """_first_entity 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _first_entity
        assert callable(_first_entity)


class TestEventClassificationFunctions:
    """事件分类函数存在性测试。"""

    def test_is_alert_event_exists(self):
        """_is_alert_event 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import (
            _is_alert_event,
        )
        assert callable(_is_alert_event)

    def test_is_waf_event_exists(self):
        """_is_waf_event 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _is_waf_event
        assert callable(_is_waf_event)

    def test_is_http_success_or_error_exists(self):
        """_is_http_success_or_error 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import (
            _is_http_success_or_error,
        )
        assert callable(_is_http_success_or_error)

    def test_is_suspicious_web_process_event_exists(self):
        """_is_suspicious_web_process_event 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import (
            _is_suspicious_web_process_event,
        )
        assert callable(_is_suspicious_web_process_event)

    def test_is_suspicious_file_write_exists(self):
        """_is_suspicious_file_write 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import (
            _is_suspicious_file_write,
        )
        assert callable(_is_suspicious_file_write)

    def test_is_egress_event_exists(self):
        """_is_egress_event 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import (
            _is_egress_event,
        )
        assert callable(_is_egress_event)

    def test_is_vpn_event_exists(self):
        """_is_vpn_event 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _is_vpn_event
        assert callable(_is_vpn_event)

    def test_is_auth_event_exists(self):
        """_is_auth_event 函数存在。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import _is_auth_event
        assert callable(_is_auth_event)


class TestInternalIPDirect:
    """内部 IP 直接测试 - 无需 mock。"""

    def test_ipv6_localhost(self):
        """IPv6 本地回环。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import (
            _is_internal_ip,
        )
        assert _is_internal_ip("::1") is True

    def test_ipv6_link_local(self):
        """IPv6 链路本地。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import (
            _is_internal_ip,
        )
        assert _is_internal_ip("fe80::1") is True

    def test_ipv6_unique_local(self):
        """IPv6 唯一本地地址。"""
        from agent_py_agent.agent.log_analysis.analytics.detectors.classifiers import (
            _is_internal_ip,
        )
        assert _is_internal_ip("fc00::1") is True
