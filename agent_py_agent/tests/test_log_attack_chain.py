"""攻击链测试 - attack_chain.py 攻击链构建、事件关联、时间线排序。"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.log_analysis.security.attack_chain import (
    AttackChainStep,
    build_attack_chain,
    lateral_movement_signs,
)
from agent_py_agent.agent.log_analysis.models import EvidenceRef


class TestAttackChainStep:
    """AttackChainStep 数据类测试。"""

    def test_step_required_fields(self, sample_attack_chain_step: AttackChainStep):
        """验证必需字段。"""
        assert sample_attack_chain_step.time == "2024-01-01T10:00:00Z"
        assert sample_attack_chain_step.stage == "initial_access"
        assert sample_attack_chain_step.action == "Web exploit"

    def test_step_defaults(self):
        """验证默认值。"""
        step = AttackChainStep(time="t", stage="s", action="a")
        assert step.entities == {}
        assert step.evidence_refs == []
        assert step.basis == "finding"
        assert step.confidence == 0.0

    def test_step_to_dict(self, sample_attack_chain_step: AttackChainStep):
        """验证字典转换。"""
        d = sample_attack_chain_step.to_dict()
        assert d["stage"] == "initial_access"
        assert d["entities"]["attacker_ip"] == ["1.2.3.4"]
        assert d["confidence"] == 0.85


class TestBuildAttackChain:
    """build_attack_chain 攻击链构建测试。"""

    def test_empty_findings(self, make_finding_func):
        """验证空输入返回空列表。"""
        result = build_attack_chain([])
        assert result == []

    def test_single_finding(self, make_finding_func):
        """验证单一发现构建步骤。"""
        finding = make_finding_func(
            detector_id="waf_attack_success_candidate",
            window=["2024-01-01T10:00:00Z"],
            entities={"attacker_ip": ["1.2.3.4"]},
        )
        result = build_attack_chain([finding])
        assert len(result) == 1
        assert result[0].stage == "initial_access"

    def test_findings_sorted_by_window(self, make_finding_func):
        """验证发现按时间窗口排序。"""
        findings = [
            make_finding_func(
                detector_id="bruteforce_then_success",
                window=["2024-01-01T12:00:00Z"],
                entities={"user": ["alice"]},
            ),
            make_finding_func(
                detector_id="waf_attack_success_candidate",
                window=["2024-01-01T10:00:00Z"],
                entities={"attacker_ip": ["1.2.3.4"]},
            ),
        ]
        result = build_attack_chain(findings)
        assert result[0].stage == "initial_access"
        assert result[1].stage == "credential_access"

    def test_all_detector_stages(self, make_finding_func):
        """验证所有检测器类型映射到正确阶段。"""
        test_cases = [
            ("waf_attack_success_candidate", "initial_access"),
            ("web_to_process_anomaly", "execution"),
            ("vpn_new_geo_login", "initial_access"),
            ("bruteforce_then_success", "credential_access"),
            ("rare_egress_after_alert", "command_and_control"),
            ("multi_source_weak_signal", "correlation"),
            ("unknown_detector", "unknown"),
        ]
        for detector_id, expected_stage in test_cases:
            finding = make_finding_func(detector_id=detector_id, window=["2024-01-01T10:00:00Z"])
            result = build_attack_chain([finding])
            assert len(result) == 1
            assert result[0].stage == expected_stage, f"detector_id={detector_id}"


class TestLateralMovementSigns:
    """lateral_movement_signs 横向移动检测测试。"""

    def test_empty_findings(self, make_finding_func):
        """验证空输入返回空列表。"""
        result = lateral_movement_signs([])
        assert result == []

    def test_bruteforce_sign(self, make_finding_func, sample_evidence_ref: EvidenceRef):
        """验证暴力破解产生横向移动迹象。"""
        findings = [
            make_finding_func(
                detector_id="bruteforce_then_success",
                window=["2024-01-01T10:00:00Z"],
                entities={"victim_ip": ["10.0.0.1"], "user": ["alice"]},
                evidence_refs=[sample_evidence_ref],
                hypothesis="Credential compromise",
            )
        ]
        result = lateral_movement_signs(findings)
        assert len(result) >= 1
        assert result[0]["kind"] == "identity_to_asset_access"

    def test_vpn_new_geo_sign(self, make_finding_func, sample_evidence_ref: EvidenceRef):
        """验证 VPN 新地域登录产生横向移动迹象。"""
        findings = [
            make_finding_func(
                detector_id="vpn_new_geo_login",
                window=["2024-01-01T10:00:00Z"],
                entities={"victim_ip": ["10.0.0.1"], "user": ["alice"]},
                evidence_refs=[sample_evidence_ref],
                hypothesis="Novel VPN login",
            )
        ]
        result = lateral_movement_signs(findings)
        assert len(result) >= 1
        assert any(s["kind"] == "identity_to_asset_access" for s in result)

    def test_multi_target_activity(self, make_finding_func):
        """验证多目标活动检测。"""
        findings = [
            make_finding_func(
                detector_id="web_to_process_anomaly",
                window=["2024-01-01T10:00:00Z"],
                entities={
                    "victim_ip": ["10.0.0.1", "10.0.0.2", "10.0.0.3"],
                    "dst_ip": ["8.8.8.8", "1.1.1.1"],
                },
            )
        ]
        result = lateral_movement_signs(findings)
        assert any(s["kind"] == "multi_target_activity" for s in result)

    def test_multi_target_requires_three_targets(self, make_finding_func):
        """验证需要至少三个目标才触发多目标活动。"""
        findings = [
            make_finding_func(
                detector_id="web_to_process_anomaly",
                window=["2024-01-01T10:00:00Z"],
                entities={"victim_ip": ["10.0.0.1", "10.0.0.2"]},
            )
        ]
        result = lateral_movement_signs(findings)
        assert not any(s["kind"] == "multi_target_activity" for s in result)


class TestBuildAttackChainWithMappings:
    """映射输入测试。"""

    def test_mapping_with_findings(self):
        """验证带 findings 的映射输入。"""
        mapping = {
            "findings": [
                {
                    "finding_id": "f-map-001",
                    "detector_id": "waf_attack_success_candidate",
                    "window": ["2024-01-01T10:00:00Z"],
                    "entities": {"attacker_ip": ["1.2.3.4"]},
                    "confidence": 0.75,
                    "risk_score": 0.75,
                    "evidence_refs": [],
                }
            ]
        }
        result = build_attack_chain(mapping)
        assert len(result) == 1
        assert result[0].stage == "initial_access"

    def test_mapping_with_attributes(self):
        """验证带 attributes 的映射输入。"""
        mapping = {
            "attributes": {
                "finding_summaries": [
                    {
                        "finding_id": "f-map-002",
                        "detector_id": "vpn_new_geo_login",
                        "window": ["2024-01-01T10:00:00Z"],
                        "entities": {"user": ["alice"]},
                        "confidence": 0.6,
                        "risk_score": 0.6,
                        "evidence_refs": [],
                    }
                ]
            }
        }
        result = build_attack_chain(mapping)
        assert len(result) == 1
        assert result[0].stage == "initial_access"

    def test_single_mapping_as_finding(self):
        """验证单个映射作为发现处理。"""
        mapping = {
            "finding_id": "f-map-003",
            "detector_id": "bruteforce_then_success",
            "window": ["2024-01-01T10:00:00Z"],
            "entities": {"user": ["alice"]},
            "confidence": 0.7,
            "risk_score": 0.7,
            "evidence_refs": [],
        }
        result = build_attack_chain(mapping)
        assert len(result) == 1
        assert result[0].stage == "credential_access"


class TestEvidenceRefsHandling:
    """证据引用处理测试。"""

    def test_evidence_refs_from_strings(self, make_finding_func):
        """验证字符串证据引用。"""
        findings = [
            make_finding_func(
                detector_id="waf_attack_success_candidate",
                window=["2024-01-01T10:00:00Z"],
                entities={},
                evidence_refs=["ev-1", "ev-2"],
            )
        ]
        result = build_attack_chain(findings)
        assert "ev-1" in result[0].evidence_refs
        assert "ev-2" in result[0].evidence_refs

    def test_evidence_refs_from_evidence_refs(self, make_finding_func, sample_evidence_ref: EvidenceRef):
        """验证 EvidenceRef 对象列表。"""
        findings = [
            make_finding_func(
                detector_id="waf_attack_success_candidate",
                window=["2024-01-01T10:00:00Z"],
                entities={},
                evidence_refs=[sample_evidence_ref],
            )
        ]
        result = build_attack_chain(findings)
        assert sample_evidence_ref.evidence_id in result[0].evidence_refs


class TestConfidenceFallback:
    """置信度回退测试。"""

    def test_uses_confidence_when_present(self, make_finding_func):
        """验证有 confidence 时使用 confidence。"""
        findings = [
            make_finding_func(
                detector_id="waf_attack_success_candidate",
                window=["2024-01-01T10:00:00Z"],
                entities={},
                confidence=0.85,
                risk_score=0.6,
            )
        ]
        result = build_attack_chain(findings)
        assert result[0].confidence == 0.85

    def test_falls_back_to_risk_score(self, make_finding_func):
        """验证 confidence 为 None 时回退到 risk_score。"""
        findings = [
            make_finding_func(
                detector_id="waf_attack_success_candidate",
                window=["2024-01-01T10:00:00Z"],
                entities={},
                confidence=None,
                risk_score=0.72,
            )
        ]
        result = build_attack_chain(findings)
        assert result[0].confidence == 0.72


class TestEmptyWindow:
    """空时间窗口测试。"""

    def test_empty_window_uses_empty_string(self, make_finding_func):
        """验证空时间窗口使用空字符串。"""
        findings = [
            make_finding_func(
                detector_id="waf_attack_success_candidate",
                window=[],
                entities={},
            )
        ]
        result = build_attack_chain(findings)
        assert result[0].time == ""

    def test_none_window_uses_empty_string(self, make_finding_func):
        """验证 None 时间窗口使用空字符串。"""
        findings = [
            make_finding_func(
                detector_id="waf_attack_success_candidate",
                window=None,
                entities={},
            )
        ]
        result = build_attack_chain(findings)
        assert result[0].time == ""


class TestLateralMovementWithDifferentDetectors:
    """不同检测器的横向移动测试。"""

    def test_unknown_detector_no_lateral_signs(self, make_finding_func):
        """验证未知检测器不产生横向移动迹象。"""
        findings = [
            make_finding_func(
                detector_id="unknown_detector",
                window=["2024-01-01T10:00:00Z"],
                entities={"victim_ip": ["10.0.0.1"]},
            )
        ]
        result = lateral_movement_signs(findings)
        multi_target = [s for s in result if s["kind"] == "multi_target_activity"]
        assert len(multi_target) == 0


class TestEntitiesExtraction:
    """实体提取测试。"""

    def test_entities_preserved_in_steps(self, make_finding_func):
        """验证实体被保留在攻击链步骤中。"""
        findings = [
            make_finding_func(
                detector_id="vpn_new_geo_login",
                window=["2024-01-01T10:00:00Z"],
                entities={"user": ["alice"], "attacker_ip": ["1.2.3.4"], "country": ["US"]},
            )
        ]
        result = build_attack_chain(findings)
        assert "user" in result[0].entities
        assert "alice" in result[0].entities["user"]
