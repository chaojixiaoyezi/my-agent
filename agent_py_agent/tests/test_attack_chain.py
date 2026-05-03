"""攻击链分析测试 - attack_chain.py 攻击链分析、阶段关联、因果推断。"""
from __future__ import annotations

from agent_py_agent.agent.log_analysis.security import (
    AttackChainStep,
    build_attack_chain,
    lateral_movement_signs,
)
from agent_py_agent.agent.log_analysis.models import CaseRecord, Finding, EvidenceRef


class TestAttackChainStep:
    """AttackChainStep 数据类测试。"""

    def test_step_required_fields(self):
        """验证必需字段。"""
        step = AttackChainStep(
            time="2024-01-01T00:00:00Z",
            stage="initial_access",
            action="Web exploit",
        )
        assert step.time == "2024-01-01T00:00:00Z"
        assert step.stage == "initial_access"
        assert step.action == "Web exploit"

    def test_step_defaults(self):
        """验证默认值。"""
        step = AttackChainStep(time="t", stage="s", action="a")
        assert step.entities == {}
        assert step.evidence_refs == []
        assert step.basis == "finding"
        assert step.confidence == 0.0

    def test_step_to_dict(self):
        """验证转换为字典。"""
        step = AttackChainStep(
            time="2024-01-01T00:00:00Z",
            stage="execution",
            action="process injection",
            entities={"host": ["server-1"]},
            confidence=0.85,
        )
        d = step.to_dict()
        assert d["stage"] == "execution"
        assert d["entities"]["host"] == ["server-1"]
        assert d["confidence"] == 0.85


class TestBuildAttackChain:
    """build_attack_chain 函数测试。"""

    def test_build_from_single_finding(self):
        """从单个 Finding 构建攻击链（传入列表）。"""
        finding = Finding(
            finding_id="finding-1",
            detector_id="waf_attack_success_candidate",
            window=["2024-01-01T00:00:00Z"],
            entities={"attacker_ip": ["1.2.3.4"]},
            confidence=0.9,
        )
        steps = build_attack_chain([finding])  # 传入列表
        assert len(steps) == 1
        assert steps[0].stage == "initial_access"
        assert steps[0].confidence == 0.9

    def test_build_from_findings_list(self):
        """从 Findings 列表构建攻击链。"""
        findings = [
            Finding(
                finding_id="f1",
                detector_id="waf_attack_success_candidate",
                window=["2024-01-01T00:00:00Z"],
                entities={},
            ),
            Finding(
                finding_id="f2",
                detector_id="web_to_process_anomaly",
                window=["2024-01-01T00:01:00Z"],
                entities={},
            ),
        ]
        steps = build_attack_chain(findings)
        assert len(steps) == 2
        assert steps[0].stage == "initial_access"
        assert steps[1].stage == "execution"

    def test_build_from_case_record(self):
        """从 CaseRecord 构建攻击链。"""
        case = CaseRecord(
            case_id="case-1",
            title="Test",
            attributes={
                "finding_summaries": [
                    {
                        "finding_id": "f1",
                        "detector_id": "vpn_new_geo_login",
                        "window": ["2024-01-01T00:00:00Z"],
                        "entities": {"user": ["admin"]},
                        "confidence": 0.8,
                    }
                ]
            },
        )
        steps = build_attack_chain(case)
        assert len(steps) == 1
        assert steps[0].stage == "initial_access"

    def test_build_sorted_by_window(self):
        """验证按时间窗口排序。"""
        findings = [
            Finding(finding_id="f2", detector_id="web_to_process_anomaly", window=["2024-01-02T00:00:00Z"], entities={}),
            Finding(finding_id="f1", detector_id="waf_attack_success_candidate", window=["2024-01-01T00:00:00Z"], entities={}),
            Finding(finding_id="f3", detector_id="bruteforce_then_success", window=["2024-01-03T00:00:00Z"], entities={}),
        ]
        steps = build_attack_chain(findings)
        assert steps[0].stage == "initial_access"
        assert steps[1].stage == "execution"
        assert steps[2].stage == "credential_access"

    def test_build_unknown_detector(self):
        """未知检测器映射到 unknown 阶段。"""
        finding = Finding(
            finding_id="f1",
            detector_id="unknown_detector",
            window=["2024-01-01T00:00:00Z"],
            entities={},
        )
        steps = build_attack_chain([finding])
        assert steps[0].stage == "unknown"

    def test_build_uses_risk_score_when_no_confidence(self):
        """无 confidence 时使用 risk_score。"""
        finding = Finding(
            finding_id="f1",
            detector_id="waf_attack_success_candidate",
            window=["2024-01-01T00:00:00Z"],
            entities={},
            risk_score=0.75,
        )
        steps = build_attack_chain([finding])
        assert steps[0].confidence == 0.75


