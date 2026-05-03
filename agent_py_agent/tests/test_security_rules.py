"""安全规则引擎测试 - security_rules.py 规则加载、匹配逻辑、DetectorRule。"""
from __future__ import annotations

import pytest
from agent_py_agent.agent.log_analysis.analytics.security_rules import (
    DetectorRule,
    RULE_VERSION,
    SOFT_DETECTOR_RULES,
    get_rule,
    detector_ids,
)


class TestDetectorRuleDataclass:
    """DetectorRule 数据类基本功能测试。"""

    def test_detector_rule_required_fields(self):
        """验证必需字段初始化。"""
        rule = DetectorRule(
            detector_id="test_rule",
            detector_kind="rule",
            mode="soft",
        )
        assert rule.detector_id == "test_rule"
        assert rule.detector_kind == "rule"
        assert rule.mode == "soft"

    def test_detector_rule_defaults(self):
        """验证默认值。"""
        rule = DetectorRule(detector_id="test_default")
        assert rule.detector_kind == "rule"
        assert rule.mode == "soft"
        assert rule.version == RULE_VERSION
        assert rule.severity_hint == "medium"
        assert rule.min_confidence_for_case == 0.6
        assert rule.lookback_seconds == 900
        assert rule.required_fields == ()
        assert rule.tactics == ()
        assert rule.techniques == ()

    def test_detector_rule_to_dict(self):
        """验证 to_dict 方法。"""
        rule = DetectorRule(
            detector_id="test_to_dict",
            description="测试规则",
            severity_hint="high",
            tactics=("initial-access",),
            techniques=("T1190",),
        )
        d = rule.to_dict()
        assert isinstance(d, dict)
        assert d["detector_id"] == "test_to_dict"
        assert d["severity_hint"] == "high"
        assert d["tactics"] == ("initial-access",)
        assert d["techniques"] == ("T1190",)


class TestSoftDetectorRules:
    """预定义规则集测试。"""

    def test_soft_detector_rules_not_empty(self):
        """验证预定义规则不为空。"""
        assert len(SOFT_DETECTOR_RULES) > 0

    def test_all_rules_have_detector_id(self):
        """验证每条规则都有 detector_id。"""
        for rule_id, rule in SOFT_DETECTOR_RULES.items():
            assert rule.detector_id == rule_id

    def test_all_rules_have_required_fields(self):
        """验证所有规则都有 required_fields。"""
        for rule in SOFT_DETECTOR_RULES.values():
            assert isinstance(rule.required_fields, tuple)

    def test_all_rules_have_version(self):
        """验证所有规则版本一致。"""
        for rule in SOFT_DETECTOR_RULES.values():
            assert rule.version == RULE_VERSION

    def test_all_rules_have_tactics(self):
        """验证所有规则都有 tactics 元数据。"""
        for rule in SOFT_DETECTOR_RULES.values():
            assert isinstance(rule.tactics, tuple)


class TestGetRule:
    """get_rule 函数测试。"""

    def test_get_rule_existing(self):
        """验证获取已存在的规则。"""
        rule = get_rule("waf_attack_success_candidate")
        assert rule.detector_id == "waf_attack_success_candidate"

    def test_get_rule_unknown_raises(self):
        """验证获取未知规则抛出 KeyError。"""
        with pytest.raises(KeyError):
            get_rule("nonexistent_rule")

    def test_get_rule_all_ids(self):
        """验证 detector_ids 返回所有 ID。"""
        ids = detector_ids()
        for rule_id in SOFT_DETECTOR_RULES:
            assert rule_id in ids


class TestRuleSpecificFields:
    """具体规则字段验证测试。"""

    def test_waf_rule_fields(self):
        """验证 WAF 攻击规则字段。"""
        rule = SOFT_DETECTOR_RULES["waf_attack_success_candidate"]
        assert rule.severity_hint == "high"
        assert rule.lookback_seconds == 900
        assert "event_time" in rule.required_fields
        assert "source_product" in rule.required_fields

    def test_web_to_process_anomaly_fields(self):
        """验证 Web 到进程异常规则。"""
        rule = SOFT_DETECTOR_RULES["web_to_process_anomaly"]
        assert rule.severity_hint == "high"
        assert rule.min_confidence_for_case == 0.62
        assert "T1059" in rule.techniques
        assert "T1105" in rule.techniques

    def test_vpn_new_geo_login_fields(self):
        """验证 VPN 新地域登录规则。"""
        rule = SOFT_DETECTOR_RULES["vpn_new_geo_login"]
        assert rule.lookback_seconds == 3600
        assert "T1078" in rule.techniques

    def test_bruteforce_then_success_fields(self):
        """验证暴力破解后成功规则。"""
        rule = SOFT_DETECTOR_RULES["bruteforce_then_success"]
        assert rule.severity_hint == "high"
        assert rule.min_confidence_for_case == 0.62
        assert rule.lookback_seconds == 1800

    def test_rare_egress_after_alert_fields(self):
        """验证告警后罕见出口流量规则。"""
        rule = SOFT_DETECTOR_RULES["rare_egress_after_alert"]
        assert rule.min_confidence_for_case == 0.62

    def test_multi_source_weak_signal_fields(self):
        """验证多源弱信号规则。"""
        rule = SOFT_DETECTOR_RULES["multi_source_weak_signal"]
        assert rule.severity_hint == "medium"
        assert rule.min_confidence_for_case == 0.58


class TestRuleVersion:
    """RULE_VERSION 常量测试。"""

    def test_rule_version_string(self):
        """验证版本是字符串格式。"""
        assert isinstance(RULE_VERSION, str)

    def test_rule_version_value(self):
        """验证版本号格式。"""
        assert RULE_VERSION == "v1"
