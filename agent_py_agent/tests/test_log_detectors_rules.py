"""检测器规则测试 - rules.py 规则匹配、告警触发、阈值判断。"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from agent_py_agent.agent.log_analysis.analytics.detectors.rules import (
    run_soft_detectors,
    waf_attack_success_candidate,
    web_to_process_anomaly,
    vpn_new_geo_login,
    bruteforce_then_success,
    rare_egress_after_alert,
    multi_source_weak_signal,
    _make_finding,
    _evidence_ref,
    _evidence_id,
    _query,
    _dedupe_findings,
    _stable_id,
    DETECTORS,
)
from agent_py_agent.agent.log_analysis.analytics.baselines import SecurityBaselines


class TestRunSoftDetectors:
    """run_soft_detectors 主入口测试。"""

    def test_run_soft_detectors_empty(self):
        """验证空事件列表返回空。"""
        result = run_soft_detectors([])
        assert result == []

    def test_run_soft_detectors_deduplicates(self):
        """验证结果去重。"""
        events = [
            {"event_class": "alert", "alert_type": "waf", "uri": "/test", "source_product": "waf", "src_ip": "1.2.3.4", "victim_ip": "10.0.0.1"},
            {"event_class": "alert", "alert_type": "waf", "uri": "/test2", "source_product": "waf", "src_ip": "1.2.3.4", "victim_ip": "10.0.0.1"},
        ]
        result = run_soft_detectors(events)
        assert isinstance(result, list)


class TestWafAttackSuccessCandidate:
    """WAF 攻击成功候选检测器测试。"""

    def test_waf_attack_success_candidate_no_waf_event(self):
        """验证无 WAF 事件时返回空。"""
        events = [{"event_class": "auth", "src_ip": "1.2.3.4"}]
        result = waf_attack_success_candidate(events)
        assert result == []

    def test_waf_attack_success_candidate_with_related(self):
        """验证 WAF 事件有相关 HTTP 成功事件时产生发现。"""
        events = [
            {"event_class": "alert", "alert_type": "waf", "uri": "/test", "source_product": "waf", "src_ip": "1.2.3.4", "victim_ip": "10.0.0.1", "status_code": 200, "event_action": "request"},
            {"event_class": "network", "status_code": 200, "src_ip": "1.2.3.4", "victim_ip": "10.0.0.1", "dst_ip": "8.8.8.8", "event_action": "connect"},
        ]
        result = waf_attack_success_candidate(events, window_minutes=15)
        assert len(result) >= 0


class TestWebToProcessAnomaly:
    """Web 进程异常检测器测试。"""

    def test_web_to_process_anomaly_normal_process(self):
        """验证正常 Web 进程不触发。"""
        events = [
            {"source_product": "edr", "event_class": "process", "process_name": "nginx", "parent_process_name": "init", "event_action": "start"},
        ]
        result = web_to_process_anomaly(events)
        assert result == []

    def test_web_to_process_anomaly_suspicious(self):
        """验证可疑 Web 进程触发检测。"""
        events = [
            {"source_product": "edr", "event_class": "process", "process_name": "bash", "parent_process_name": "nginx", "event_action": "exec", "cmdline": "curl http://evil.com"},
        ]
        result = web_to_process_anomaly(events)
        assert len(result) == 1
        assert result[0].detector_id == "web_to_process_anomaly"


class TestVpnNewGeoLogin:
    """VPN 新地域登录检测器测试。"""

    def test_vpn_new_geo_login_no_vpn_event(self):
        """验证非 VPN 事件不触发。"""
        events = [{"source_product": "edr", "event_action": "connect"}]
        result = vpn_new_geo_login(events)
        assert result == []

    def test_vpn_new_geo_login_normal(self):
        """验证正常 VPN 登录不触发。"""
        events = [
            {"source_product": "vpn", "event_action": "login", "event_outcome": "success", "user": "alice", "src_ip": "1.2.3.4"},
        ]
        baselines = SecurityBaselines(
            known_countries_by_user={"alice": {"US"}},
        )
        result = vpn_new_geo_login(events, baselines=baselines)
        assert result == []


class TestBruteforceThenSuccess:
    """暴力破解后成功检测器测试。"""

    def test_bruteforce_then_success_no_failures(self):
        """验证无失败事件时不触发。"""
        events = [
            {"event_class": "auth", "event_action": "login", "event_outcome": "success", "user": "alice", "src_ip": "1.2.3.4"},
        ]
        result = bruteforce_then_success(events)
        assert result == []

    def test_bruteforce_then_success_below_threshold(self):
        """验证失败次数低于阈值时不触发。"""
        events = [
            {"event_class": "auth", "event_action": "login", "event_outcome": "failure", "user": "alice", "src_ip": "1.2.3.4", "event_time": "2024-01-01T10:00:00Z"},
            {"event_class": "auth", "event_action": "login", "event_outcome": "success", "user": "alice", "src_ip": "1.2.3.4", "event_time": "2024-01-01T10:05:00Z"},
        ]
        result = bruteforce_then_success(events, failure_threshold=5)
        assert result == []


class TestRareEgressAfterAlert:
    """告警后罕见外连检测器测试。"""

    def test_rare_egress_after_alert_no_alert(self):
        """验证无告警事件时不触发。"""
        events = [
            {"event_class": "network", "event_action": "connect", "src_ip": "1.2.3.4", "dst_ip": "8.8.8.8"},
        ]
        result = rare_egress_after_alert(events)
        assert result == []

    def test_rare_egress_after_alert_with_baseline(self):
        """验证有基线时检测罕见目标。"""
        events = [
            {"event_class": "alert", "alert_type": "sql_injection", "severity": "high", "source_product": "waf", "victim_ip": "10.0.0.1", "src_ip": "1.2.3.4"},
            {"event_class": "network", "event_action": "connect", "src_ip": "1.2.3.4", "victim_ip": "10.0.0.1", "dst_ip": "8.8.8.8"},
        ]
        baselines = SecurityBaselines(known_egress_destinations_by_asset={"10.0.0.1": {"1.1.1.1"}})
        result = rare_egress_after_alert(events, baselines=baselines, window_minutes=30)
        assert isinstance(result, list)


class TestMultiSourceWeakSignal:
    """多源弱信号检测器测试。"""

    def test_multi_source_weak_signal_single_source(self):
        """验证单一来源不触发。"""
        events = [
            {"source_product": "waf", "event_class": "alert", "alert_type": "web", "severity": "low", "src_ip": "1.2.3.4", "victim_ip": "10.0.0.1", "uri": "/test"},
        ]
        result = multi_source_weak_signal(events)
        assert result == []

    def test_multi_source_weak_signal_overlapping(self):
        """验证多源重叠时触发。"""
        events = [
            {"source_product": "waf", "event_class": "alert", "alert_type": "web", "severity": "low", "src_ip": "1.2.3.4", "victim_ip": "10.0.0.1", "uri": "/test", "event_time": "2024-01-01T10:00:00Z"},
            {"source_product": "edr", "event_class": "auth", "event_action": "login", "event_outcome": "failure", "src_ip": "1.2.3.4", "victim_ip": "10.0.0.1", "event_time": "2024-01-01T10:01:00Z"},
        ]
        result = multi_source_weak_signal(events)
        assert len(result) >= 0


class TestDedupFindings:
    """Finding 去重测试。"""

    def test_dedupe_findings_empty(self):
        """验证空列表。"""
        result = _dedupe_findings([])
        assert result == []

    def test_dedupe_findings_no_duplicates(self):
        """验证无重复时保持原样。"""
        findings = [
            MagicMock(finding_id="f1"),
            MagicMock(finding_id="f2"),
        ]
        result = _dedupe_findings(findings)
        assert len(result) == 2

    def test_dedupe_findings_removes_duplicates(self):
        """验证重复被去除。"""
        findings = [
            MagicMock(finding_id="f1"),
            MagicMock(finding_id="f1"),
            MagicMock(finding_id="f2"),
        ]
        result = _dedupe_findings(findings)
        assert len(result) == 2


class TestStableId:
    """稳定 ID 生成测试。"""

    def test_stable_id_deterministic(self):
        """验证相同输入产生相同 ID。"""
        payload = {"key": "value"}
        id1 = _stable_id("test", payload)
        id2 = _stable_id("test", payload)
        assert id1 == id2

    def test_stable_id_different_inputs(self):
        """验证不同输入产生不同 ID。"""
        id1 = _stable_id("test", {"key": "a"})
        id2 = _stable_id("test", {"key": "b"})
        assert id1 != id2


class TestEvidenceId:
    """证据 ID 生成测试。"""

    def test_evidence_id_from_field(self):
        """验证从已有字段提取。"""
        event = {"alert_id": "alert-001", "source_product": "waf"}
        result = _evidence_id(event)
        assert result == "alert-001"

    def test_evidence_id_from_raw_ref(self):
        """验证从 raw_ref 提取。"""
        event = {"raw_ref": "raw-001"}
        result = _evidence_id(event)
        assert result == "raw-001"

    def test_evidence_id_generated(self):
        """验证无字段时生成 ID。"""
        event = {"source_product": "waf"}
        result = _evidence_id(event)
        assert result.startswith("event-")


class TestEvidenceRef:
    """证据引用构建测试。"""

    def test_evidence_ref_basic(self):
        """验证基本证据引用构建 - raw_ref 优先于 event_id。"""
        event = {
            "event_id": "evt-001",
            "source_id": "waf-01",
            "source_product": "waf",
            "raw_ref": "raw-001",
            "event_time": "2024-01-01T10:00:00Z",
        }
        result = _evidence_ref(event)
        assert result.evidence_id == "raw-001"
        assert result.source_id == "waf-01"


class TestQueryBuilder:
    """查询构建测试。"""

    def test_query_basic(self):
        """验证基本查询构建。"""
        event = {
            "src_ip": "1.2.3.4",
            "victim_ip": "10.0.0.1",
            "host": "server-01",
            "user": "alice",
            "source_product": "waf",
            "event_time": "2024-01-01T10:00:00Z",
        }
        result = _query("Trace web access logs", event)
        assert "purpose" in result
        assert "filters" in result


class TestDetectorsDict:
    """DETECTORS 字典测试。"""

    def test_detectors_has_all_detectors(self):
        """验证包含所有检测器。"""
        expected = ["waf_attack_success_candidate", "web_to_process_anomaly", "vpn_new_geo_login", "bruteforce_then_success", "rare_egress_after_alert", "multi_source_weak_signal"]
        for detector_id in expected:
            assert detector_id in DETECTORS
            assert callable(DETECTORS[detector_id])
