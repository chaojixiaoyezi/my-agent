
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .field_aliases import (
    CHINESE_SECURITY_ALERT_FIELD_MAP,
    FIELD_ALIASES,
    SECURITY_ALERT_V1_KEYS,
    header_token,
    strip_key,
)

DEFAULT_PAYLOAD_MAX_CHARS = 512


@dataclass(frozen=True)
class _NormalizeOptions:
    raw_ref: str
    source_id: str | None
    source_product: str | None
    line_no: int | None
    payload_max_chars: int
    ingest_time: str | None

def normalize_security_alert_v1(
    record: Mapping[str, Any],
    *,
    options: _NormalizeOptions | None = None,
    raw_ref: str = "",
    source_id: str | None = None,
    source_product: str | None = None,
    line_no: int | None = None,
    payload_max_chars: int = DEFAULT_PAYLOAD_MAX_CHARS,
    ingest_time: str | None = None,
) -> dict[str, Any]:
    """Normalize one SecurityAlertV1 mapping while preserving raw fields."""

    normalize_options = options or _NormalizeOptions(
        raw_ref=str(raw_ref),
        source_id=source_id,
        source_product=source_product,
        line_no=line_no,
        payload_max_chars=int(payload_max_chars),
        ingest_time=ingest_time,
    )
    raw_fields = _clean_raw_fields(record)
    mapped, mapping_source, attributes = _map_security_alert_fields(raw_fields)
    event = _base_security_event(mapped, raw_fields, normalize_options)

    _copy_security_ip_semantics(event)
    _coerce_port_fields(event)
    _apply_payload_policy(event, payload_max_chars=normalize_options.payload_max_chars)

    event["raw_fields"] = raw_fields
    event["attributes"] = attributes
    event["mapping_source"] = mapping_source
    event["parser_confidence"] = parser_confidence(raw_fields, mapping_source)

    dedup_hash = event_fingerprint(event)
    event["dedup_key"] = f"sha256:{dedup_hash}"
    event["event_id"] = f"evt-{dedup_hash[:24]}"
    return event


