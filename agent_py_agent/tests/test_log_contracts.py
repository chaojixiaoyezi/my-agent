from __future__ import annotations

"""测试日志分析契约模块 (contracts.py)

测试重点：
- 证据引用规范化
- AnalystInput/AnalystReport 验证
- ReviewerDecision 评审逻辑
"""
from dataclasses import dataclass
from typing import Any

import pytest

from agent_py_agent.agent.log_analysis.agents.contracts import (
    DEFAULT_ACCEPTANCE_CHECKS,
    DEFAULT_ANALYST_TOOLS,
    AnalystInput,
    AnalystReport,
    ContractValidationError,
    ReviewerDecision,
    ReviewerInput,
    normalize_evidence_refs,
    require_evidence_refs,
    review_analyst_report,
    validate_analyst_input,
    validate_analyst_report,
)

# ============================================================
# 辅助函数
# ============================================================

@dataclass(frozen=True)
class MakeAnalystInputParams:
    """Parameter bundle for make_analyst_input."""
    case_id: str = "case-001"
    case_summary: str = "Test case summary"
    evidence_refs: list[str] | None = None
    route_summary: dict[str, Any] | None = None
    entity_refs: list[str] | None = None
    finding_refs: list[str] | None = None
    available_tools: list[str] | None = None
    budget: dict[str, int] | None = None


def make_analyst_input(params: MakeAnalystInputParams) -> AnalystInput:
    """创建测试用 AnalystInput 对象"""
    return AnalystInput(
        case_id=params.case_id,
        case_summary=params.case_summary,
        evidence_refs=params.evidence_refs or ["ev-001", "ev-002"],
        route_summary=params.route_summary or {},
        entity_refs=params.entity_refs or [],
        finding_refs=params.finding_refs or [],
        available_tools=params.available_tools or list(DEFAULT_ANALYST_TOOLS),
        budget=params.budget or {},
    )


@dataclass(frozen=True)
class MakeAnalystReportParams:
    """Parameter bundle for make_analyst_report."""
    case_id: str = "case-001"
    summary: str = "Test report summary"
    evidence_refs: list[str] | None = None
    facts: list[str] | None = None
    inferences: list[str] | None = None
    gaps: list[str] | None = None
    next_actions: list[str] | None = None
    confidence: str = "medium"
    status: str = "AWAITING_REVIEW"


def make_analyst_report(params: MakeAnalystReportParams) -> AnalystReport:
    """创建测试用 AnalystReport 对象"""
    return AnalystReport(
        case_id=params.case_id,
        summary=params.summary,
        evidence_refs=params.evidence_refs or ["ev-001"],
        facts=params.facts or [],
        inferences=params.inferences or [],
        gaps=params.gaps or [],
        next_actions=params.next_actions or [],
        confidence=params.confidence,
        status=params.status,
    )


# ============================================================
# 测试用例：normalize_evidence_refs
# ============================================================

class TestNormalizeEvidenceRefs:
    """测试证据引用规范化函数"""

    def test_normalize_string_list(self):
        """测试字符串列表规范化"""
        refs = normalize_evidence_refs(["ev-001", "ev-002"])
        assert len(refs) == 2
        assert "ev-001" in refs
        assert "ev-002" in refs

    def test_normalize_single_string(self):
        """测试单个字符串规范化"""
        refs = normalize_evidence_refs("ev-001")
        assert len(refs) == 1
        assert "ev-001" in refs

    def test_normalize_none(self):
        """测试 None 输入返回空列表"""
        refs = normalize_evidence_refs(None)
        assert refs == []

    def test_normalize_dict_list(self):
        """测试字典列表规范化"""
        refs = normalize_evidence_refs([
            {"evidence_id": "ev-001"},
            {"evidence_ref": "ev-002"},
        ])
        assert len(refs) == 2
        assert "ev-001" in refs
        assert "ev-002" in refs

    def test_normalize_with_metadata(self):
        """测试带 metadata 的字典"""
        refs = normalize_evidence_refs([
            {"metadata": {"evidence_id": "ev-001"}},
        ])
        assert "ev-001" in refs

    def test_normalize_deduplicates(self):
        """测试去重"""
        refs = normalize_evidence_refs(["ev-001", "ev-001", "ev-002"])
        assert len(refs) == 2

    def test_normalize_with_limit(self):
        """测试数量限制"""
        refs = normalize_evidence_refs([f"ev-{i:03d}" for i in range(100)], limit=50)
        assert len(refs) == 50


