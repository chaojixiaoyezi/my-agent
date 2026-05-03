from __future__ import annotations

"""LLM: rule matching and scoring logic for log analysis detectors.

新手说明:
这个文件放的是检测器规则的评估和评分逻辑。
给定事件列表，运行各个检测器并计算置信度。
"""

from collections.abc import Sequence
from typing import Any

from .classifiers import (
    _destination,
    _is_alert_event,
    _is_auth_event,
    _is_egress_event,
    _is_failure,
    _is_http_success_or_error,
    _is_success,
    _is_suspicious_file_write,
    _is_suspicious_web_process_event,
    _is_vpn_event,
    _is_waf_event,
    _primary_asset,
    _same_asset,
    _same_auth_scope,
    _same_source,
    _same_user,
    _weak_signal,
)
from .field_access import (
    EventLike,
    JsonDict,
    _event_dict,
    _field,
    _sort_time,
    _text,
    _time_bucket,
    _truthy,
    _within_after,
    _within_before,
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
from .rule_helpers import (
    _dedupe_findings,
    _evidence_ref,
    _make_finding,
    _query,
    _stable_id,
)


def run_soft_detectors(
    events: Sequence[EventLike],
    *,
    baselines: Any = None,
) -> list[Any]:
    """LLM: Run all soft detectors on *events* and return deduplicated findings."""
    from ..baselines import ensure_baselines

    baseline_obj = ensure_baselines(baselines)
    findings: list[Any] = []
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
    baselines: Any = None,
    window_minutes: int = 15,
) -> list[Any]:
    """LLM: Detect WAF alerts followed by HTTP success, suspicious processes, file writes, or egress."""
    del baselines
    normalized = [_event_dict(event) for event in events]
    findings: list[Any] = []
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
    baselines: Any = None,
    window_minutes: int = 10,
) -> list[Any]:
    """LLM: Detect web server processes spawning suspicious child processes."""
    del baselines, window_minutes
    findings: list[Any] = []
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
    baselines: Any = None,
    window_minutes: int = 60,
) -> list[Any]:
    """LLM: Detect successful VPN logins from new geo/ASN/device or unusual hours."""
    del window_minutes
    from ..baselines import ensure_baselines

    baseline_obj = ensure_baselines(baselines)
    findings: list[Any] = []
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
    baselines: Any = None,
    window_minutes: int = 15,
    failure_threshold: int = 5,
) -> list[Any]:
    """LLM: Detect repeated auth failures followed by a successful login (credential compromise)."""
    del baselines
    normalized = sorted((_event_dict(item) for item in events), key=_sort_time)
    auth_events = [event for event in normalized if _is_auth_event(event)]
    findings: list[Any] = []
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
    baselines: Any = None,
    window_minutes: int = 30,
) -> list[Any]:
    """LLM: Detect rare outbound connections from alerted assets."""
    from ..baselines import ensure_baselines

    baseline_obj = ensure_baselines(baselines)
    normalized = [_event_dict(item) for item in events]
    alerts = [event for event in normalized if _is_alert_event(event)]
    egress_events = [event for event in normalized if _is_egress_event(event)]
    findings: list[Any] = []
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
                    "destinations": _unique_texts(_destination(e) for e in candidates),
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
    baselines: Any = None,
    window_minutes: int = 15,
) -> list[Any]:
    """LLM: Detect overlapping weak signals from multiple sources on the same entity."""
    del baselines
    normalized = sorted((_event_dict(item) for item in events), key=_sort_time)
    weak_signals = [signal for signal in (_weak_signal(event) for event in normalized) if signal]
    groups: dict[tuple[str, str], list[JsonDict]] = {}
    for signal in weak_signals:
        event = signal["event"]
        key = (_primary_asset(event) or _user(event) or _source_ip(event), _time_bucket(_event_time(event), window_minutes))
        if key[0]:
            groups.setdefault(key, []).append(signal)

    findings: list[Any] = []
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
