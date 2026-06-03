
from __future__ import annotations

"""Web and network-oriented soft detector rules."""

from collections.abc import Sequence
from typing import Any

from .classifiers import (
    _destination,
    _entities_from_events,
    _is_alert_event,
    _is_egress_event,
    _is_http_success_or_error,
    _is_suspicious_file_write,
    _is_suspicious_web_process_event,
    _is_waf_event,
    _primary_asset,
    _same_asset,
    _unique_texts,
)
from .field_access import EventLike, JsonDict, _event_dict, _field, _text, _within_after
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
    _victim_ip,
)
from .rule_helpers import MakeFindingParams, _make_finding, _query


def waf_attack_success_candidate(
    events: Sequence[EventLike], *, baselines: Any = None, window_minutes: int = 15
) -> list[Any]:
    """Detect WAF alerts followed by success or server-side impact signals."""
    del baselines
    normalized = [_event_dict(event) for event in events]
    findings: list[Any] = []
    for alert in normalized:
        if not _is_waf_event(alert):
            continue
        signal_groups = _waf_signal_groups(alert, normalized, window_minutes)
        evidence_events = _waf_evidence_events(alert, signal_groups)
        if len(evidence_events) <= 1:
            continue
        findings.append(_waf_finding(alert, evidence_events, signal_groups))
    return findings


def web_to_process_anomaly(
    events: Sequence[EventLike], *, baselines: Any = None, window_minutes: int = 10
) -> list[Any]:
    """Detect web server processes spawning suspicious child processes."""
    del baselines, window_minutes
    findings: list[Any] = []
    for event in [_event_dict(item) for item in events]:
        if _is_suspicious_web_process_event(event):
            findings.append(_web_process_finding(event))
    return findings


def rare_egress_after_alert(
    events: Sequence[EventLike], *, baselines: Any = None, window_minutes: int = 30
) -> list[Any]:
    """Detect rare outbound connections from alerted assets."""
    from ..baselines import ensure_baselines

    baseline_obj = ensure_baselines(baselines)
    normalized = [_event_dict(item) for item in events]
    egress_events = [event for event in normalized if _is_egress_event(event)]
    findings: list[Any] = []
    for alert in [event for event in normalized if _is_alert_event(event)]:
        candidates = _rare_egress_candidates(alert, egress_events, baseline_obj, window_minutes)
        if candidates:
            findings.append(_rare_egress_finding(alert, candidates))
    return findings


def _waf_signal_groups(alert: JsonDict, events: list[JsonDict], window_minutes: int) -> dict[str, list[JsonDict]]:
    related = [event for event in events if event is not alert and _within_after(alert, event, window_minutes) and _same_asset(alert, event)]
    return {
        "http": [event for event in related if _is_http_success_or_error(event)],
        "process": [event for event in related if _is_suspicious_web_process_event(event)],
        "file": [event for event in related if _is_suspicious_file_write(event)],
        "egress": [event for event in related if _is_egress_event(event)],
    }


def _waf_evidence_events(alert: JsonDict, groups: dict[str, list[JsonDict]]) -> list[JsonDict]:
    return [alert, *groups["http"][:3], *groups["process"][:3], *groups["file"][:3], *groups["egress"][:3]]


def _waf_confidence(groups: dict[str, list[JsonDict]]) -> float:
    return min(
        0.54
        + (0.09 if groups["http"] else 0.0)
        + (0.15 if groups["process"] else 0.0)
        + (0.12 if groups["file"] else 0.0)
        + (0.1 if groups["egress"] else 0.0),
        0.88,
    )


def _waf_gaps(groups: dict[str, list[JsonDict]]) -> list[str]:
    gaps: list[str] = []
    if not groups["process"]:
        gaps.append("No EDR process event tied to the web service was observed in the window.")
    if not groups["file"]:
        gaps.append("No server-side file write evidence was observed in the window.")
    if not groups["egress"]:
        gaps.append("No outbound network evidence from the victim asset was observed in the window.")
    return gaps


