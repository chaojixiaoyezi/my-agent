# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Correlation helpers that turn a case into a route draft."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..models import CaseRecord, EvidenceRef, Finding, QueryPlan
from .attack_chain import build_attack_chain, lateral_movement_signs


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 RouteDraft 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 RouteDraft 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class RouteDraft:
    case_id: str = ""
    entry_candidates: list[dict[str, Any]] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    impacted_entities: dict[str, list[str]] = field(default_factory=dict)
    lateral_signs: list[dict[str, Any]] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)
    inferences: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    next_queries: list[Any] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _RouteGapContext 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _RouteGapContext 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class _RouteGapContext:
    case_obj: CaseRecord
    findings: Sequence[Finding]
    impacted_entities: Mapping[str, Sequence[Any]]
    entry_candidates: Sequence[Mapping[str, Any]]
    gaps: list[str]
    next_queries: list[Any]


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 build_route_draft 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 build route draft 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def build_route_draft(
    case: CaseRecord | Mapping[str, Any],
    *,
    findings: Sequence[Finding | Mapping[str, Any]] | None = None,
) -> RouteDraft:
    case_obj = case if isinstance(case, CaseRecord) else CaseRecord.from_dict(case)
    finding_objs = _extract_findings(case_obj, findings)
    impacted_entities = _merge_entities([case_obj.entities, *(finding.entities for finding in finding_objs)])
    facts = [_fact_for_finding(finding) for finding in finding_objs]
    inferences = [_inference_for_finding(finding) for finding in finding_objs]
    entry_candidates = _entry_candidates(finding_objs)
    gaps = _unique([*case_obj.gaps, *(gap for finding in finding_objs for gap in finding.gaps)])
    next_queries = _unique_values([*case_obj.next_queries, *(query for finding in finding_objs for query in finding.next_queries)])
    _add_route_gaps(_RouteGapContext(case_obj, finding_objs, impacted_entities, entry_candidates, gaps, next_queries))

    chain = build_attack_chain(finding_objs)
    route = RouteDraft(
        case_id=case_obj.case_id,
        entry_candidates=entry_candidates,
        timeline=[step.to_dict() for step in chain],
        impacted_entities=impacted_entities,
        lateral_signs=lateral_movement_signs(finding_objs),
        facts=facts,
        inferences=inferences,
        gaps=_unique(gaps),
        next_queries=_unique_values(next_queries),
        evidence_refs=_unique([*_ref_ids(case_obj.evidence_refs), *(ref for finding in finding_objs for ref in _ref_ids(finding.evidence_refs))]),
    )
    attributes = case_obj.attributes if isinstance(case_obj.attributes, Mapping) else {}
    case_obj.attributes = {**attributes, "route_draft": route.to_dict()}
    return route


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _add_route_gaps 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 add route gaps 相关记录，集中处理目标路径、格式化和状态更新。
def _add_route_gaps(context: _RouteGapContext) -> None:
    if not context.entry_candidates:
        context.gaps.append("Entry point is not identified; rank candidate source IP, VPN account, WAF URI, and first host touch.")
        context.next_queries.append(_entry_candidate_query(context.case_obj, context.impacted_entities))
    detector_ids = {finding.detector_id for finding in context.findings}
    if "waf_attack_success_candidate" in detector_ids and "web_to_process_anomaly" not in detector_ids:
        context.gaps.append("WAF path lacks linked EDR process evidence for the victim asset.")
    if detector_ids & {"vpn_new_geo_login", "bruteforce_then_success"} and not _has_identity_asset_link(context.findings):
        context.gaps.append("Identity path lacks linked host logon or asset access evidence.")


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _entry_candidate_query 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 entry candidate query 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _entry_candidate_query(case_obj: CaseRecord, impacted_entities: Mapping[str, Sequence[Any]]) -> dict[str, Any]:
    return QueryPlan(
        purpose="Build entry-candidate query across WAF, VPN, SSO, exposed services, and first host activity",
        source_products=["waf", "vpn", "sso", "edr"],
        start_time=case_obj.created_at,
        end_time=case_obj.updated_at,
        filters=_first_entity_filters(impacted_entities),
        limit=100,
        evidence_needed=["entry_candidate", "first_host_touch", "identity_session"],
    ).to_dict()


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _has_identity_asset_link 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 has identity asset link 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _has_identity_asset_link(findings: Sequence[Finding]) -> bool:
    return any("host" in finding.entities or "victim_ip" in finding.entities for finding in findings)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 draft_route 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 draft route 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def draft_route(case: CaseRecord | Mapping[str, Any], *, findings: Sequence[Finding | Mapping[str, Any]] | None = None) -> RouteDraft:
    return build_route_draft(case, findings=findings)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 route_from_case 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 route from case 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def route_from_case(case: CaseRecord | Mapping[str, Any]) -> RouteDraft:
    return build_route_draft(case)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _extract_findings 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 extract findings 涉及的字段，让后续匹配和存储使用同一形态。
