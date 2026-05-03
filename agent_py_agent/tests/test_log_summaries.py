"""CaseSummary 摘要生成测试 - summaries.py 摘要生成、统计聚合、趋势分析。"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.log_analysis.agents.summaries import (
    CASE_FIELDS,
    EVIDENCE_REF_FIELDS,
    ROUTE_FIELDS,
    CaseSummary,
    _compact_list,
    _compact_mapping,
    _compact_text,
    _summarize_case_fields,
    _summarize_evidence,
    _summarize_route,
    case_summary_for_prompt,
    render_case_summary,
    summarize_case,
)


class TestCaseSummaryDataclass:
    """CaseSummary 数据类基本功能测试。"""

    def test_case_summary_empty(self):
        """验证空 CaseSummary 初始化。"""
        summary = CaseSummary(case={})
        assert summary.case == {}
        assert summary.evidence == []
        assert summary.route == {}

    def test_case_summary_with_data(self):
        """验证带数据的 CaseSummary。"""
        summary = CaseSummary(
            case={"case_id": "C-001", "title": "测试案例", "severity": "high"},
            evidence=[{"ref": "ev-1", "summary": "证据1"}],
            route={"entry_candidates": ["WAF"]},
        )
        assert summary.case["case_id"] == "C-001"
        assert len(summary.evidence) == 1
        assert summary.route["entry_candidates"] == ["WAF"]

    def test_case_summary_to_dict(self):
        """验证 to_dict 方法。"""
        summary = CaseSummary(
            case={"case_id": "C-002"},
            evidence=["ev-2"],
            route={"timeline": ["t1"]},
        )
        d = summary.to_dict()
        assert isinstance(d, dict)
        assert d["case"]["case_id"] == "C-002"
        assert d["evidence"][0] == "ev-2"


class TestCompactText:
    """_compact_text 文本压缩测试。"""

    def test_compact_text_none(self):
        """验证 None 输入返回 None。"""
        assert _compact_text(None) is None

    def test_compact_text_short_string(self):
        """验证短字符串直接返回。"""
        text = "hello"
        assert _compact_text(text) == text

    def test_compact_text_long_string_truncated(self):
        """验证超长字符串被截断。"""
        long_text = "a" * 300
        result = _compact_text(long_text, limit=220)
        assert len(result) <= 220
        assert result.endswith("...")

    def test_compact_text_numeric(self):
        """验证数值类型直接返回。"""
        assert _compact_text(42) == 42
        assert _compact_text(3.14) == 3.14

    def test_compact_text_bool(self):
        """验证布尔类型直接返回。"""
        assert _compact_text(True) is True
        assert _compact_text(False) is False


class TestCompactList:
    """_compact_list 列表压缩测试。"""

    def test_compact_list_none(self):
        """验证 None 输入返回空列表。"""
        assert _compact_list(None) == []

    def test_compact_list_empty(self):
        """验证空列表输入返回空列表。"""
        assert _compact_list([]) == []

    def test_compact_list_string_input(self):
        """验证字符串输入被当作单个元素。"""
        result = _compact_list("single")
        assert "single" in result

    def test_compact_list_normal_items(self):
        """验证普通列表项处理。"""
        items = ["item1", "item2", "item3"]
        result = _compact_list(items)
        assert len(result) == 3

    def test_compact_list_mapping_items(self):
        """验证映射项被递归压缩。"""
        items = [{"key": "value1"}, {"key": "value2"}]
        result = _compact_list(items)
        assert all(isinstance(item, dict) for item in result)

    def test_compact_list_respects_limit(self):
        """验证列表项数量限制。"""
        items = [f"item{i}" for i in range(20)]
        result = _compact_list(items, limit=8)
        assert len(result) == 8


class TestCompactMapping:
    """_compact_mapping 映射压缩测试。"""

    def test_compact_mapping_empty(self):
        """验证空映射返回空映射。"""
        assert _compact_mapping({}) == {}

    def test_compact_mapping_filters_empty_values(self):
        """验证空值被过滤。"""
        m = {"a": "val", "b": None, "c": "", "d": []}
        result = _compact_mapping(m)
        assert "a" in result
        assert "b" not in result
        assert "c" not in result
        assert "d" not in result

    def test_compact_mapping_nested_mapping(self):
        """验证嵌套映射被递归处理。"""
        m = {"outer": {"inner": "value"}}
        result = _compact_mapping(m)
        assert "outer" in result
        assert isinstance(result["outer"], dict)

    def test_compact_mapping_respects_limit(self):
        """验证映射键数量限制。"""
        m = {f"key{i}": f"val{i}" for i in range(15)}
        result = _compact_mapping(m, limit=8)
        assert len(result) == 8


class TestSummarizeCaseFields:
    """_summarize_case_fields 案例字段摘要测试。"""

    def test_summarize_case_fields_basic(self):
        """验证基本字段摘要。"""
        case = {
            "case_id": "C-001",
            "title": "测试案例",
            "severity": "high",
            "status": "open",
        }
        result = _summarize_case_fields(case)
        assert result["case_id"] == "C-001"
        assert result["title"] == "测试案例"
        assert result["severity"] == "high"

    def test_summarize_case_fields_with_id(self):
        """验证 case_id 和 id 字段等价。"""
        case = {"id": "C-002", "title": "测试"}
        result = _summarize_case_fields(case)
        assert result["case_id"] == "C-002"

    def test_summarize_case_fields_empty_values_filtered(self):
        """验证空值字段被过滤。"""
        case = {"case_id": "C-003", "title": None, "empty_field": ""}
        result = _summarize_case_fields(case)
        assert "title" not in result
        assert "empty_field" not in result

    def test_summarize_case_fields_entity_refs(self):
        """验证 entity_refs 字段处理。"""
        case = {"case_id": "C-004", "entity_refs": [{"id": "e1"}, {"id": "e2"}]}
        result = _summarize_case_fields(case)
        assert "entity_refs" in result


class TestSummarizeEvidence:
    """_summarize_evidence 证据摘要测试。"""

    def test_summarize_evidence_empty(self):
        """验证空证据列表。"""
        result = _summarize_evidence([])
        assert result == []

    def test_summarize_evidence_none(self):
        """验证 None 输入。"""
        result = _summarize_evidence(None)
        assert result == []

    def test_summarize_evidence_mapping_items(self):
        """验证映射类型证据项。"""
        evidence = [{"evidence_id": "ev-001", "summary": "测试证据"}]
        result = _summarize_evidence(evidence)
        assert len(result) == 1
        assert result[0].get("evidence_id") == "ev-001"

    def test_summarize_evidence_string_items(self):
        """验证字符串类型证据项。"""
        evidence = ["evidence_ref_1", "evidence_ref_2"]
        result = _summarize_evidence(evidence)
        assert result == ["evidence_ref_1", "evidence_ref_2"]

    def test_summarize_evidence_deduplication(self):
        """验证重复证据去重。"""
        evidence = [
            {"evidence_id": "ev-001", "summary": "same"},
            {"evidence_id": "ev-001", "summary": "same"},
        ]
        result = _summarize_evidence(evidence)
        assert len(result) == 1

    def test_summarize_evidence_limit(self):
        """验证证据数量限制。"""
        evidence = [{"evidence_id": f"ev-{i:03d}"} for i in range(20)]
        result = _summarize_evidence(evidence)
        assert len(result) <= 12


class TestSummarizeRoute:
    """_summarize_route 路由摘要测试。"""

    def test_summarize_route_empty(self):
        """验证空路由返回空字典。"""
        result = _summarize_route(None)
        assert result == {}

    def test_summarize_route_with_fields(self):
        """验证路由字段提取。"""
        route = {
            "entry_candidates": ["WAF", "EDR"],
            "timeline": ["alert", "investigation"],
            "impacted_entities": ["server-1"],
            "route_confidence": 0.85,
        }
        result = _summarize_route(route)
        assert result["entry_candidates"] == ["WAF", "EDR"]
        assert result["route_confidence"] == 0.85

    def test_summarize_route_empty_fields_filtered(self):
        """验证空字段被过滤。"""
        route = {"entry_candidates": [], "timeline": None, "gaps": ""}
        result = _summarize_route(route)
        assert "entry_candidates" not in result
        assert "timeline" not in result


class TestSummarizeCase:
    """summarize_case 主函数测试。"""

    def test_summarize_case_complete(self):
        """验证完整案例摘要。"""
        case = {
            "case_id": "C-001",
            "title": "安全事件调查",
            "severity": "high",
            "evidence_refs": [{"evidence_id": "ev-001"}],
            "route_summary": {"entry_candidates": ["WAF"]},
        }
        result = summarize_case(case)
        assert isinstance(result, CaseSummary)
        assert result.case["case_id"] == "C-001"
        assert len(result.evidence) == 1
        assert result.route["entry_candidates"] == ["WAF"]

    def test_summarize_case_minimal(self):
        """验证最小化案例输入。"""
        case = {"case_id": "C-002"}
        result = summarize_case(case)
        assert isinstance(result, CaseSummary)
        assert result.case["case_id"] == "C-002"


class TestRenderFunctions:
    """render_case_summary 和 case_summary_for_prompt 测试。"""

    def test_render_case_summary(self):
        """验证 JSON 渲染输出。"""
        case = {"case_id": "C-001", "title": "测试"}
        summary = summarize_case(case)
        result = render_case_summary(summary)
        assert isinstance(result, str)
        assert "C-001" in result

    def test_render_case_summary_mapping_input(self):
        """验证接受映射类型输入。"""
        mapping = {"case": {"case_id": "C-002"}}
        result = render_case_summary(mapping)
        assert "C-002" in result

    def test_case_summary_for_prompt(self):
        """验证提示词格式化输出。"""
        case = {"case_id": "C-003", "title": "测试"}
        result = case_summary_for_prompt(case)
        assert isinstance(result, str)
        assert "C-003" in result
