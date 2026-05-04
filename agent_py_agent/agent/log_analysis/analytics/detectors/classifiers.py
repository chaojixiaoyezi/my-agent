"""LLM: Event classification predicates, entity comparison helpers, weak-signal aggregation, and entity extraction.

This module provides:
  - Event classification predicates (is_waf_event, is_auth_event, etc.)
  - Entity comparison functions (same_user, same_asset, etc.)
  - Weak-signal detection helpers
  - Entity extraction and normalization utilities
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from typing import Any

from .field_access import (
    SUSPICIOUS_CHILD_PROCESSES,
    WEB_PARENT_PROCESSES,
    JsonDict,
    _event_time,
    _field,
    _text,
    _truthy,
)
from .field_extractors import (
    _destination_ip,
    _domain,
    _event_action,
    _event_class,
    _host,
    _outcome,
    _parent_process_name,
    _process_name,
    _severity,
    _source_ip,
    _source_product,
    _user,
    _victim_ip,
)

# ---------------------------------------------------------------------------
# Event classification predicates
# ---------------------------------------------------------------------------

def _is_alert_event(event: Mapping[str, Any]) -> bool:
    """Return True if event is an alert or high-severity event."""
    if _event_class(event) == "alert":
        return True
    if _truthy(_field(event, "alert_type", "threat_name", "alert_rule", "ioc_or_rule_id")):
        return True
    return _severity(event) in {"high", "critical"}


def _is_waf_event(event: Mapping[str, Any]) -> bool:
    """Return True if event is a WAF / web-injection alert."""
    product = _source_product(event)
    alert_text = " ".join(
        _text(_field(event, name))
        for name in ("alert_type", "threat_name", "alert_rule", "api_threat_type", "owasp_type")
    ).lower()
    has_web_fields = bool(_field(event, "uri", "api", "url", "payload", "http.url"))
    return "waf" in product or ("web" in alert_text and has_web_fields) or ("injection" in alert_text and has_web_fields)


def _is_http_success_or_error(event: Mapping[str, Any]) -> bool:
    """Return True if event has an HTTP 2xx or 5xx status code."""
    status = _to_int(_field(event, "status_code", "http_status", "http.status_code", "response_status"))
    if status is None:
        return False
    return 200 <= status < 300 or status >= 500


def _is_suspicious_web_process_event(event: Mapping[str, Any]) -> bool:
    """Return True if event shows a web parent spawning a suspicious child."""
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
    """Return True if event is a file write to a web-accessible path."""
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
    """Return True if event is an outbound network/egress event."""
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
    """Return True if value is a private/link-local IP address."""
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
    """Return True if event is a VPN login/session event."""
    product = _source_product(event)
    return "vpn" in product or ("vpn" in _event_action(event) and _is_auth_event(event))


def _is_auth_event(event: Mapping[str, Any]) -> bool:
    """Return True if event is an authentication/identity event."""
    event_class = _event_class(event)
    action = _event_action(event)
    product = _source_product(event)
    return event_class in {"auth", "authentication", "identity"} or "login" in action or "auth" in action or "vpn" in product


def _is_success(event: Mapping[str, Any]) -> bool:
    """Return True if event outcome indicates success."""
    return _outcome(event) in {"success", "succeeded", "successful", "allowed", "ok", "accepted", "pass"}


def _is_failure(event: Mapping[str, Any]) -> bool:
    """Return True if event outcome indicates failure."""
    return _outcome(event) in {"failure", "failed", "fail", "denied", "blocked", "rejected", "invalid"}


# ---------------------------------------------------------------------------
# Helper functions for text/int conversion
# ---------------------------------------------------------------------------

def _to_int(value) -> int | None:
    """Convert value to int or return None."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Entity comparison helpers
# ---------------------------------------------------------------------------

