from __future__ import annotations

"""First-pass soft detectors for local log analysis."""

import hashlib
import ipaddress
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from ..models import EvidenceRef, Finding, utc_now_iso
from .baselines import SecurityBaselines, ensure_baselines
from .security_rules import get_rule


JsonDict = dict[str, Any]
EventLike = Mapping[str, Any] | object


WEB_PARENT_PROCESSES = {
    "apache",
    "apache2",
    "caddy",
    "gunicorn",
    "httpd",
    "iisexpress",
    "java",
    "nginx",
    "node",
    "php-cgi",
    "php-fpm",
    "python",
    "tomcat",
    "uwsgi",
    "w3wp.exe",
}

SUSPICIOUS_CHILD_PROCESSES = {
    "bash",
    "bitsadmin.exe",
    "certutil.exe",
    "cmd.exe",
    "curl",
    "curl.exe",
    "mshta.exe",
    "nc",
    "ncat",
    "netcat",
    "perl",
    "php",
    "powershell.exe",
    "pwsh",
    "python",
    "python.exe",
    "regsvr32.exe",
    "ruby",
    "sh",
    "wget",
    "wget.exe",
    "wmic.exe",
}


def run_soft_detectors(
    events: Sequence[EventLike],
    *,
    baselines: SecurityBaselines | dict[str, Any] | None = None,
) -> list[Finding]:
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


def _make_finding(
    detector_id: str,
    evidence_events: Sequence[JsonDict],
    *,
    hypothesis: str,
    confidence: float,
    gaps: Sequence[str],
    next_queries: Sequence[str],
    features: Mapping[str, Any] | None = None,
    severity_hint: str | None = None,
    extra_entities: Mapping[str, Sequence[Any]] | None = None,
) -> Finding:
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
        next_queries=_unique_texts(next_queries),
        rule_version=rule.version,
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
        attributes={"mode": rule.mode},
    )
    setattr(finding, "mode", rule.mode)
    return finding