# ============================================================
# 测试用例：require_evidence_refs
# ============================================================

class TestRequireEvidenceRefs:
    """测试 require_evidence_refs 函数"""

    def test_require_non_empty_succeeds(self):
        """测试非空引用通过"""
        refs = require_evidence_refs(["ev-001"])
        assert refs == ["ev-001"]

    def test_require_empty_raises(self):
        """测试空列表抛出异常"""
        with pytest.raises(ContractValidationError):
            require_evidence_refs([])

    def test_require_none_raises(self):
        """测试 None 抛出异常"""
        with pytest.raises(ContractValidationError):
            require_evidence_refs(None)

    def test_require_with_custom_field_name(self):
        """测试自定义字段名的错误消息"""
        with pytest.raises(ContractValidationError) as exc_info:
            require_evidence_refs([], field_name="custom_refs")
        assert "custom_refs" in str(exc_info.value)


# ============================================================
# 测试用例：AnalystInput 验证
# ============================================================

class TestAnalystInputValidation:
    """测试 AnalystInput 验证"""

    def test_valid_analyst_input(self):
        """测试有效输入通过验证"""
        input_obj = make_analyst_input(MakeAnalystInputParams())
        input_obj.validate()  # 不抛出异常

    def test_empty_case_id_raises(self):
        """测试空 case_id 抛出异常"""
        input_obj = make_analyst_input(MakeAnalystInputParams(case_id=""))
        with pytest.raises(ContractValidationError) as exc_info:
            input_obj.validate()
        assert "case_id" in str(exc_info.value)

    def test_empty_case_summary_raises(self):
        """测试空 case_summary 抛出异常"""
        input_obj = make_analyst_input(MakeAnalystInputParams(case_summary=""))
        with pytest.raises(ContractValidationError):
            input_obj.validate()

    def test_to_dict_calls_validate(self):
        """测试 to_dict 调用 validate"""
        input_obj = make_analyst_input(MakeAnalystInputParams())
        result = input_obj.to_dict()
        assert "case_id" in result
        assert result["case_id"] == "case-001"

    def test_from_mapping_basic(self):
        """测试 from_mapping 基本功能"""
        payload = {
            "case_id": "case-002",
            "case_summary": "Summary from mapping",
            "evidence_refs": ["ev-003"],
        }
        input_obj = AnalystInput.from_mapping(payload)
        assert input_obj.case_id == "case-002"
        assert input_obj.case_summary == "Summary from mapping"
        assert "ev-003" in input_obj.evidence_refs

    def test_from_mapping_with_aliases(self):
        """测试 from_mapping 支持别名"""
        payload = {
            "id": "case-alias-001",  # case_id 别名
            "summary": "Summary via alias",  # case_summary 别名
            "evidence": ["ev-004"],  # evidence_refs 别名
        }
        input_obj = AnalystInput.from_mapping(payload)
        assert input_obj.case_id == "case-alias-001"
        assert input_obj.case_summary == "Summary via alias"


# ============================================================
# 测试用例：AnalystReport 验证
# ============================================================

