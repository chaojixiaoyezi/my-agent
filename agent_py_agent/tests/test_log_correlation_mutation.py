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