def _event_dict(event: EventLike) -> JsonDict:
    if isinstance(event, Mapping):
        return dict(event)
    if is_dataclass(event):
        return asdict(event)
    to_dict = getattr(event, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        if isinstance(value, Mapping):
            return dict(value)
    try:
        return dict(vars(event))
    except TypeError:
        return {"value": event}


def _field(payload: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = _path_value(payload, name)
        if _present(value):
            return value
    for bag_name in ("attributes", "raw_fields", "event", "security", "network", "http", "process", "file", "rule"):
        bag = _path_value(payload, bag_name)
        if isinstance(bag, Mapping):
            for name in names:
                value = _path_value(bag, name)
                if _present(value):
                    return value
    return None


def _path_value(payload: Mapping[str, Any], path: str) -> Any:
    if path in payload:
        return payload[path]
    lower_map = {str(key).lower(): key for key in payload}
    direct_key = lower_map.get(path.lower())
    if direct_key is not None:
        return payload[direct_key]
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        lower = {str(key).lower(): key for key in value}
        key = lower.get(part.lower())
        if key is None:
            return None
        value = value[key]
    return value


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = _text(value).lower()
    return text in {"1", "true", "yes", "y", "new", "rare", "unusual"}


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp_float(value: Any) -> float:
    clean = _to_float(value)
    if clean is None:
        return 0.0
    return max(0.0, min(1.0, clean))


def _event_time(event: Mapping[str, Any]) -> datetime | None:
    return _parse_time(_field(event, "event_time", "@timestamp", "timestamp", "time", "created_at", "ingest_time"))


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    text = _text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _canonical_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sort_time(event: Mapping[str, Any]) -> tuple[int, str]:
    when = _event_time(event)
    return (0, _canonical_time(when)) if when is not None else (1, _evidence_id(event))


def _within_after(start: Mapping[str, Any], candidate: Mapping[str, Any], minutes: int) -> bool:
    start_time = _event_time(start)
    candidate_time = _event_time(candidate)
    if start_time is None or candidate_time is None:
        return False
    return start_time <= candidate_time <= start_time + timedelta(minutes=minutes)


def _within_before(candidate: Mapping[str, Any], end: Mapping[str, Any], minutes: int) -> bool:
    candidate_time = _event_time(candidate)
    end_time = _event_time(end)
    if candidate_time is None or end_time is None:
        return False
    return end_time - timedelta(minutes=minutes) <= candidate_time <= end_time


def _window_for_events(events: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    times = sorted(time for time in (_event_time(event) for event in events) if time is not None)
    if not times:
        stamp = utc_now_iso()
        return (stamp, stamp)
    return (_canonical_time(times[0]), _canonical_time(times[-1]))


def _time_bucket(value: datetime | None, minutes: int) -> str:
    if value is None:
        return "unknown-time"
    minute = (value.minute // max(minutes, 1)) * max(minutes, 1)
    bucket = value.astimezone(timezone.utc).replace(minute=minute, second=0, microsecond=0)
    return _canonical_time(bucket)


def _source_product(event: Mapping[str, Any]) -> str:
    return _text(_field(event, "source_product", "product", "source", "source_id")).lower()


def _event_class(event: Mapping[str, Any]) -> str:
    return _text(_field(event, "event_class", "class", "category", "type", "event_type")).lower()


def _event_action(event: Mapping[str, Any]) -> str:
    return _text(_field(event, "event_action", "action", "operation")).lower()


def _outcome(event: Mapping[str, Any]) -> str:
    return _text(_field(event, "event_outcome", "outcome", "result", "auth_result", "login_result")).lower()


def _severity(event: Mapping[str, Any]) -> str:
    return _text(_field(event, "severity", "severity_hint", "level", "risk_level")).lower()


def _source_ip(event: Mapping[str, Any]) -> str:
    return _text(_field(event, "attacker_ip", "src_ip", "source_ip", "client_ip", "remote_ip", "source.ip"))


def _destination_ip(event: Mapping[str, Any]) -> str:
    return _text(_field(event, "dst_ip", "destination_ip", "dest_ip", "destination.ip"))


def _victim_ip(event: Mapping[str, Any]) -> str:
    return _text(_field(event, "victim_ip", "asset_ip", "host_ip", "dst_ip", "destination_ip"))


def _asset_ip(event: Mapping[str, Any]) -> str:
    return _text(_field(event, "victim_ip", "asset_ip", "host_ip", "src_ip", "dst_ip"))


def _host(event: Mapping[str, Any]) -> str:
    return _text(_field(event, "host", "hostname", "asset_id", "device_name", "computer_name"))


def _user(event: Mapping[str, Any]) -> str:
    return _text(_field(event, "user", "username", "account", "principal", "user.name"))


def _domain(event: Mapping[str, Any]) -> str:
    return _text(_field(event, "domain", "dns_query", "query", "host_header", "sni", "network.domain"))


def _dst_port(event: Mapping[str, Any]) -> int | None:
    return _to_int(_field(event, "dst_port", "destination_port", "port", "network.dst_port"))


def _process_name(event: Mapping[str, Any]) -> str:
    return _basename(_field(event, "process.name", "process_name", "image", "process", "child_process"))


def _parent_process_name(event: Mapping[str, Any]) -> str:
    return _basename(_field(event, "process.parent_name", "parent_process_name", "parent_process", "parent.name"))


def _cmdline(event: Mapping[str, Any]) -> str:
    return _text(_field(event, "process.cmdline", "cmdline", "command_line", "process_command_line"))


def _basename(value: Any) -> str:
    text = _text(value).replace("\\", "/")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text.strip().lower()


def _is_alert_event(event: Mapping[str, Any]) -> bool:
    if _event_class(event) == "alert":
        return True
    if _present(_field(event, "alert_type", "threat_name", "alert_rule", "ioc_or_rule_id")):
        return True
    return _severity(event) in {"high", "critical"}


def _is_waf_event(event: Mapping[str, Any]) -> bool:
    product = _source_product(event)
    alert_text = " ".join(
        _text(_field(event, name))
        for name in ("alert_type", "threat_name", "alert_rule", "api_threat_type", "owasp_type")
    ).lower()
    has_web_fields = _present(_field(event, "uri", "api", "url", "payload", "http.url"))
    return "waf" in product or ("web" in alert_text and has_web_fields) or ("injection" in alert_text and has_web_fields)


def _is_http_success_or_error(event: Mapping[str, Any]) -> bool:
    status = _to_int(_field(event, "status_code", "http_status", "http.status_code", "response_status"))
    if status is None:
        return False
    return 200 <= status < 300 or status >= 500


def _is_suspicious_web_process_event(event: Mapping[str, Any]) -> bool:
    parent = _parent_process_name(event)
    child = _process_name(event)
    action = _event_action(event)
    event_class = _event_class(event)
    if event_class and event_class not in {"process", "alert", "endpoint", "edr"}:
        return False
    if action and not any(token in action for token in ("exec", "process", "start", "spawn", "create")):
        return False
    return parent in WEB_PARENT_PROCESSES and child in SUSPICIOUS_CHILD_PROCESSES


def _is_suspicious_file_write(event: Mapping[str, Any]) -> bool:
    event_class = _event_class(event)
    action = _event_action(event)
    if event_class and event_class not in {"file", "alert", "endpoint", "edr"}:
        return False
    if action and not any(token in action for token in ("write", "create", "modify", "drop")):
        return False
    path = _text(_field(event, "file.path", "path", "file_path", "target_path")).lower()
    if not path:
        return False
    web_ext = (".php", ".jsp", ".jspx", ".asp", ".aspx", ".ashx", ".war", ".js", ".sh", ".ps1")
    web_dirs = ("/var/www", "/usr/share/nginx", "/webapps", "\\inetpub", "/inetpub", "/tmp", "/uploads")
    return path.endswith(web_ext) or any(item in path for item in web_dirs)


def _is_egress_event(event: Mapping[str, Any]) -> bool:
    event_class = _event_class(event)
    action = _event_action(event)
    if event_class and event_class not in {"network", "dns", "proxy", "netflow", "alert", "connection"}:
        return False
    if action and not any(token in action for token in ("connect", "dns", "http", "flow", "request")):
        return False
    src = _text(_field(event, "src_ip", "source_ip", "source.ip"))
    dst = _destination_ip(event)
    domain = _domain(event)
    if not (dst or domain):
        return False
    if dst:
        return bool(src) and not _is_internal_ip(dst)
    return bool(src and domain)


def _is_internal_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    private_networks = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("169.254.0.0/16"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fc00::/7"),
        ipaddress.ip_network("fe80::/10"),
    )
    return any(ip in network for network in private_networks)


def _is_vpn_event(event: Mapping[str, Any]) -> bool:
    product = _source_product(event)
    return "vpn" in product or ("vpn" in _event_action(event) and _is_auth_event(event))


def _is_auth_event(event: Mapping[str, Any]) -> bool:
    event_class = _event_class(event)
    action = _event_action(event)
    product = _source_product(event)
    return event_class in {"auth", "authentication", "identity"} or "login" in action or "auth" in action or "vpn" in product


def _is_success(event: Mapping[str, Any]) -> bool:
    return _outcome(event) in {"success", "succeeded", "successful", "allowed", "ok", "accepted", "pass"}


def _is_failure(event: Mapping[str, Any]) -> bool:
    return _outcome(event) in {"failure", "failed", "fail", "denied", "blocked", "rejected", "invalid"}


def _same_auth_scope(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_user = _user(left)
    right_user = _user(right)
    left_src = _source_ip(left)
    right_src = _source_ip(right)
    if left_user and right_user and left_src and right_src:
        return left_user == right_user and left_src == right_src
    if left_user and right_user:
        return left_user == right_user
    return bool(left_src and right_src and left_src == right_src)


def _same_user(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_user = _user(left)
    return bool(left_user and left_user == _user(right))


def _same_source(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_src = _source_ip(left)
    return bool(left_src and left_src == _source_ip(right))


def _same_asset(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_assets = set(_asset_candidates(left))
    right_assets = set(_asset_candidates(right))
    return bool(left_assets and right_assets and left_assets.intersection(right_assets))


def _asset_candidates(event: Mapping[str, Any]) -> list[str]:
    values = [
        _text(_field(event, "victim_ip")),
        _text(_field(event, "asset_ip")),
        _text(_field(event, "host_ip")),
        _text(_field(event, "dst_ip")) if not _is_egress_event(event) else "",
        _text(_field(event, "src_ip")) if _is_egress_event(event) else "",
        _host(event),
        _text(_field(event, "asset_id")),
    ]
    return _unique_texts(values)


def _primary_asset(event: Mapping[str, Any]) -> str:
    candidates = _asset_candidates(event)
    return candidates[0] if candidates else ""


def _destination(event: Mapping[str, Any]) -> str:
    return _destination_ip(event) or _domain(event)


def _weak_signal(event: JsonDict) -> JsonDict | None:
    signal_type = ""
    if _is_waf_event(event):
        signal_type = "waf_web_alert"
    elif _is_vpn_event(event) and _is_success(event):
        if _truthy(_field(event, "new_geo", "new_asn", "new_device", "unusual_hour")):
            signal_type = "vpn_novel_login"
    elif _is_auth_event(event) and _is_failure(event):
        signal_type = "auth_failure"
    elif _is_suspicious_web_process_event(event):
        signal_type = "web_process"
    elif _is_suspicious_file_write(event):
        signal_type = "file_write"
    elif _is_egress_event(event) and (
        _truthy(_field(event, "rare", "is_rare", "new_dst", "new_destination")) or _destination_ip(event)
    ):
        signal_type = "egress"
    elif _is_alert_event(event) and _severity(event) in {"low", "medium", "high", "critical"}:
        signal_type = "alert"
    if not signal_type:
        return None
    return {"signal_type": signal_type, "source_product": _source_product(event), "event": event}


def _entities_from_events(events: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    entities: dict[str, list[str]] = {}
    for event in events:
        candidates: dict[str, list[str]] = {
            "src_ip": [_text(_field(event, "src_ip", "source_ip", "client_ip", "source.ip"))],
            "dst_ip": [_destination_ip(event)],
            "victim_ip": [_victim_ip(event)],
            "host": [_host(event)],
            "user": [_user(event)],
            "domain": [_domain(event)],
            "process": [_process_name(event)],
            "parent_process": [_parent_process_name(event)],
        }
        explicit_attacker = _text(_field(event, "attacker_ip"))
        if explicit_attacker:
            candidates["attacker_ip"] = [explicit_attacker]
        for key, values in candidates.items():
            entities.setdefault(key, [])
            entities[key].extend(value for value in values if value)
    return _normalize_entities(entities)


def _normalize_entities(entities: Mapping[str, Sequence[Any]]) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    for key, values in entities.items():
        clean_key = _text(key)
        if not clean_key:
            continue
        normalized[clean_key] = _unique_texts(_text(value) for value in values if _text(value))
    return {key: values for key, values in sorted(normalized.items()) if values}


def _unique_texts(values: Sequence[Any] | Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _evidence_id(event: Mapping[str, Any]) -> str:
    for field_name in ("raw_ref", "evidence_ref", "event_id", "security_event_id", "alert_id", "id"):
        value = _text(_field(event, field_name))
        if value:
            return value
    return _stable_id("event", event)


def _evidence_ref(event: Mapping[str, Any]) -> EvidenceRef:
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


def _query(prefix: str, event: Mapping[str, Any]) -> str:
    parts = [prefix]
    when = _canonical_time(_event_time(event))
    if when:
        parts.append(f"time={when}")
    for label, value in (
        ("src_ip", _source_ip(event)),
        ("victim_ip", _victim_ip(event)),
        ("host", _host(event)),
        ("user", _user(event)),
        ("domain", _domain(event)),
    ):
        if value:
            parts.append(f"{label}={value}")
    return " | ".join(parts)


def _stable_id(prefix: str, payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:16]}"


def _dedupe_findings(findings: Sequence[Finding]) -> list[Finding]:
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
    "Finding",
    "SUSPICIOUS_CHILD_PROCESSES",
    "WEB_PARENT_PROCESSES",
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
