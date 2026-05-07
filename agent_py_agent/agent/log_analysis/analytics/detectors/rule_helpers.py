# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""shared helper functions for rule evaluation and finding construction.

新手说明:
这个文件放的是检测器评估中使用的共享辅助函数。
它们被所有检测器共用，包括 Finding 构造、证据处理、查询构建等。
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from agent_py_agent.agent.log_analysis.analytics.security_rules import get_rule

from ...models import EvidenceRef, QueryPlan, utc_now_iso
from .classifiers import (
    _entities_from_events,
    _gap_details,
    _normalize_entities,
    _unique_json_values,
    _unique_texts,
)
from .field_access import (
    _canonical_time,
    _clamp_float,
    _event_time,
    _field,
    _text,
    _window_for_events,
)
from .field_extractors import (
    _destination_ip,
    _domain,
    _host,
    _source_ip,
    _source_product,
    _user,
    _victim_ip,
)


# LLM: detector finding fields stay in MakeFindingParams so rule helpers remain extensible.
# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 MakeFindingParams 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 MakeFindingParams 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class MakeFindingParams:
    """Params bundle for _make_finding."""

    detector_id: str
    evidence_events: Sequence[Any]
    hypothesis: str = ""
    confidence: float = 0.0
    gaps: Sequence[str] = ()
    next_queries: Sequence[Any] = ()
    features: dict[str, Any] | None = None
    severity_hint: str | None = None
    extra_entities: dict[str, list[Any]] | None = None


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _FindingIdPayloadParams 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _FindingIdPayloadParams 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _FindingIdPayloadParams:
    finding: MakeFindingParams
    window: list[str]
    entities: dict[str, list[str]]
    evidence_refs: Sequence[EvidenceRef]


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _make_finding 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 make finding 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _make_finding(
    detector_id: str,
    evidence_events: Sequence[Any],
    *,
    params: MakeFindingParams,
) -> Any:
    """Build a Finding object from detector output.

    Args:
        detector_id: Detector identifier.
        evidence_events: Sequence of evidence events.
        params: Params bundle containing all finding construction args."""
    rule = get_rule(params.detector_id)
    entities = _finding_entities(params)
    evidence_refs = [_evidence_ref(event) for event in params.evidence_events]
    window = list(_window_for_events(params.evidence_events))
    from ...models import Finding

    finding = Finding(
        finding_id=_stable_id(
            "finding",
            _finding_id_payload(_FindingIdPayloadParams(params, window, entities, evidence_refs)),
        ),
        detector_id=params.detector_id,
        detector_kind=rule.detector_kind,
        window=window,
        severity_hint=params.severity_hint or rule.severity_hint,
        risk_score=_clamp_float(params.confidence),
        entities=entities,
        features=dict(params.features or {}),
        evidence_refs=evidence_refs,
        status="OPEN",
        hypothesis=params.hypothesis,
        confidence=_clamp_float(params.confidence),
        gaps=_unique_texts(params.gaps),
        next_queries=_unique_json_values(params.next_queries),
        rule_version=rule.version,
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
        attributes={"mode": rule.mode, "gap_details": _gap_details(params.gaps, window, entities, params.evidence_events)},
    )
    finding.mode = rule.mode
    return finding


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _finding_entities 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 finding entities 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _finding_entities(params: MakeFindingParams) -> dict[str, list[str]]:
    entities = _entities_from_events(params.evidence_events)
    for key, values in (params.extra_entities or {}).items():
        entities.setdefault(key, [])
        entities[key].extend(_text(value) for value in values if _text(value))
    return _normalize_entities(entities)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _finding_id_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 finding id payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _finding_id_payload(
    params: _FindingIdPayloadParams,
) -> dict[str, Any]:
    finding = params.finding
    return {
        "detector_id": finding.detector_id,
        "window": params.window,
        "entities": params.entities,
        "evidence_refs": [ref.evidence_id for ref in params.evidence_refs],
        "hypothesis": finding.hypothesis,
    }


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _evidence_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 evidence id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _evidence_id(event: dict[str, Any]) -> str:
    """Return a stable evidence identifier for *event*."""
    for field_name in ("raw_ref", "evidence_ref", "event_id", "security_event_id", "alert_id", "id"):
        value = _text(_field(event, field_name))
        if value:
            return value
    return _stable_id("event", event)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _evidence_ref 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 evidence ref 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _evidence_ref(event: dict[str, Any]) -> EvidenceRef:
    """Build an EvidenceRef from *event*."""
    evidence_id = _evidence_id(event)
    raw_ref = _text(_field(event, "raw_ref"))
    source_id = _text(_field(event, "source_id"))
    when = _canonical_time(_event_time(event))
    return EvidenceRef(
        evidence_id=evidence_id,
        kind="event",
        source_id=source_id,
        raw_ref=raw_ref,
        time_range=[when, when] if when else [],
        summary=f"event evidence {evidence_id}",
        metadata={"source_product": _source_product(event), "event_id": _text(_field(event, "event_id", "alert_id"))},
    )


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _query 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 query 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _query(prefix: str, event: dict[str, Any]) -> dict[str, Any]:
    """Build a follow-up QueryPlan dict for *event*."""
    when = _event_time(event)
    filters = {
        label: value
        for label, value in (
            ("src_ip", _source_ip(event)),
            ("victim_ip", _victim_ip(event)),
            ("host", _host(event)),
            ("user", _user(event)),
            ("domain", _domain(event)),
        )
        if value
    }
    source_products = _query_source_products(prefix, event)
    plan = QueryPlan(
        purpose=prefix,
        source_products=source_products,
        start_time=_canonical_time(when - timedelta(minutes=15)) if when else "",
        end_time=_canonical_time(when + timedelta(minutes=30)) if when else "",
        filters=filters,
        limit=100,
        evidence_needed=_query_evidence_needed(prefix),
    )
    return plan.to_dict()


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _query_source_products 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 query source products 的候选结果，并按参数完成筛选、排序或数量限制。
def _query_source_products(prefix: str, event: dict[str, Any]) -> list[str]:
    """Infer relevant source products from the query prefix text."""
    text = prefix.lower()
    products: list[str] = []
    for token, product in (
        ("waf", "waf"), ("web", "web"), ("edr", "edr"), ("process", "edr"),
        ("host", "edr"), ("file", "edr"), ("dns", "dns"), ("proxy", "proxy"),
        ("netflow", "netflow"), ("outbound", "netflow"), ("vpn", "vpn"),
        ("auth", "sso"), ("mfa", "identity"), ("password", "identity"),
    ):
        if token in text:
            products.append(product)
    current = _source_product(event)
    if current:
        products.append(current)
    return _unique_texts(products)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _query_evidence_needed 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 query evidence needed 的候选结果，并按参数完成筛选、排序或数量限制。
def _query_evidence_needed(prefix: str) -> list[str]:
    """Infer what evidence types the query needs from its prefix text."""
    text = prefix.lower()
    needed: list[str] = []
    if "process" in text or "edr" in text or "host" in text:
        needed.append("process_tree")
    if "dns" in text or "proxy" in text or "netflow" in text or "outbound" in text:
        needed.append("network_flow")
    if "auth" in text or "vpn" in text or "mfa" in text or "password" in text:
        needed.append("identity_events")
    if "web" in text or "waf" in text or "http" in text:
        needed.append("web_request")
    if "timeline" in text:
        needed.append("cross_source_timeline")
    return needed or ["corroborating_events"]


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _stable_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 stable id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _stable_id(prefix: str, payload: Any) -> str:
    """Generate a stable SHA-256-based ID from *payload*."""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:16]}"


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _dedupe_findings 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 dedupe findings 涉及的字段，让后续匹配和存储使用同一形态。
def _dedupe_findings(findings: Sequence[Any]) -> list[Any]:
    """Remove duplicate findings by finding_id."""
    result: list[Any] = []
    seen: set[str] = set()
    for finding in findings:
        if finding.finding_id in seen:
            continue
        seen.add(finding.finding_id)
        result.append(finding)
    return result