class TestAnalystReportValidation:
    """测试 AnalystReport 验证"""

    def test_valid_report_with_facts(self):
        """测试带事实的有效报告"""
        report = make_analyst_report(MakeAnalystReportParams(facts=["fact 1", "fact 2"]))
        report.validate()  # 不抛出异常

    def test_valid_report_with_inferences(self):
        """测试带推论的有效报告"""
        report = make_analyst_report(MakeAnalystReportParams(inferences=["inference 1"]))
        report.validate()  # 不抛出异常

    def test_valid_report_with_gaps(self):
        """测试带 gaps 的有效报告"""
        report = make_analyst_report(MakeAnalystReportParams(gaps=["gap 1"]))
        report.validate()  # 不抛出异常

    def test_empty_facts_inferences_gaps_raises(self):
        """测试 facts/inferences/gaps 全为空时抛出异常"""
        # 明确传入空列表
        report = make_analyst_report(MakeAnalystReportParams(facts=[], inferences=[], gaps=[]))
        with pytest.raises(ContractValidationError) as exc_info:
            report.validate()
        assert "facts" in str(exc_info.value).lower() or "gaps" in str(exc_info.value).lower()

    def test_from_mapping_with_hypotheses_alias(self):
        """测试 from_mapping 支持 hypotheses 作为 inferences 别名"""
        payload = {
            "case_id": "case-003",
            "summary": "Report via hypothesis alias",
            "evidence_refs": ["ev-005"],
            "hypotheses": ["hypothesis 1"],  # inferences 的别名
            "facts": ["fact 1"],
        }
        report = AnalystReport.from_mapping(payload)
        assert "hypothesis 1" in report.inferences

    def test_report_to_dict(self):
        """测试报告转换为字典"""
        report = make_analyst_report(MakeAnalystReportParams(facts=["test fact"]))
        result = report.to_dict()
        assert "case_id" in result
        assert result["confidence"] == "medium"


# ============================================================
# 测试用例：ReviewerInput 验证
# ============================================================

class TestReviewerInputValidation:
    """测试 ReviewerInput 验证"""

    def test_valid_reviewer_input(self):
        """测试有效的 ReviewerInput"""
        analyst_report = make_analyst_report(MakeAnalystReportParams(facts=["fact 1"]))
        input_obj = ReviewerInput(
            case_id="case-001",
            analyst_report=analyst_report,
            evidence_refs=["ev-001"],
        )
        input_obj.validate()  # 不抛出异常

    def test_reviewer_input_empty_case_id_raises(self):
        """测试空 case_id 抛出异常"""
        analyst_report = make_analyst_report(MakeAnalystReportParams())
        input_obj = ReviewerInput(
            case_id="",
            analyst_report=analyst_report,
        )
        with pytest.raises(ContractValidationError):
            input_obj.validate()

    def test_reviewer_input_to_dict(self):
        """测试 ReviewerInput 转换为字典"""
        analyst_report = make_analyst_report(MakeAnalystReportParams(facts=["fact 1"]))
        input_obj = ReviewerInput(
            case_id="case-001",
            analyst_report=analyst_report,
            evidence_refs=["ev-001"],
        )
        result = input_obj.to_dict()
        assert "case_id" in result
        assert "analyst_report" in result
        assert "evidence_refs" in result


# ============================================================
# 测试用例：review_analyst_report
# ============================================================

