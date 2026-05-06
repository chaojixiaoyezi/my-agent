"""LLM: Event classification predicates and weak-signal aggregation.

This module provides:
  - Event classification predicates (is_waf_event, is_auth_event, etc.)
  - Entity comparison functions are re-exported from classifier_entities.py.
  - Weak-signal detection helpers
  - Entity extraction and normalization utilities
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from typing import Any

from .classifier_entities import (
    _asset_candidates,
    _destination,
    _entities_from_events,
    _first_entity,
    _gap_details,
    _normalize_entities,
    _primary_asset,
    _same_asset,
    _same_auth_scope,
    _same_source,
    _same_user,
    _unique_json_values,
    _unique_texts,
)
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