def _extract_findings(case: CaseRecord, findings: Sequence[Finding | Mapping[str, Any]] | None) -> list[Finding]:
    if findings is not None:
        return _filter_findings_for_case(case, [item if isinstance(item, Finding) else Finding.from_dict(item) for item in findings])
    attributes = case.attributes if isinstance(case.attributes, Mapping) else {}
    return _filter_findings_for_case(
        case,
        [Finding.from_dict(item) for item in attributes.get("finding_summaries", []) if isinstance(item, Mapping)],
    )


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _filter_findings_for_case 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 filter findings for case 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
def _filter_findings_for_case(case: CaseRecord, findings: Sequence[Finding]) -> list[Finding]:
    refs = {str(ref) for ref in case.finding_refs if str(ref or "").strip()}
    if not refs:
        return list(findings)
    return [finding for finding in findings if finding.finding_id in refs]


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _fact_for_finding 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 fact for finding 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _fact_for_finding(finding: Finding) -> dict[str, Any]:
    return {
        "kind": "detector_finding",
        "detector_id": finding.detector_id,
        "time_window": list(finding.window),
        "statement": f"{finding.detector_id} produced a soft finding with evidence references.",
        "entities": finding.entities,
        "evidence_refs": _ref_ids(finding.evidence_refs),
    }


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _inference_for_finding 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 inference for finding 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _inference_for_finding(finding: Finding) -> dict[str, Any]:
    return {
        "kind": "hypothesis",
        "detector_id": finding.detector_id,
        "hypothesis": finding.hypothesis,
        "confidence": finding.confidence if finding.confidence is not None else finding.risk_score,
        "evidence_refs": _ref_ids(finding.evidence_refs),
    }


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _entry_candidates 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 entry candidates 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _entry_candidates(findings: Sequence[Finding]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for finding in findings:
        kind = _detector_kind(finding.detector_id)
        if not kind:
            continue
        candidates.append({
            "kind": kind,
            "detector_id": finding.detector_id,
            "confidence": finding.confidence if finding.confidence is not None else finding.risk_score,
            "entities": finding.entities,
            "evidence_refs": _ref_ids(finding.evidence_refs),
            "basis": "inference",
        })
    return sorted(candidates, key=lambda item: float(item.get("confidence", 0.0)), reverse=True)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _detector_kind 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 基于规则或事件字段计算 detector kind 的判定结果，避免把推测当作事实写入。
def _detector_kind(detector_id: str) -> str:
    """Map detector ID to candidate kind string."""
    if detector_id == "waf_attack_success_candidate":
        return "web_exploit_candidate"
    if detector_id == "web_to_process_anomaly":
        return "web_post_exploit_execution_candidate"
    if detector_id == "vpn_new_geo_login":
        return "vpn_credential_abuse_candidate"
    if detector_id == "bruteforce_then_success":
        return "bruteforce_credential_abuse_candidate"
    return ""


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _merge_entities 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 merge entities 涉及的字段，让后续匹配和存储使用同一形态。
def _merge_entities(entity_sets: Sequence[Mapping[str, Sequence[Any]]]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for entities in entity_sets:
        for key, values in entities.items():
            merged.setdefault(str(key), [])
            merged[str(key)].extend(str(value) for value in values)
    return {key: clean for key, values in sorted(merged.items()) if (clean := _unique(values))}


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _ref_ids 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 ref ids 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _ref_ids(refs: Sequence[Any]) -> list[str]:
    result: list[str] = []
    for ref in refs:
        result.append(_ref_id(ref))
    return _unique(result)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _ref_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 ref id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _ref_id(ref: Any) -> str:
    if isinstance(ref, EvidenceRef):
        return ref.evidence_id
    if isinstance(ref, Mapping):
        return str(ref.get("evidence_id") or ref.get("raw_ref") or ref)
    return str(ref)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _first_entity_filters 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 first entity filters 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _first_entity_filters(entities: Mapping[str, Sequence[Any]]) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    for key in ("victim_ip", "host", "user", "attacker_ip", "src_ip"):
        values = entities.get(key)
        if values:
            filters[key] = str(values[0])
    return filters


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _unique_values 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 unique values 涉及的字段，让后续匹配和存储使用同一形态。
def _unique_values(values: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        item: Any = value.to_dict() if isinstance(value, QueryPlan) else dict(value) if isinstance(value, Mapping) else str(value or "").strip()
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        if not item or marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _unique 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 unique 涉及的字段，让后续匹配和存储使用同一形态。
def _unique(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


__all__ = ["RouteDraft", "build_route_draft", "draft_route", "route_from_case"]