class TestLateralMovementSigns:
    """lateral_movement_signs 函数测试。"""

    def test_bruteforce_then_success(self):
        """检测暴力破解后成功登录。"""
        finding = Finding(
            finding_id="f1",
            detector_id="bruteforce_then_success",
            hypothesis="暴力破解后成功登录",
            entities={"victim_ip": ["10.0.0.1"], "user": ["admin"]},
            confidence=0.9,
            evidence_refs=[],
        )
        signs = lateral_movement_signs([finding])
        assert len(signs) == 1
        assert signs[0]["kind"] == "identity_to_asset_access"
        assert signs[0]["confidence"] == 0.9

    def test_vpn_new_geo_login(self):
        """检测新型地理登录。"""
        finding = Finding(
            finding_id="f1",
            detector_id="vpn_new_geo_login",
            hypothesis="VPN 新地理来源登录",
            entities={"user": ["admin"]},
            confidence=0.85,
            evidence_refs=[],
        )
        signs = lateral_movement_signs([finding])
        assert len(signs) == 1
        assert signs[0]["kind"] == "identity_to_asset_access"

    def test_multi_target_activity(self):
        """检测多目标活动。"""
        finding = Finding(
            finding_id="f1",
            detector_id="some_detector",
            entities={
                "victim_ip": ["10.0.0.1", "10.0.0.2", "10.0.0.3"],
                "dst_ip": ["10.0.0.4"],
            },
            confidence=0.7,
            evidence_refs=[],
        )
        signs = lateral_movement_signs([finding])
        multi_target = next((s for s in signs if s["kind"] == "multi_target_activity"), None)
        assert multi_target is not None
        assert len(multi_target["targets"]) >= 3

    def test_no_signs_for_normal_activity(self):
        """正常活动无异常标记。"""
        finding = Finding(
            finding_id="f1",
            detector_id="low_priority_detector",
            entities={"victim_ip": ["10.0.0.1"]},
            confidence=0.3,
            evidence_refs=[],
        )
        signs = lateral_movement_signs([finding])
        assert len(signs) == 0

    def test_signs_extract_ref_ids(self):
        """验证提取引用 ID。"""
        finding = Finding(
            finding_id="f1",
            detector_id="bruteforce_then_success",
            entities={},
            confidence=0.9,
            evidence_refs=[
                EvidenceRef(evidence_id="ev-1"),
                {"evidence_id": "ev-2"},
                "ev-3",
            ],
        )
        signs = lateral_movement_signs([finding])
        assert "ev-1" in signs[0]["evidence_refs"]
        assert "ev-2" in signs[0]["evidence_refs"]
        assert "ev-3" in signs[0]["evidence_refs"]


class TestDetectorStageMapping:
    """检测器阶段映射测试。"""

    def test_all_known_detectors_mapped(self):
        """验证所有已知检测器都被正确映射。"""
        known_detectors = [
            ("waf_attack_success_candidate", "initial_access"),
            ("web_to_process_anomaly", "execution"),
            ("vpn_new_geo_login", "initial_access"),
            ("bruteforce_then_success", "credential_access"),
            ("rare_egress_after_alert", "command_and_control"),
            ("multi_source_weak_signal", "correlation"),
        ]
        for detector_id, expected_stage in known_detectors:
            finding = Finding(
                finding_id="f1",
                detector_id=detector_id,
                window=["2024-01-01T00:00:00Z"],
                entities={},
            )
            steps = build_attack_chain([finding])
            assert steps[0].stage == expected_stage, f"{detector_id} should map to {expected_stage}"


class TestEdgeCases:
    """边界场景测试。"""

    def test_empty_findings(self):
        """空 findings 返回空步骤。"""
        steps = build_attack_chain([])
        assert steps == []

    def test_finding_without_window(self):
        """无时间窗口的 Finding 处理。"""
        finding = Finding(
            finding_id="f1",
            detector_id="waf_attack_success_candidate",
            window=[],
            entities={},
        )
        steps = build_attack_chain([finding])
        assert len(steps) == 1
        assert steps[0].time == ""

    def test_finding_with_none_confidence(self):
        """None confidence 处理。"""
        finding = Finding(
            finding_id="f1",
            detector_id="waf_attack_success_candidate",
            window=["2024-01-01T00:00:00Z"],
            entities={},
            confidence=None,
            risk_score=0.6,
        )
        steps = build_attack_chain([finding])
        assert steps[0].confidence == 0.6

    def test_case_without_findings(self):
        """无 findings 的 Case 处理。"""
        case = CaseRecord(
            case_id="case-1",
            title="Empty case",
            attributes={},
        )
        steps = build_attack_chain(case)
        assert steps == []

    def test_mapping_case_with_empty_findings_list(self):
        """Case attributes 中有空 findings 列表。"""
        case = CaseRecord(
            case_id="case-1",
            title="Test",
            attributes={"finding_summaries": []},
        )
        steps = build_attack_chain(case)
        assert steps == []