class TestReviewAnalystReport:
    """测试 review_analyst_report 函数"""

    def test_review_approves_valid_report_with_known_evidence(self):
        """测试有已知证据的有效报告被批准"""
        report = make_analyst_report(
            MakeAnalystReportParams(
                facts=["fact 1"],
                evidence_refs=["ev-001"],
            )
        )
        decision = review_analyst_report(report, known_evidence_refs=["ev-001"])
        assert decision.approved is True
        assert decision.decision == "APPROVE"

    def test_review_rejects_unknown_evidence(self):
        """测试未知证据被拒绝"""
        report = make_analyst_report(
            MakeAnalystReportParams(
                facts=["fact 1"],
                evidence_refs=["ev-unknown"],
            )
        )
        decision = review_analyst_report(report, known_evidence_refs=["ev-known"])
        assert decision.approved is False
        assert decision.decision == "REJECT"

    def test_review_rejects_invalid_payload(self):
        """测试无效 payload 被拒绝"""
        # 缺少必要字段的 payload
        payload = {
            "case_id": "",  # 空 case_id
            "summary": "Test",
            "evidence_refs": ["ev-001"],
            "facts": ["fact 1"],
        }
        decision = review_analyst_report(payload)
        assert decision.approved is False

    def test_review_with_mapping_payload(self):
        """测试使用字典 payload"""
        payload = {
            "case_id": "case-004",
            "summary": "Test report",
            "evidence_refs": ["ev-001"],
            "facts": ["fact 1"],
        }
        decision = review_analyst_report(payload, known_evidence_refs=["ev-001"])
        assert decision.approved is True

    def test_review_includes_gaps_in_decision(self):
        """测试决策包含 gaps 信息"""
        report = make_analyst_report(
            MakeAnalystReportParams(
                facts=[],
                evidence_refs=["ev-001"],
                gaps=["gap1", "gap2"],
            )
        )
        decision = review_analyst_report(report, known_evidence_refs=["ev-001"])
        assert decision.approved is False
        # 有 gaps 时会返回 NEEDS_MORE_EVIDENCE 因为没有 facts

    def test_review_multiple_evidence_refs(self):
        """测试多个证据引用"""
        report = make_analyst_report(
            MakeAnalystReportParams(
                facts=["fact 1"],
                evidence_refs=["ev-001", "ev-002", "ev-003"],
            )
        )
        decision = review_analyst_report(report, known_evidence_refs=["ev-001", "ev-002"])
        assert decision.approved is False
        assert decision.decision == "REJECT"

    def test_review_partial_known_evidence(self):
        """测试部分已知证据"""
        report = make_analyst_report(
            MakeAnalystReportParams(
                facts=["fact 1"],
                evidence_refs=["ev-001", "ev-002"],
            )
        )
        decision = review_analyst_report(report, known_evidence_refs=["ev-001"])
        assert decision.approved is False
        assert decision.decision == "REJECT"


# ============================================================
# 测试用例：便利函数
# ============================================================

class TestConvenienceFunctions:
    """测试便利验证函数"""

    def test_validate_analyst_input_with_object(self):
        """测试验证 AnalystInput 对象"""
        input_obj = make_analyst_input(MakeAnalystInputParams())
        result = validate_analyst_input(input_obj)
        assert isinstance(result, AnalystInput)

    def test_validate_analyst_input_with_mapping(self):
        """测试验证字典 payload"""
        payload = {
            "case_id": "case-005",
            "case_summary": "Summary",
            "evidence_refs": ["ev-001"],
        }
        result = validate_analyst_input(payload)
        assert isinstance(result, AnalystInput)
        assert result.case_id == "case-005"

    def test_validate_analyst_report_with_object(self):
        """测试验证 AnalystReport 对象"""
        report = make_analyst_report(MakeAnalystReportParams(facts=["fact 1"]))
        result = validate_analyst_report(report)
        assert isinstance(result, AnalystReport)

    def test_validate_analyst_report_with_mapping(self):
        """测试验证字典 payload"""
        payload = {
            "case_id": "case-006",
            "summary": "Summary",
            "evidence_refs": ["ev-001"],
            "facts": ["fact 1"],
        }
        result = validate_analyst_report(payload)
        assert isinstance(result, AnalystReport)


# ============================================================
# 测试用例：默认值和常量
# ============================================================

class TestDefaultsAndConstants:
    """测试默认值和常量"""

    def test_default_analyst_tools(self):
        """测试默认分析工具列表"""
        assert "security_query" in DEFAULT_ANALYST_TOOLS
        assert "evidence_read" in DEFAULT_ANALYST_TOOLS

    def test_default_acceptance_checks(self):
        """测试默认验收检查列表"""
        assert len(DEFAULT_ACCEPTANCE_CHECKS) > 0
        assert any("evidence_ref" in check for check in DEFAULT_ACCEPTANCE_CHECKS)

    def test_default_case_summary_truncated(self):
        """测试过长 summary 被截断"""
        long_summary = "x" * 1000
        payload = {"case_id": "case-007", "case_summary": long_summary, "evidence_refs": ["ev-001"]}
        result = AnalystInput.from_mapping(payload)
        assert len(result.case_summary) <= 500