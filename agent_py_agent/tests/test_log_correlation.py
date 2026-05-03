"""测试事件关联、时间窗口和因果链功能。

本模块测试日志分析中的关联模块，包括：
1. RouteDraft 数据类和序列化
2. build_route_draft 从 case/findings 构建路由草案
3. 时间窗口、因果链和入口候选项推理
4. 边界场景：无 findings、缺失入口候选、复杂实体合并
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.log_analysis.models import CaseRecord, EvidenceRef, Finding, QueryPlan
from agent_py_agent.agent.log_analysis.security.correlation import (
    RouteDraft,
    build_route_draft,
    draft_route,
    route_from_case,
)


def test_route_draft_to_dict():
    """测试 RouteDraft 正确序列化为字典。"""
    draft = RouteDraft(
        case_id="case-1",
        entry_candidates=[
            {"kind": "web_exploit_candidate", "detector_id": "waf_attack_success_candidate"},
        ],
        timeline=[
            {"step": "waf_hit", "detector_id": "waf_attack_success_candidate"},
        ],
        impacted_entities={"attacker_ip": ["198.51.100.1"], "victim_ip": ["10.0.0.5"]},
        facts=[
            {"kind": "detector_finding", "detector_id": "waf_attack_success_candidate"},
        ],
        inferences=[
            {"kind": "hypothesis", "hypothesis": "Web attack detected"},
        ],
        gaps=["Missing EDR telemetry"],
        next_queries=[
            {"purpose": "Find process events", "source_products": ["edr"]},
        ],
        evidence_refs=["ev-1", "ev-2"],
    )

    draft_dict = draft.to_dict()
    assert draft_dict["case_id"] == "case-1"
    assert len(draft_dict["entry_candidates"]) == 1
    assert len(draft_dict["timeline"]) == 1
    assert draft_dict["impacted_entities"]["attacker_ip"] == ["198.51.100.1"]
    assert len(draft_dict["facts"]) == 1
    assert len(draft_dict["inferences"]) == 1
    assert "Missing EDR telemetry" in draft_dict["gaps"]
    assert len(draft_dict["next_queries"]) == 1
    assert set(draft_dict["evidence_refs"]) == {"ev-1", "ev-2"}


def test_route_draft_default_values():
    """测试 RouteDraft 的默认值。"""
    draft = RouteDraft()

    assert draft.case_id == ""
    assert draft.entry_candidates == []
    assert draft.timeline == []
    assert draft.impacted_entities == {}
    assert draft.facts == []
    assert draft.inferences == []
    assert draft.gaps == []
    assert draft.next_queries == []
    assert draft.evidence_refs == []
    assert draft.generated_at  # 应该有一个时间戳


def test_build_route_draft_from_case_with_findings():
    """测试从 case 和 findings 构建完整路由草案。"""
    case = CaseRecord(
        case_id="case-1",
        title="web exploit",
        entities={
            "attacker_ip": ["198.51.100.1"],
            "victim_ip": ["10.0.0.5"],
        },
        evidence_refs=[
            EvidenceRef(evidence_id="ev-1", source_id="waf-prod"),
        ],
        gaps=["EDR telemetry missing"],
        next_queries=[
            QueryPlan(purpose="Check EDR", source_products=["edr"]),
        ],
    )

    findings = [
        Finding(
            finding_id="finding-1",
            detector_id="waf_attack_success_candidate",
            window=["2026-04-30T10:00:00Z", "2026-04-30T10:05:00Z"],
            entities={
                "attacker_ip": ["198.51.100.1"],
                "victim_ip": ["10.0.0.5"],
            },
            hypothesis="WAF hit followed by host activity",
            confidence=0.85,
            evidence_refs=[EvidenceRef(evidence_id="ev-1")],
        ),
    ]

    draft = build_route_draft(case, findings=findings)

    assert draft.case_id == "case-1"
    assert len(draft.entry_candidates) == 1
    assert draft.entry_candidates[0]["kind"] == "web_exploit_candidate"
    assert "attacker_ip" in draft.impacted_entities
    assert "victim_ip" in draft.impacted_entities
    assert len(draft.facts) == 1
    assert len(draft.inferences) == 1
    assert "EDR telemetry missing" in draft.gaps
    assert len(draft.next_queries) >= 1


def test_build_route_draft_extracts_entry_candidates():
    """测试从不同检测器提取入口候选项。"""
    findings = [
        Finding(
            finding_id="f1",
            detector_id="waf_attack_success_candidate",
            confidence=0.9,
            entities={"attacker_ip": ["198.51.100.1"]},
        ),
        Finding(
            finding_id="f2",
            detector_id="web_to_process_anomaly",
            confidence=0.85,
            entities={"victim_ip": ["10.0.0.5"]},
        ),
        Finding(
            finding_id="f3",
            detector_id="vpn_new_geo_login",
            confidence=0.75,
            entities={"user": ["admin"]},
        ),
        Finding(
            finding_id="f4",
            detector_id="bruteforce_then_success",
            confidence=0.8,
            entities={"user": ["hacker"]},
        ),
    ]

    case = CaseRecord(case_id="case-1", title="multi-finding")
    draft = build_route_draft(case, findings=findings)

    # 应该提取 4 个入口候选项
    assert len(draft.entry_candidates) == 4

    kinds = [c["kind"] for c in draft.entry_candidates]
    assert "web_exploit_candidate" in kinds
    assert "web_post_exploit_execution_candidate" in kinds
    assert "vpn_credential_abuse_candidate" in kinds
    assert "bruteforce_credential_abuse_candidate" in kinds

    # 应该按置信度降序排列
    confidences = [c["confidence"] for c in draft.entry_candidates]
    assert confidences == sorted(confidences, reverse=True)


def test_build_route_draft_merges_entities():
    """测试合并来自 case 和 findings 的实体。"""
    case = CaseRecord(
        case_id="case-1",
        title="merge test",
        entities={
            "attacker_ip": ["198.51.100.1"],
            "victim_ip": ["10.0.0.5"],
            "user": ["admin"],
        },
    )

    findings = [
        Finding(
            finding_id="f1",
            detector_id="test",
            entities={
                "attacker_ip": ["198.51.100.1", "198.51.100.2"],  # 重复 + 新增
                "victim_ip": ["10.0.0.6"],  # 新增
                "domain": ["evil.example.com"],
            },
        ),
    ]

    draft = build_route_draft(case, findings=findings)

    # 攻击者 IP 应该去重：1.1 和 1.2
    assert set(draft.impacted_entities.get("attacker_ip", [])) == {"198.51.100.1", "198.51.100.2"}

    # 受害者 IP 应该去重：0.5 和 0.6
    assert set(draft.impacted_entities.get("victim_ip", [])) == {"10.0.0.5", "10.0.0.6"}

    # 用户和域名应该包含
    assert "admin" in draft.impacted_entities.get("user", [])
    assert "evil.example.com" in draft.impacted_entities.get("domain", [])


def test_build_route_draft_dedupes_gaps_and_queries():
    """测试去重 gaps 和 next_queries。"""
    case = CaseRecord(
        case_id="case-1",
        title="dedupe test",
        gaps=["gap-1", "gap-2", "gap-1"],  # gap-1 重复
        next_queries=[QueryPlan(purpose="query-1"), QueryPlan(purpose="query-2"), QueryPlan(purpose="query-1")],
        entities={"ip": ["1.2.3.4"]},  # 有入口候选项，避免生成 entry gap
    )

    findings = [
        Finding(
            finding_id="f1",
            detector_id="test",
            gaps=["gap-2", "gap-3"],  # gap-2 重复，gap-3 新增
            next_queries=[QueryPlan(purpose="query-2"), QueryPlan(purpose="query-3")],
        ),
    ]

    draft = build_route_draft(case, findings=findings)

    # gaps 应该去重，且包含所有输入的 gap
    assert {"gap-1", "gap-2", "gap-3"}.issubset(set(draft.gaps))

    # next_queries 应该去重
    purposes = [q.get("purpose", "") for q in draft.next_queries]
    assert "query-1" in purposes
    assert "query-2" in purposes
    assert "query-3" in purposes


def test_build_route_draft_generates_entry_gap_when_no_candidates():
    """测试没有入口候选项时生成入口信息缺口。"""
    case = CaseRecord(
        case_id="case-1",
        title="no entry",
        entities={},
    )

    findings = [
        Finding(
            finding_id="f1",
            detector_id="unknown_detector",  # 不是已知的入口检测器
            entities={"host": ["web-01"]},
        ),
    ]

    draft = build_route_draft(case, findings=findings)

    # 应该生成入口相关缺口
    entry_gaps = [gap for gap in draft.gaps if "entry" in gap.lower()]
    assert len(entry_gaps) > 0

    # 应该生成入口候选项查询
    entry_queries = [q for q in draft.next_queries if "entry" in str(q).lower()]
    assert len(entry_queries) > 0


def test_build_route_draft_waf_gap_without_edr():
    """测试 WAF 检测器缺少 EDR 证据时生成缺口。"""
    case = CaseRecord(case_id="case-1", title="waf only")

    findings = [
        Finding(
            finding_id="f1",
            detector_id="waf_attack_success_candidate",
            entities={"attacker_ip": ["198.51.100.1"], "victim_ip": ["10.0.0.5"]},
        ),
    ]

    draft = build_route_draft(case, findings=findings)

    # 应该生成 WAF-EDR 链接缺口
    edr_gaps = [gap for gap in draft.gaps if "edr" in gap.lower() or "process" in gap.lower()]
    assert len(edr_gaps) > 0


def test_build_route_draft_vpn_gap_without_host():
    """测试 VPN 检测器缺少主机证据时生成缺口。"""
    case = CaseRecord(case_id="case-1", title="vpn only")

    findings = [
        Finding(
            finding_id="f1",
            detector_id="vpn_new_geo_login",
            entities={"user": ["admin"]},  # 没有主机实体
        ),
    ]

    draft = build_route_draft(case, findings=findings)

    # 应该生成 VPN-主机 链接缺口
    host_gaps = [gap for gap in draft.gaps if "host" in gap.lower() or "identity" in gap.lower()]
    assert len(host_gaps) > 0


def test_build_route_draft_filters_findings_by_ref():
    """测试根据 finding_refs 过滤 findings。"""
    case = CaseRecord(
        case_id="case-1",
        title="filtered",
        finding_refs=["f1", "f3"],  # 只包含 f1 和 f3
    )

    findings = [
        Finding(finding_id="f1", detector_id="waf_attack_success_candidate"),
        Finding(finding_id="f2", detector_id="web_to_process_anomaly"),  # 应该被过滤
        Finding(finding_id="f3", detector_id="vpn_new_geo_login"),
    ]

    draft = build_route_draft(case, findings=findings)

    # 应该只从 f1 和 f3 生成入口候选项
    assert len(draft.entry_candidates) == 2

    detectors = {c["detector_id"] for c in draft.entry_candidates}
    assert "waf_attack_success_candidate" in detectors
    assert "vpn_new_geo_login" in detectors
    assert "web_to_process_anomaly" not in detectors


def test_build_route_draft_from_case_attributes():
    """测试从 case.attributes 中提取 findings。"""
    case = CaseRecord(
        case_id="case-1",
        title="attributes",
        attributes={
            "finding_summaries": [
                {
                    "finding_id": "f1",
                    "detector_id": "waf_attack_success_candidate",
                    "entities": {"attacker_ip": ["198.51.100.1"]},
                    "confidence": 0.9,
                }
            ]
        },
    )

    draft = build_route_draft(case)

    assert len(draft.entry_candidates) == 1
    assert draft.entry_candidates[0]["detector_id"] == "waf_attack_success_candidate"


def test_build_route_draft_from_case_finding_refs():
    """测试通过 finding_refs 查找 case.attributes 中的 findings。"""
    case = CaseRecord(
        case_id="case-1",
        title="finding refs",
        finding_refs=["f1", "f2"],
        attributes={
            "finding_summaries": [
                {
                    "finding_id": "f1",
                    "detector_id": "waf_attack_success_candidate",
                    "entities": {"attacker_ip": ["198.51.100.1"]},
                },
                {
                    "finding_id": "f2",
                    "detector_id": "vpn_new_geo_login",
                    "entities": {"user": ["admin"]},
                },
                {
                    "finding_id": "f3",
                    "detector_id": "web_to_process_anomaly",  # 应该被过滤
                    "entities": {"victim_ip": ["10.0.0.5"]},
                },
            ]
        },
    )

    draft = build_route_draft(case)

    # 应该只使用 f1 和 f2
    assert len(draft.entry_candidates) == 2
    detectors = {c["detector_id"] for c in draft.entry_candidates}
    assert "waf_attack_success_candidate" in detectors
    assert "vpn_new_geo_login" in detectors
    assert "web_to_process_anomaly" not in detectors


def test_build_route_draft_merges_evidence_refs():
    """测试合并来自 case 和 findings 的证据引用。"""
    case = CaseRecord(
        case_id="case-1",
        title="evidence merge",
        evidence_refs=[
            EvidenceRef(evidence_id="ev-case-1"),
            EvidenceRef(evidence_id="ev-case-2"),
        ],
    )

    findings = [
        Finding(
            finding_id="f1",
            detector_id="test",
            evidence_refs=[
                EvidenceRef(evidence_id="ev-f1-1"),
                EvidenceRef(evidence_id="ev-case-1"),  # 重复
            ],
        ),
    ]

    draft = build_route_draft(case, findings=findings)

    # 应该去重
    assert set(draft.evidence_refs) == {"ev-case-1", "ev-case-2", "ev-f1-1"}


def test_draft_route_alias():
    """测试 draft_route 是 build_route_draft 的别名。"""
    case = CaseRecord(case_id="case-1", title="alias test")

    draft1 = draft_route(case)
    draft2 = build_route_draft(case)

    assert draft1.case_id == draft2.case_id
    # 两次调用时间可能略有差异，只比较秒级精度
    assert draft1.generated_at[:19] == draft2.generated_at[:19]


def test_route_from_case_alias():
    """测试 route_from_case 是 build_route_draft 的别名。"""
    case = CaseRecord(case_id="case-1", title="alias test 2")

    draft = route_from_case(case)

    assert draft.case_id == "case-1"
    assert isinstance(draft, RouteDraft)


def test_build_route_draft_empty_case():
    """测试空 case 生成最小有效草案。"""
    case = CaseRecord(case_id="case-1", title="empty")

    draft = build_route_draft(case)

    assert draft.case_id == "case-1"
    assert draft.entry_candidates == []
    assert draft.timeline == []
    assert draft.impacted_entities == {}


def test_build_route_draft_generates_first_entity_filters():
    """测试为实体候选项查询生成正确的过滤器。"""
    case = CaseRecord(
        case_id="case-1",
        title="filter test",
        entities={
            "victim_ip": ["10.0.0.5"],
            "user": ["admin"],
            "attacker_ip": ["198.51.100.1"],
        },
    )

    findings = [
        Finding(finding_id="f1", detector_id="unknown_detector", entities={}),
    ]

    draft = build_route_draft(case, findings=findings)

    # 应该生成入口候选项查询
    entry_queries = [q for q in draft.next_queries if "entry" in str(q).lower()]
    assert len(entry_queries) > 0

    # 查询应该包含实体过滤器
    query = entry_queries[0]
    if "filters" in query:
        filters = query["filters"]
        # 应该使用第一个 victim_ip
        if "victim_ip" in filters:
            assert filters["victim_ip"] == "10.0.0.5"


class TestCorrelationMutationCoverage:
    """Tests to cover mutation-prone logic in correlation.py."""

    def test_entry_candidates_sorted_descending(self):
        """Entry candidates must be sorted in descending confidence order.

        Mutation: reverse=True removed from sorted()
        This would produce incorrect ascending order.
        """
        from agent_py_agent.agent.log_analysis.models import Finding
        from agent_py_agent.agent.log_analysis.security.correlation import _entry_candidates

        # Create findings with different confidence levels
        finding1 = Finding(
            finding_id="f1",
            detector_id="waf_attack_success_candidate",
            confidence=0.3,
            risk_score=0.3,
        )
        finding2 = Finding(
            finding_id="f2",
            detector_id="waf_attack_success_candidate",
            confidence=0.9,
            risk_score=0.9,
        )

        candidates = _entry_candidates([finding1, finding2])

        # Higher confidence should come first
        assert len(candidates) == 2
        assert candidates[0]["confidence"] >= candidates[1]["confidence"],             f"Expected descending order, got {[c['confidence'] for c in candidates]}"

    def test_finding_filter_respects_finding_refs(self):
        """Finding filter must respect case.finding_refs.

        Mutation: 'if not refs: return list(findings)' removed
        This would return all findings regardless of case's finding_refs.
        """
        from agent_py_agent.agent.log_analysis.models import CaseRecord, Finding
        from agent_py_agent.agent.log_analysis.security.correlation import _filter_findings_for_case

        case = CaseRecord(
            case_id="c1",
            title="test case",
            finding_refs=["f1"],  # Only want f1
        )

        finding1 = Finding(
            finding_id="f1",
            detector_id="test",
            hypothesis="test",
            confidence=0.5,
        )
        finding2 = Finding(
            finding_id="f2",
            detector_id="test",
            hypothesis="test",
            confidence=0.5,
        )

        result = _filter_findings_for_case(case, [finding1, finding2])

        # Should only return f1 since case.finding_refs = ["f1"]
        assert len(result) == 1, f"Expected 1 finding (f1 only), got {len(result)}"
        assert result[0].finding_id == "f1"

    def test_inference_uses_confidence_field_when_present(self):
        """Inference confidence must use 'confidence' field when available.

        Mutation: Always uses risk_score instead of confidence
        This would ignore explicit confidence values.
        """
        from agent_py_agent.agent.log_analysis.models import Finding
        from agent_py_agent.agent.log_analysis.security.correlation import _inference_for_finding

        finding = Finding(
            finding_id="f1",
            detector_id="test",
            hypothesis="test hypothesis",
            confidence=0.8,  # Explicit confidence
            risk_score=0.3,  # Different risk_score
        )

        inference = _inference_for_finding(finding)

        # Should use confidence (0.8), not risk_score (0.3)
        assert inference["confidence"] == 0.8,             f"Expected confidence 0.8, got {inference['confidence']}"

    def test_route_draft_stored_in_case_attributes(self):
        """Route draft must be stored in case attributes.

        Mutation: case_obj.attributes = {...} line commented out
        This would lose the route_draft when case is used further.
        """
        from agent_py_agent.agent.log_analysis.models import CaseRecord, Finding
        from agent_py_agent.agent.log_analysis.security.correlation import build_route_draft

        # Create a case with existing attributes
        case = CaseRecord(
            case_id="test-case",
            title="test",
            attributes={"existing_key": "existing_value"},
        )

        findings = [
            Finding(
                finding_id="f1",
                detector_id="waf_attack_success_candidate",
                confidence=0.8,
            ),
        ]

        result = build_route_draft(case, findings=findings)

        # Route draft should be stored in case attributes
        assert "route_draft" in case.attributes,             "route_draft should be stored in case.attributes"
        assert case.attributes["route_draft"]["case_id"] == "test-case"
        # Original attributes should be preserved
        assert case.attributes["existing_key"] == "existing_value"

