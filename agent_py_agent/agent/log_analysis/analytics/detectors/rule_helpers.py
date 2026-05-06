from __future__ import annotations

"""LLM: shared helper functions for rule evaluation and finding construction.

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


def _make_finding(
    detector_id: str,
    evidence_events: Sequence[Any],
    *,
    params: MakeFindingParams,
) -> Any:
    """LLM: Build a Finding object from detector output.

    Args:
        detector_id: Detector identifier.
        evidence_events: Sequence of evidence events.
        params: Params bundle containing all finding construction args.
    """
    rule = get_rule(params.detector_id)
    entities = _finding_entities(params)
    evidence_refs = [_evidence_ref(event) for event in params.evidence_events]
    window = list(_window_for_events(params.evidence_events))
    from ...models import Finding

    finding = Finding(
        finding_id=_stable_id("finding", _finding_id_payload(params, window, entities, evidence_refs)),
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


def _finding_entities(params: MakeFindingParams) -> dict[str, list[str]]:
    entities = _entities_from_events(params.evidence_events)
    for key, values in (params.extra_entities or {}).items():
        entities.setdefault(key, [])
        entities[key].extend(_text(value) for value in values if _text(value))
    return _normalize_entities(entities)


def _finding_id_payload(
    params: MakeFindingParams,
    window: list[str],
    entities: dict[str, list[str]],
    evidence_refs: Sequence[EvidenceRef],
) -> dict[str, Any]:
    return {
        "detector_id": params.detector_id,
        "window": window,
        "entities": entities,
        "evidence_refs": [ref.evidence_id for ref in evidence_refs],
        "hypothesis": params.hypothesis,
    }


def _evidence_id(event: dict[str, Any]) -> str:
    """LLM: Return a stable evidence identifier for *event*."""
    for field_name in ("raw_ref", "evidence_ref", "event_id", "security_event_id", "alert_id", "id"):
        value = _text(_field(event, field_name))
        if value:
            return value
    return _stable_id("event", event)


def _evidence_ref(event: dict[str, Any]) -> EvidenceRef:
    """LLM: Build an EvidenceRef from *event*."""
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


def _query(prefix: str, event: dict[str, Any]) -> dict[str, Any]:
    """LLM: Build a follow-up QueryPlan dict for *event*."""
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


def _query_source_products(prefix: str, event: dict[str, Any]) -> list[str]:
    """LLM: Infer relevant source products from the query prefix text."""
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


def _query_evidence_needed(prefix: str) -> list[str]:
    """LLM: Infer what evidence types the query needs from its prefix text."""
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


def _stable_id(prefix: str, payload: Any) -> str:
    """LLM: Generate a stable SHA-256-based ID from *payload*."""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:16]}"


def _dedupe_findings(findings: Sequence[Any]) -> list[Any]:
    """LLM: Remove duplicate findings by finding_id."""
    result: list[Any] = []
    seen: set[str] = set()
    for finding in findings:
        if finding.finding_id in seen:
            continue
        seen.add(finding.finding_id)
        result.append(finding)
    return result