def _waf_finding(alert: JsonDict, evidence_events: list[JsonDict], groups: dict[str, list[JsonDict]]) -> Any:
    confidence = _waf_confidence(groups)
    return _make_finding(
        "waf_attack_success_candidate",
        evidence_events,
        params=MakeFindingParams(
            detector_id="waf_attack_success_candidate",
            evidence_events=evidence_events,
            hypothesis="Possible web attack success: WAF/web alert was followed by server-side success indicators.",
            confidence=confidence,
            gaps=_waf_gaps(groups),
            next_queries=[
                _query("Trace web access/error logs around the WAF alert", alert),
                _query("Trace EDR process tree for the victim asset around the alert", alert),
                _query("Hunt outbound DNS/proxy/NetFlow from the victim asset after the alert", alert),
            ],
            features={
                "http_success_or_error_events": len(groups["http"]),
                "process_anomaly_events": len(groups["process"]),
                "file_write_events": len(groups["file"]),
                "egress_events": len(groups["egress"]),
            },
            severity_hint="high" if confidence >= 0.7 else "medium",
            extra_entities={"attacker_ip": [_source_ip(alert)], "victim_ip": [_victim_ip(alert)], "entry_uri": [_text(_field(alert, "uri", "api", "url", "http.url"))]},
        ),
    )


def _web_process_finding(event: JsonDict) -> Any:
    parent = _parent_process_name(event)
    child = _process_name(event)
    cmdline = _cmdline(event)
    confidence = 0.76
    if any(token in cmdline.lower() for token in (" -c ", "/c ", "download", "http://", "https://", "base64")):
        confidence += 0.06
    if _severity(event) in {"high", "critical"}:
        confidence += 0.04
    return _make_finding(
        "web_to_process_anomaly",
        [event],
        params=MakeFindingParams(
            detector_id="web_to_process_anomaly",
            evidence_events=[event],
            hypothesis=f"Possible post-exploit command execution: web parent {parent or 'unknown'} launched {child or 'unknown'}.",
            confidence=min(confidence, 0.9),
            gaps=["The initiating HTTP request is not confirmed unless web access/WAF evidence is linked.", "Process GUID or full process tree may be needed to prove execution lineage."],
            next_queries=[_query("Find web requests to this host in the 10 minutes before the process event", event), _query("Expand child process tree and file/network activity for this process", event), _query("Search for matching command line on peer web servers", event)],
            features={"parent_process": parent, "child_process": child, "cmdline": cmdline[:300]},
            severity_hint="high",
            extra_entities={"victim_ip": [_asset_ip(event)], "process": [child], "parent_process": [parent], "host": [_host(event)]},
        ),
    )


def _rare_egress_candidates(alert: JsonDict, egress_events: list[JsonDict], baseline_obj: Any, window_minutes: int) -> list[JsonDict]:
    candidates: list[JsonDict] = []
    for event in egress_events:
        if not (_within_after(alert, event, window_minutes) and _same_asset(alert, event)):
            continue
        asset = _primary_asset(alert) or _primary_asset(event)
        rare_destination = bool(_destination(event)) and baseline_obj.is_rare_egress_destination(asset, _destination(event))
        rare_port = baseline_obj.is_rare_egress_port(asset, _dst_port(event))
        if rare_destination or rare_port or _severity(alert) in {"high", "critical"}:
            candidates.append(event)
    return candidates


def _rare_egress_finding(alert: JsonDict, candidates: list[JsonDict]) -> Any:
    confidence = min(0.62 + (0.08 if _severity(alert) in {"high", "critical"} else 0.0) + min(len(candidates), 4) * 0.035, 0.86)
    return _make_finding(
        "rare_egress_after_alert",
        [alert, *candidates[:6]],
        params=MakeFindingParams(
            detector_id="rare_egress_after_alert",
            evidence_events=[alert, *candidates[:6]],
            hypothesis="Possible post-alert command-and-control or exfiltration: alerted asset made rare outbound connections.",
            confidence=confidence,
            gaps=["Outbound process owner is missing unless EDR network telemetry is linked.", "DNS/proxy and byte-count context are needed to distinguish callback from benign update traffic."],
            next_queries=[_query("Trace DNS, proxy, and NetFlow rows for the rare destination", candidates[0]), _query("Find the local process that opened the outbound connection", candidates[0]), _query("Search the rare destination across other assets for lateral spread", candidates[0])],
            features={"rare_egress_events": len(candidates), "destinations": _unique_texts(_destination(e) for e in candidates), "alert_severity": _severity(alert)},
            severity_hint="high",
            extra_entities={"attacker_ip": [_source_ip(alert)], "victim_ip": [_victim_ip(alert), _source_ip(candidates[0])], "dst_ip": [_destination_ip(candidates[0])], "domain": [_domain(candidates[0])]},
        ),
    )


__all__ = ["rare_egress_after_alert", "waf_attack_success_candidate", "web_to_process_anomaly"]