def _same_auth_scope(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Return True if left and right share user+IP, user, or IP scope."""
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
    """Return True if left and right have the same username."""
    left_user = _user(left)
    return bool(left_user and left_user == _user(right))


def _same_source(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Return True if left and right have the same source IP."""
    left_src = _source_ip(left)
    return bool(left_src and left_src == _source_ip(right))


def _same_asset(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Return True if left and right share any asset candidate."""
    left_assets = set(_asset_candidates(left))
    right_assets = set(_asset_candidates(right))
    return bool(left_assets and right_assets and left_assets.intersection(right_assets))


def _asset_candidates(event: Mapping[str, Any]) -> list[str]:
    """Return deduplicated asset identifiers for event."""
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
    """Return the first asset candidate for event."""
    candidates = _asset_candidates(event)
    return candidates[0] if candidates else ""


def _destination(event: Mapping[str, Any]) -> str:
    """Return the destination IP or domain from event."""
    return _destination_ip(event) or _domain(event)


# ---------------------------------------------------------------------------
# Weak-signal & entity helpers
# ---------------------------------------------------------------------------

def _weak_signal(event: JsonDict) -> JsonDict | None:
    """Classify event as a weak signal and return its metadata, or None."""
    signal_type = _classify_weak_signal(event)
    if not signal_type:
        return None
    return {"signal_type": signal_type, "source_product": _source_product(event), "event": event}


def _classify_weak_signal(event: JsonDict) -> str:
    """Return signal type string for a weak signal event, or empty string."""
    if _is_waf_event(event):
        return "waf_web_alert"
    if _is_vpn_event(event) and _is_success(event) and _truthy(_field(event, "new_geo", "new_asn", "new_device", "unusual_hour")):
        return "vpn_novel_login"
    if _is_auth_event(event) and _is_failure(event):
        return "auth_failure"
    if _is_suspicious_web_process_event(event):
        return "web_process"
    if _is_suspicious_file_write(event):
        return "file_write"
    if _is_egress_event(event) and (_truthy(_field(event, "rare", "is_rare", "new_dst", "new_destination")) or _destination_ip(event)):
        return "egress"
    if _is_alert_event(event) and _severity(event) in {"low", "medium", "high", "critical"}:
        return "alert"
    return ""


def _entities_from_events(events: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Build an entity dict from a sequence of events."""
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
    """Deduplicate and sort entity values."""
    normalized: dict[str, list[str]] = {}
    for key, values in entities.items():
        clean_key = _text(key)
        if not clean_key:
            continue
        normalized[clean_key] = _unique_texts(_text(value) for value in values if _text(value))
    return {key: values for key, values in sorted(normalized.items()) if values}


def _unique_texts(values: Sequence[Any] | Any) -> list[str]:
    """Return deduplicated, order-preserving text values."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _unique_json_values(values: Sequence[Any]) -> list[Any]:
    """Return deduplicated values using JSON serialisation as the key."""
    import json

    from ...models import QueryPlan

    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, QueryPlan):
            item: Any = value.to_dict()
        elif isinstance(value, Mapping):
            item = dict(value)
        else:
            item = _text(value)
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        if marker in seen or item in ("", None, [], {}):
            continue
        seen.add(marker)
        result.append(item)
    return result


def _gap_details(
    gaps: Sequence[str],
    window: Sequence[str],
    entities: Mapping[str, Sequence[Any]],
    evidence_events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build structured gap detail dicts from textual gap strings."""
    products = _unique_texts(_source_product(event) for event in evidence_events if _source_product(event))
    primary_entity = _first_entity(entities)
    details: list[dict[str, Any]] = []
    for gap in gaps:
        detail = _build_gap_detail(gap, products, window, primary_entity)
        details.append(detail)
    return details


def _build_gap_detail(gap: str, products: Sequence[str], window: Sequence[str], primary_entity: dict[str, str]) -> dict[str, Any]:
    """Build one gap detail dict."""
    text = _text(gap)
    detail: dict[str, Any] = {
        "description": text,
        "source_products": list(products),
        "time_window": list(window),
        "entity": primary_entity,
    }
    _apply_gap_telemetry(detail, text)
    return detail


def _apply_gap_telemetry(detail: dict[str, Any], text: str) -> None:
    """Apply telemetry or field classification to a gap detail based on text content."""
    lower = text.lower()
    _set_by_keywords(detail, lower, (
        (("edr", "process", "host"), "missing_telemetry", "edr_process"),
        (("outbound", "dns", "proxy", "netflow", "network"), "missing_telemetry", "network_egress"),
        (("file",), "missing_telemetry", "file_activity"),
        (("mfa",), "missing_telemetry", "identity_mfa"),
        (("geoip", "country"), "missing_field", "country"),
        (("asn",), "missing_field", "asn"),
    ))


def _set_by_keywords(detail: dict[str, Any], text: str, rules: tuple[tuple[tuple[str, ...], str, str], ...]) -> None:
    """Set detail fields based on keyword match rules."""
    for keywords, key, value in rules:
        if any(kw in text for kw in keywords):
            detail[key] = value
            return


def _first_entity(entities: Mapping[str, Sequence[Any]]) -> dict[str, str]:
    """Return the first non-empty entity from entities."""
    for key in ("victim_ip", "host", "user", "attacker_ip", "src_ip", "dst_ip"):
        values = entities.get(key)
        if values:
            return {"field": key, "value": _text(values[0])}
    return {}