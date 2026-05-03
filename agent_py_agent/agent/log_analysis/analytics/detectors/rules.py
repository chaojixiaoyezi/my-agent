"""LLM: High-level soft detector rules and finding construction.

给人看的解释：
本模块包含 6 个高层检测器规则（WAF 攻击成功、Web 进程异常、VPN 新地理登录、
暴力破解后成功、告警后罕见外连、多源弱信号汇聚）以及 Finding 构造辅助函数。
底层字段访问和事件分类函数来自 field_access、field_extractors、classifiers 子模块。
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any, Mapping, Sequence

from ...models import EvidenceRef, Finding, QueryPlan, utc_now_iso
from ..baselines import SecurityBaselines, ensure_baselines
from ..security_rules import get_rule

from .field_access import (
    EventLike,
    JsonDict,
    _canonical_time,
    _clamp_float,
    _event_dict,
    _event_time,
    _field,
    _sort_time,
    _text,
    _time_bucket,
    _truthy,
    _within_after,
    _within_before,
    _window_for_events,
)
from .field_extractors import (
    _asset_ip,
    _cmdline,
    _destination_ip,
    _domain,
    _dst_port,
    _host,
    _parent_process_name,
    _process_name,
    _severity,
    _source_ip,
    _source_product,
    _user,
    _victim_ip,
)
from .classifiers import (
    _destination,
    _entities_from_events,
    _gap_details,
    _is_alert_event,
    _is_auth_event,
    _is_egress_event,
    _is_failure,
    _is_http_success_or_error,
    _is_suspicious_file_write,
    _is_suspicious_web_process_event,
    _is_success,
    _is_vpn_event,
    _is_waf_event,
    _normalize_entities,
    _primary_asset,
    _same_asset,
    _same_auth_scope,
    _same_source,
    _same_user,
    _unique_json_values,
    _unique_texts,
    _weak_signal,
    _gap_details,
)


# ---------------------------------------------------------------------------
# High-level detector rules
# ---------------------------------------------------------------------------

def run_soft_detectors(
    events: Sequence[EventLike],
    *,
    baselines: SecurityBaselines | dict[str, Any] | None = None,
) -> list[Finding]:
    """LLM: Run all soft detectors on *events* and return deduplicated findings.

    新手说明:
    对一组事件运行所有软检测器，返回去重后的发现列表。
    """
    baseline_obj = ensure_baselines(baselines)
    findings: list[Finding] = []
    for detector in (
        waf_attack_success_candidate,
        web_to_process_anomaly,
        vpn_new_geo_login,
        bruteforce_then_success,
        rare_egress_after_alert,
        multi_source_weak_signal,
    ):
        findings.extend(detector(events, baselines=baseline_obj))
    return _dedupe_findings(findings)


def waf_attack_success_candidate(
    events: Sequence[EventLike],
    *,
    baselines: SecurityBaselines | dict[str, Any] | None = None,
    window_minutes: int = 15,
) -> list[Finding]:
    """LLM: Detect WAF alerts followed by HTTP success, suspicious processes, file writes, or egress.

    新手说明:
    检测 WAF 告警后是否出现 HTTP 成功、可疑进程、文件写入或外连——可能表示攻击成功。
    """
    del baselines
    normalized = [_event_dict(event) for event in events]
    findings: list[Finding] = []
    for alert in normalized:
        if not _is_waf_event(alert):
            continue
        related = [
            event
            for event in normalized
            if event is not alert
            and _within_after(alert, event, window_minutes)
            and _same_asset(alert, event)
        ]
        http_success = [event for event in related if _is_http_success_or_error(event)]
        process_hits = [event for event in related if _is_suspicious_web_process_event(event)]
        file_hits = [event for event in related if _is_suspicious_file_write(event)]
        egress_hits = [event for event in related if _is_egress_event(event)]
        evidence_events = [alert, *http_success[:3], *process_hits[:3], *file_hits[:3], *egress_hits[:3]]
        if not (http_success or process_hits or file_hits or egress_hits):
            continue

        confidence = 0.54
        confidence += 0.09 if http_success else 0.0
        confidence += 0.15 if process_hits else 0.0
        confidence += 0.12 if file_hits else 0.0
        confidence += 0.1 if egress_hits else 0.0
        confidence = min(confidence, 0.88)

        gaps: list[str] = []
        if not process_hits:
            gaps.append("No EDR process event tied to the web service was observed in the window.")
        if not file_hits:
            gaps.append("No server-side file write evidence was observed in the window.")
        if not egress_hits:
            gaps.append("No outbound network evidence from the victim asset was observed in the window.")

        findings.append(
            _make_finding(
                "waf_attack_success_candidate",
                evidence_events,
                hypothesis="Possible web attack success: WAF/web alert was followed by server-side success indicators.",
                confidence=confidence,
                gaps=gaps,
                next_queries=[
                    _query("Trace web access/error logs around the WAF alert", alert),
                    _query("Trace EDR process tree for the victim asset around the alert", alert),
                    _query("Hunt outbound DNS/proxy/NetFlow from the victim asset after the alert", alert),
                ],
                features={
                    "http_success_or_error_events": len(http_success),
                    "process_anomaly_events": len(process_hits),
                    "file_write_events": len(file_hits),
                    "egress_events": len(egress_hits),
                },
                severity_hint="high" if confidence >= 0.7 else "medium",
                extra_entities={
                    "attacker_ip": [_source_ip(alert)],
                    "victim_ip": [_victim_ip(alert)],
                    "entry_uri": [_text(_field(alert, "uri", "api", "url", "http.url"))],
                },
            )
        )
    return findings


def web_to_process_anomaly(
    events: Sequence[EventLike],
    *,
    baselines: SecurityBaselines | dict[str, Any] | None = None,
    window_minutes: int = 10,
) -> list[Finding]:
    """LLM: Detect web server processes spawning suspicious child processes.

    新手说明:
    检测 Web 服务器进程（如 nginx/apache）是否启动了可疑子进程（如 bash/sh/curl）。
    """
    del baselines, window_minutes
    findings: list[Finding] = []
    for event in [_event_dict(item) for item in events]:
        if not _is_suspicious_web_process_event(event):
            continue
        parent = _parent_process_name(event)
        child = _process_name(event)
        cmdline = _cmdline(event)
        confidence = 0.76
        if any(token in cmdline.lower() for token in (" -c ", "/c ", "download", "http://", "https://", "base64")):
            confidence += 0.06
        if _severity(event) in {"high", "critical"}:
            confidence += 0.04
        findings.append(
            _make_finding(
                "web_to_process_anomaly",
                [event],
                hypothesis=f"Possible post-exploit command execution: web parent {parent or 'unknown'} launched {child or 'unknown'}.",
                confidence=min(confidence, 0.9),
                gaps=[
                    "The initiating HTTP request is not confirmed unless web access/WAF evidence is linked.",
                    "Process GUID or full process tree may be needed to prove execution lineage.",
                ],
                next_queries=[
                    _query("Find web requests to this host in the 10 minutes before the process event", event),
                    _query("Expand child process tree and file/network activity for this process", event),
                    _query("Search for matching command line on peer web servers", event),
                ],
                features={"parent_process": parent, "child_process": child, "cmdline": cmdline[:300]},
                severity_hint="high",
                extra_entities={
                    "victim_ip": [_asset_ip(event)],
                    "process": [child],
                    "parent_process": [parent],
                    "host": [_host(event)],
                },
            )
        )
    return findings


def vpn_new_geo_login(
    events: Sequence[EventLike],
    *,
    baselines: SecurityBaselines | dict[str, Any] | None = None,
    window_minutes: int = 60,
) -> list[Finding]:
    """LLM: Detect successful VPN logins from new geo/ASN/device or unusual hours.

    新手说明:
    检测 VPN 成功登录是否来自新的地理位置、ASN、设备或异常时段。
    """
    del window_minutes
    baseline_obj = ensure_baselines(baselines)
    findings: list[Finding] = []
    for event in [_event_dict(item) for item in events]:
        if not (_is_vpn_event(event) and _is_success(event)):
            continue
        user = _user(event)
        if not user:
            continue
        event_time = _event_time(event)
        country = _text(_field(event, "geo_country", "src_country", "country", "geo.country", "source.geo.country"))
        asn = _text(_field(event, "src_asn", "asn", "geo_asn", "source.asn"))
        device = _text(_field(event, "device_id", "device", "device_name", "client_device"))
        new_geo = _truthy(_field(event, "new_geo", "new_country", "is_new_geo")) or baseline_obj.is_new_country(user, country)
        new_asn = _truthy(_field(event, "new_asn", "is_new_asn")) or baseline_obj.is_new_asn(user, asn)
        new_device = _truthy(_field(event, "new_device", "is_new_device")) or baseline_obj.is_new_device(user, device)
        unusual_hour = _truthy(_field(event, "unusual_hour", "is_unusual_hour")) or baseline_obj.is_unusual_login_hour(
            user, event_time
        )
        if not (new_geo or new_asn or new_device or unusual_hour):
            continue

        confidence = min(
            0.58
            + (0.12 if new_geo else 0.0)
            + (0.08 if new_asn else 0.0)
            + (0.08 if new_device else 0.0)
            + (0.05 if unusual_hour else 0.0),
            0.86,
        )
        gaps = [
            "MFA result, device posture, and identity-risk context are not confirmed.",
            "Post-login host access is not yet correlated.",
        ]
        if not country:
            gaps.append("Source country is missing; GeoIP enrichment is needed.")
        if not asn:
            gaps.append("Source ASN is missing; ASN enrichment is needed.")

        findings.append(
            _make_finding(
                "vpn_new_geo_login",
                [event],
                hypothesis="Possible VPN credential misuse: successful login used new or unusual source context.",
                confidence=confidence,
                gaps=gaps,
                next_queries=[
                    _query("Trace VPN session activity and assigned internal IP for this user", event),
                    _query("Search host logons and admin actions by this user after VPN login", event),
                    _query("Review MFA, device posture, and recent password reset events for this user", event),
                ],
                features={
                    "new_geo": new_geo,
                    "new_asn": new_asn,
                    "new_device": new_device,
                    "unusual_hour": unusual_hour,
                    "country": country,
                    "asn": asn,
                    "device": device,
                },
                severity_hint="high" if confidence >= 0.7 else "medium",
                extra_entities={
                    "user": [user],
                    "attacker_ip": [_source_ip(event)],
                    "src_ip": [_source_ip(event)],
                    "country": [country],
                    "asn": [asn],
                    "device": [device],
                },
            )
        )
    return findings


def bruteforce_then_success(
    events: Sequence[EventLike],
    *,
    baselines: SecurityBaselines | dict[str, Any] | None = None,
    window_minutes: int = 15,
    failure_threshold: int = 5,
) -> list[Finding]:
    """LLM: Detect repeated auth failures followed by a successful login (credential compromise).

    新手说明:
    检测多次认证失败后出现成功登录——可能是暴力破解成功或凭据泄露。
    """
    del baselines
    normalized = sorted((_event_dict(item) for item in events), key=_sort_time)
    auth_events = [event for event in normalized if _is_auth_event(event)]
    findings: list[Finding] = []
    for success in auth_events:
        if not _is_success(success):
            continue
        related_failures = [
            event
            for event in auth_events
            if _is_failure(event) and _same_auth_scope(event, success) and _within_before(event, success, window_minutes)
        ]
        if len(related_failures) < failure_threshold:
            continue
        post_events = [
            event
            for event in normalized
            if event is not success
            and _within_after(success, event, window_minutes)
            and (_same_user(event, success) or _same_source(event, success) or _same_asset(success, event))
        ]
        confidence = min(0.68 + min(len(related_failures), 20) * 0.008 + (0.05 if post_events else 0.0), 0.88)
        findings.append(
            _make_finding(
                "bruteforce_then_success",
                [*related_failures[:5], success, *post_events[:5]],
                hypothesis="Possible credential compromise: repeated failures were followed by a successful login.",
                confidence=confidence,
                gaps=[
                    "MFA result and lockout policy outcome are not confirmed.",
                    "Credential owner confirmation is needed before treating the login as compromised.",
                ],
                next_queries=[
                    _query("Review all authentication events for this user and source around the success", success),
                    _query("Trace resource access and host logons after the successful authentication", success),
                    _query("Check MFA, password reset, lockout, and impossible travel signals", success),
                ],
                features={
                    "failure_count": len(related_failures),
                    "first_failure_time": _canonical_time(_event_time(related_failures[0])),
                    "success_time": _canonical_time(_event_time(success)),
                    "post_success_related_events": len(post_events),
                },
                severity_hint="high",
                extra_entities={
                    "user": [_user(success)],
                    "attacker_ip": [_source_ip(success)],
                    "src_ip": [_source_ip(success)],
                    "victim_ip": [_victim_ip(success)],
                    "host": [_host(success)],
                },
            )
        )
    return findings


def rare_egress_after_alert(
    events: Sequence[EventLike],
    *,
    baselines: SecurityBaselines | dict[str, Any] | None = None,
    window_minutes: int = 30,
) -> list[Finding]:
    """LLM: Detect rare outbound connections from alerted assets.

    新手说明:
    检测告警资产是否发起了罕见的出站连接——可能是 C2 回连或数据外泄。
    """
    baseline_obj = ensure_baselines(baselines)
    normalized = [_event_dict(item) for item in events]
    alerts = [event for event in normalized if _is_alert_event(event)]
    egress_events = [event for event in normalized if _is_egress_event(event)]
    findings: list[Finding] = []
    for alert in alerts:
        candidates: list[JsonDict] = []
        for event in egress_events:
            if not (_within_after(alert, event, window_minutes) and _same_asset(alert, event)):
                continue
            asset = _primary_asset(alert) or _primary_asset(event)
            destination = _destination(event)
            port = _dst_port(event)
            rare_destination = _truthy(_field(event, "rare", "is_rare", "new_dst", "new_destination")) or (
                bool(destination) and baseline_obj.is_rare_egress_destination(asset, destination)
            )
            rare_port = _truthy(_field(event, "rare_port", "new_port")) or baseline_obj.is_rare_egress_port(asset, port)
            if rare_destination or rare_port or _severity(alert) in {"high", "critical"}:
                candidates.append(event)
        if not candidates:
            continue
        confidence = min(0.62 + (0.08 if _severity(alert) in {"high", "critical"} else 0.0) + min(len(candidates), 4) * 0.035, 0.86)
        findings.append(
            _make_finding(
                "rare_egress_after_alert",
                [alert, *candidates[:6]],
                hypothesis="Possible post-alert command-and-control or exfiltration: alerted asset made rare outbound connections.",
                confidence=confidence,
                gaps=[
                    "Outbound process owner is missing unless EDR network telemetry is linked.",
                    "DNS/proxy and byte-count context are needed to distinguish callback from benign update traffic.",
                ],
                next_queries=[
                    _query("Trace DNS, proxy, and NetFlow rows for the rare destination", candidates[0]),
                    _query("Find the local process that opened the outbound connection", candidates[0]),
                    _query("Search the rare destination across other assets for lateral spread", candidates[0]),
                ],
                features={
                    "rare_egress_events": len(candidates),
                    "destinations": _unique_texts(_destination(event) for event in candidates),
                    "alert_severity": _severity(alert),
                },
                severity_hint="high",
                extra_entities={
                    "attacker_ip": [_source_ip(alert)],
                    "victim_ip": [_victim_ip(alert), _source_ip(candidates[0])],
                    "dst_ip": [_destination_ip(candidates[0])],
                    "domain": [_domain(candidates[0])],
                },
            )
        )
    return findings


def multi_source_weak_signal(
    events: Sequence[EventLike],
    *,
    baselines: SecurityBaselines | dict[str, Any] | None = None,
    window_minutes: int = 15,
) -> list[Finding]:
    """LLM: Detect overlapping weak signals from multiple sources on the same entity.

    新手说明:
    检测同一实体在同一时间窗口内是否有来自不同来源的弱信号汇聚——可能表示入侵路径。
    """
    del baselines
    normalized = sorted((_event_dict(item) for item in events), key=_sort_time)
    weak_signals = [signal for signal in (_weak_signal(event) for event in normalized) if signal]
    groups: dict[tuple[str, str], list[JsonDict]] = {}
    for signal in weak_signals:
        event = signal["event"]
        key = (_primary_asset(event) or _user(event) or _source_ip(event), _time_bucket(_event_time(event), window_minutes))
        if key[0]:
            groups.setdefault(key, []).append(signal)

    findings: list[Finding] = []
    for (_entity, _bucket), signals in groups.items():
        signal_types = {str(signal["signal_type"]) for signal in signals}
        sources = {str(signal["source_product"]) for signal in signals if signal.get("source_product")}
        if len(signal_types) < 2 and len(sources) < 2:
            continue
        signal_events = [signal["event"] for signal in signals]
        confidence = min(0.53 + 0.06 * len(signal_types) + 0.035 * len(sources), 0.82)
        findings.append(
            _make_finding(
                "multi_source_weak_signal",
                signal_events[:10],
                hypothesis="Possible intrusion path: multiple weak signals overlap on the same entity and time window.",
                confidence=confidence,
                gaps=[
                    "Signals are individually weak; analyst review must confirm whether they share one root cause.",
                    "A route draft needs additional process, identity, and network context.",
                ],
                next_queries=[
                    _query("Build a single timeline for the overlapping entity across WAF/VPN/EDR/DNS/proxy logs", signal_events[0]),
                    _query("Expand related entities from the overlapping weak signals", signal_events[0]),
                    _query("Check known maintenance, deployment, and vulnerability-scan windows", signal_events[0]),
                ],
                features={
                    "signal_count": len(signals),
                    "signal_types": sorted(signal_types),
                    "source_products": sorted(sources),
                },
                severity_hint="high" if confidence >= 0.7 else "medium",
                extra_entities=_entities_from_events(signal_events),
            )
        )
    return findings


# Backward-compatible aliases
detect_waf_attack_success_candidate = waf_attack_success_candidate
detect_web_to_process_anomaly = web_to_process_anomaly
detect_vpn_new_geo_login = vpn_new_geo_login
detect_bruteforce_then_success = bruteforce_then_success
detect_rare_egress_after_alert = rare_egress_after_alert
detect_multi_source_weak_signal = multi_source_weak_signal


DETECTORS = {
    "waf_attack_success_candidate": waf_attack_success_candidate,
    "web_to_process_anomaly": web_to_process_anomaly,
    "vpn_new_geo_login": vpn_new_geo_login,
    "bruteforce_then_success": bruteforce_then_success,
    "rare_egress_after_alert": rare_egress_after_alert,
    "multi_source_weak_signal": multi_source_weak_signal,
}


# ---------------------------------------------------------------------------
# Finding construction helpers
# ---------------------------------------------------------------------------

def _make_finding(
    detector_id: str,
    evidence_events: Sequence[JsonDict],
    *,
    hypothesis: str,
    confidence: float,
    gaps: Sequence[str],
    next_queries: Sequence[Any],
    features: Mapping[str, Any] | None = None,
    severity_hint: str | None = None,
    extra_entities: Mapping[str, Sequence[Any]] | None = None,
) -> Finding:
    """LLM: Build a Finding object from detector output.

    新手说明:
    把检测器的输出组装成标准的 Finding 对象（包含证据、置信度、下一步查询等）。
    """
    rule = get_rule(detector_id)
    entities = _entities_from_events(evidence_events)
    for key, values in (extra_entities or {}).items():
        entities.setdefault(key, [])
        entities[key].extend(_text(value) for value in values if _text(value))
    entities = _normalize_entities(entities)
    evidence_refs = [_evidence_ref(event) for event in evidence_events]
    window = list(_window_for_events(evidence_events))
    payload_for_id = {
        "detector_id": detector_id,
        "window": window,
        "entities": entities,
        "evidence_refs": [ref.evidence_id for ref in evidence_refs],
        "hypothesis": hypothesis,
    }
    finding = Finding(
        finding_id=_stable_id("finding", payload_for_id),
        detector_id=detector_id,
        detector_kind=rule.detector_kind,
        window=window,
        severity_hint=severity_hint or rule.severity_hint,
        risk_score=_clamp_float(confidence),
        entities=entities,
        features=dict(features or {}),
        evidence_refs=evidence_refs,
        status="OPEN",
        hypothesis=hypothesis,
        confidence=_clamp_float(confidence),
        gaps=_unique_texts(gaps),
        next_queries=_unique_json_values(next_queries),
        rule_version=rule.version,
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
        attributes={"mode": rule.mode, "gap_details": _gap_details(gaps, window, entities, evidence_events)},
    )
    setattr(finding, "mode", rule.mode)
    return finding


def _evidence_id(event: Mapping[str, Any]) -> str:
    """LLM: Return a stable evidence identifier for *event*.

    新手说明:
    从事件中提取证据 ID，优先使用已有字段，否则生成哈希 ID。
    """
    for field_name in ("raw_ref", "evidence_ref", "event_id", "security_event_id", "alert_id", "id"):
        value = _text(_field(event, field_name))
        if value:
            return value
    return _stable_id("event", event)


def _evidence_ref(event: Mapping[str, Any]) -> EvidenceRef:
    """LLM: Build an EvidenceRef from *event*.

    新手说明:
    把事件包装成标准的 EvidenceRef 对象，用于 Finding 的证据列表。
    """
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


def _query(prefix: str, event: Mapping[str, Any]) -> dict[str, Any]:
    """LLM: Build a follow-up QueryPlan dict for *event*.

    新手说明:
    根据事件信息生成下一步查询计划（用于分析师或自动化后续调查）。
    """
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


def _query_source_products(prefix: str, event: Mapping[str, Any]) -> list[str]:
    """LLM: Infer relevant source products from the query prefix text.

    新手说明:
    根据查询描述推断需要搜索的日志源产品（WAF、EDR、DNS 等）。
    """
    text = prefix.lower()
    products: list[str] = []
    for token, product in (
        ("waf", "waf"),
        ("web", "web"),
        ("edr", "edr"),
        ("process", "edr"),
        ("host", "edr"),
        ("file", "edr"),
        ("dns", "dns"),
        ("proxy", "proxy"),
        ("netflow", "netflow"),
        ("outbound", "netflow"),
        ("vpn", "vpn"),
        ("auth", "sso"),
        ("mfa", "identity"),
        ("password", "identity"),
    ):
        if token in text:
            products.append(product)
    current = _source_product(event)
    if current:
        products.append(current)
    return _unique_texts(products)


def _query_evidence_needed(prefix: str) -> list[str]:
    """LLM: Infer what evidence types the query needs from its prefix text.

    新手说明:
    根据查询描述推断需要的证据类型（进程树、网络流、身份事件等）。
    """
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
    """LLM: Generate a stable SHA-256-based ID from *payload*.

    新手说明:
    用 JSON 序列化 + SHA-256 生成稳定的唯一 ID。
    """
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:16]}"


def _dedupe_findings(findings: Sequence[Finding]) -> list[Finding]:
    """LLM: Remove duplicate findings by finding_id.

    新手说明:
    对 Finding 列表按 finding_id 去重。
    """
    result: list[Finding] = []
    seen: set[str] = set()
    for finding in findings:
        if finding.finding_id in seen:
            continue
        seen.add(finding.finding_id)
        result.append(finding)
    return result


__all__ = [
    "DETECTORS",
    "bruteforce_then_success",
    "detect_bruteforce_then_success",
    "detect_multi_source_weak_signal",
    "detect_rare_egress_after_alert",
    "detect_vpn_new_geo_login",
    "detect_waf_attack_success_candidate",
    "detect_web_to_process_anomaly",
    "multi_source_weak_signal",
    "rare_egress_after_alert",
    "run_soft_detectors",
    "vpn_new_geo_login",
    "waf_attack_success_candidate",
    "web_to_process_anomaly",
]
