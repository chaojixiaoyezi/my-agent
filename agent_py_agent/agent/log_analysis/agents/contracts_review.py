# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Reviewer gate for analyst report contracts."""

from collections.abc import Mapping
from typing import Any

from .contracts import (
    AnalystReport,
    ContractValidationError,
    ReviewerDecision,
    _compact_string,
    _get,
    normalize_evidence_refs,
    validate_analyst_report,
)


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 review_analyst_report 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 review analyst report 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def review_analyst_report(
    payload: AnalystReport | Mapping[str, Any],
    *,
    known_evidence_refs: list[str] | None = None,
) -> ReviewerDecision:
    """Review an analyst report against evidence boundaries."""

    try:
        report = validate_analyst_report(payload)
    except ContractValidationError as exc:
        return _validation_reject(payload, exc)

    unknown_refs = _unknown_report_refs(report, known_evidence_refs)
    if unknown_refs:
        return _unknown_refs_reject(report, unknown_refs)
    if not report.facts:
        return _missing_facts_decision(report)
    return _approved_decision(report)


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _validation_reject 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 validation reject 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _validation_reject(payload: AnalystReport | Mapping[str, Any], exc: ContractValidationError) -> ReviewerDecision:
    case_id = _compact_string(_get(payload, "case_id") or _get(payload, "id"), limit=120)
    return ReviewerDecision(
        case_id=case_id,
        approved=False,
        decision="REJECT",
        reasons=[str(exc)],
        next_actions=["request_evidence_backed_report"],
    )


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _unknown_report_refs 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 unknown report refs 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def _unknown_report_refs(report: AnalystReport, known_evidence_refs: list[str] | None) -> list[str]:
    known_refs = set(normalize_evidence_refs(known_evidence_refs))
    return [ref for ref in report.evidence_refs if known_refs and ref not in known_refs]


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _unknown_refs_reject 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 unknown refs reject 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def _unknown_refs_reject(report: AnalystReport, unknown_refs: list[str]) -> ReviewerDecision:
    return ReviewerDecision(
        case_id=report.case_id,
        approved=False,
        decision="REJECT",
        reasons=["report cites evidence_refs outside the reviewer evidence boundary"],
        evidence_refs=list(report.evidence_refs),
        gaps=[f"unknown evidence_ref: {ref}" for ref in unknown_refs],
        next_actions=["rerun analyst with valid evidence refs"],
    )


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _missing_facts_decision 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 missing facts decision 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def _missing_facts_decision(report: AnalystReport) -> ReviewerDecision:
    return ReviewerDecision(
        case_id=report.case_id,
        approved=False,
        decision="NEEDS_MORE_EVIDENCE",
        reasons=["report has evidence_refs but no evidence-backed facts"],
        evidence_refs=list(report.evidence_refs),
        gaps=list(report.gaps) or ["facts missing"],
        next_actions=list(report.next_actions) or ["add facts tied to evidence_refs"],
    )


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _approved_decision 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 approved decision 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def _approved_decision(report: AnalystReport) -> ReviewerDecision:
    return ReviewerDecision(
        case_id=report.case_id,
        approved=True,
        decision="APPROVE",
        reasons=["evidence_refs present and facts/inferences/gaps are separated"],
        evidence_refs=list(report.evidence_refs),
        gaps=list(report.gaps),
        next_actions=list(report.next_actions),
    )