def _map_security_alert_fields(raw_fields: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    mapped: dict[str, Any] = {}
    mapping_source: dict[str, str] = {}
    attributes: dict[str, Any] = {}
    for raw_key, raw_value in raw_fields.items():
        stable_key = stable_field_key(raw_key)
        value = clean_value(raw_value)
        if value is None:
            continue
        if stable_key in SECURITY_ALERT_V1_KEYS:
            mapped[stable_key] = value
            mapping_source[stable_key] = mapping_source_for(raw_key, stable_key)
        else:
            attributes[_to_snake(raw_key)] = value
    return mapped, mapping_source, attributes


def _base_security_event(
    mapped: Mapping[str, Any],
    raw_fields: Mapping[str, Any],
    options: _NormalizeOptions,
) -> dict[str, Any]:
    now = options.ingest_time or utc_now()
    event: dict[str, Any] = dict.fromkeys(SECURITY_ALERT_V1_KEYS)
    event.update(mapped)

    event["source_id"] = clean_value(event.get("source_id")) or options.source_id or "unknown"
    event["source_product"] = (
        clean_value(event.get("source_product"))
        or options.source_product
        or infer_source_product(event, raw_fields)
    )
    event["event_time"] = normalize_timestamp(clean_value(event.get("event_time")) or now)
    event["ingest_time"] = now
    event["security_schema"] = "SecurityAlertV1"
    event["schema_version"] = "1"
    event["event_type"] = "ids_alert"
    event["event_class"] = "alert"
    event["event_action"] = "detected"
    event["raw_ref"] = options.raw_ref
    event["raw_line_no"] = options.line_no
    event["parser_id"] = "security_alert_v1"
    return event

def stable_field_key(raw_key: str) -> str:
    key = _strip_key(raw_key)
    return FIELD_ALIASES.get(key) or FIELD_ALIASES.get(header_token(key)) or _to_snake(key)


def mapping_source_for(raw_key: str, stable_key: str) -> str:
    key = _strip_key(raw_key)
    if CHINESE_SECURITY_ALERT_FIELD_MAP.get(key) == stable_key:
        return "confirmed"
    if key == stable_key:
        return "confirmed"
    return "alias"


def parser_confidence(raw_fields: Mapping[str, Any], mapping_source: Mapping[str, str]) -> float:
    if not raw_fields:
        return 0.0
    mapped_count = len(mapping_source)
    ratio = mapped_count / max(1, len(raw_fields))
    if mapped_count >= 5:
        return min(0.99, 0.86 + ratio * 0.1)
    if mapped_count >= 1:
        return min(0.86, 0.65 + ratio * 0.2)
    return 0.25


def clean_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return value


def normalize_timestamp(value: Any) -> str:
    if value is None:
        return utc_now()
    text = str(value).strip()
    if not text:
        return utc_now()
    parsed = _try_parse_datetime(text)
    if parsed is None:
        return text
    if parsed.tzinfo is None:
        return parsed.isoformat()
    parsed = parsed.astimezone(timezone.utc).replace(microsecond=0)
    return parsed.isoformat().replace("+00:00", "Z")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256_text(payload)


def event_fingerprint(event: Mapping[str, Any]) -> str:
    source_id = event.get("source_id") or "unknown"
    alert_id = event.get("alert_id")
    if alert_id:
        identity = {"source_id": source_id, "alert_id": alert_id}
    else:
        identity = {
            "source_id": source_id,
            "event_time": event.get("event_time"),
            "alert_type": event.get("alert_type"),
            "threat_name": event.get("threat_name"),
            "ioc_or_rule_id": event.get("ioc_or_rule_id"),
            "attacker_ip": event.get("attacker_ip") or event.get("src_ip"),
            "victim_ip": event.get("victim_ip") or event.get("dst_ip"),
            "uri": event.get("uri"),
            "domain": event.get("domain"),
            "dst_port": event.get("dst_port"),
            "protocol": event.get("protocol"),
            "payload_sha256": event.get("payload_sha256"),
            "device_serial_number": event.get("device_serial_number"),
            "alert_rule": event.get("alert_rule"),
        }
    compact = {key: value for key, value in identity.items() if value is not None and value != ""}
    return sha256_json(compact)


def infer_source_product(event: Mapping[str, Any], raw_fields: Mapping[str, Any]) -> str | None:
    haystack = " ".join(
        str(value)
        for value in [
            event.get("source_id"),
            event.get("alert_type"),
            event.get("threat_name"),
            event.get("detection_location"),
            raw_fields.get("source_product"),
            raw_fields.get("产品"),
        ]
        if value
    ).lower()
    for product in ("waf", "edr", "vpn", "hids", "ids", "firewall"):
        if product in haystack:
            return product
    return None


def _apply_payload_policy(event: dict[str, Any], *, payload_max_chars: int) -> None:
    payload = clean_value(event.get("payload"))
    if payload is None:
        event["payload"] = None
        event["payload_sha256"] = None
        event["payload_truncated"] = False
        event["payload_original_size"] = 0
        return

    text = str(payload)
    max_chars = max(0, int(payload_max_chars))
    truncated = len(text) > max_chars
    event["payload"] = text[:max_chars] if truncated else text
    event["payload_sha256"] = f"sha256:{sha256_text(text)}"
    event["payload_truncated"] = truncated
    event["payload_original_size"] = len(text.encode("utf-8"))


def _copy_security_ip_semantics(event: dict[str, Any]) -> None:
    if not event.get("attacker_ip") and event.get("src_ip"):
        event["attacker_ip"] = event["src_ip"]
    if not event.get("src_ip") and event.get("attacker_ip"):
        event["src_ip"] = event["attacker_ip"]
    if not event.get("victim_ip") and event.get("dst_ip"):
        event["victim_ip"] = event["dst_ip"]
    if not event.get("dst_ip") and event.get("victim_ip"):
        event["dst_ip"] = event["victim_ip"]


def _coerce_port_fields(event: dict[str, Any]) -> None:
    for key in ("dst_port",):
        value = clean_value(event.get(key))
        if isinstance(value, int) or value is None:
            event[key] = value
            continue
        try:
            event[key] = int(str(value), 10)
        except ValueError:
            event[key] = value


def _clean_raw_fields(record: Mapping[str, Any]) -> dict[str, Any]:
    raw_fields: dict[str, Any] = {}
    for key, value in record.items():
        if key is None:
            continue
        raw_fields[_strip_key(str(key))] = value
    return raw_fields


def _strip_key(key: str) -> str:
    return strip_key(key)


def _header_token(key: str) -> str:
    return header_token(key)


def _to_snake(key: str) -> str:
    stripped = _strip_key(key)
    token = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", stripped).strip("_")
    if not token:
        return "field"
    if re.search(r"[\u4e00-\u9fff]", token):
        return token
    token = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", token)
    return token.lower()


def _try_parse_datetime(value: str) -> datetime | None:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for candidate in (text, text.replace("/", "-")):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None
