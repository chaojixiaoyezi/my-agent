from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

DEFAULT_PAYLOAD_MAX_CHARS = 512

SECURITY_ALERT_V1_BASE_KEYS: tuple[str, ...] = (
    "alert_id",
    "event_time",
    "source_id",
    "source_product",
)

SECURITY_ALERT_V1_ALERT_KEYS: tuple[str, ...] = (
    "alert_type",
    "threat_name",
    "ioc_or_rule_id",
    "uri",
    "xff_proxy",
    "payload",
    "domain",
    "referer",
    "dst_port",
    "protocol",
    "victim_asset_group",
    "attacker_asset_group",
    "victim_ip",
    "attacker_ip",
    "src_ip",
    "dst_ip",
    "detection_location",
    "detection_field",
    "match_operator",
    "matched_value",
    "device_serial_number",
    "alert_rule",
    "api",
    "api_threat_type",
    "owasp_type",
)

SECURITY_ALERT_V1_EXTRA_KEYS: tuple[str, ...] = (
    "user",
    "host",
    "process_name",
    "process_cmdline",
    "file_hash",
    "login_result",
    "geo",
)

SECURITY_ALERT_V1_KEYS: tuple[str, ...] = (
    *SECURITY_ALERT_V1_BASE_KEYS,
    *SECURITY_ALERT_V1_ALERT_KEYS,
    *SECURITY_ALERT_V1_EXTRA_KEYS,
)

CHINESE_SECURITY_ALERT_FIELD_MAP: dict[str, str] = {
    "告警类型": "alert_type",
    "威胁名称": "threat_name",
    "IOC/规则ID": "ioc_or_rule_id",
    "URI": "uri",
    "XFF代理": "xff_proxy",
    "Payload": "payload",
    "域名": "domain",
    "referer": "referer",
    "目的端口": "dst_port",
    "协议": "protocol",
    "受害资产组": "victim_asset_group",
    "攻击资产组": "attacker_asset_group",
    "受害IP": "victim_ip",
    "攻击IP": "attacker_ip",
    "源IP": "src_ip",
    "目的IP": "dst_ip",
    "检测位置": "detection_location",
    "检测字段": "detection_field",
    "匹配": "match_operator",
    "值": "matched_value",
    "设备序列号": "device_serial_number",
    "告警规则": "alert_rule",
    "API": "api",
    "API威胁类型": "api_threat_type",
    "OWASP类型": "owasp_type",
}

EXTRA_FIELD_ALIASES: dict[str, str] = {
    "告警ID": "alert_id",
    "告警编号": "alert_id",
    "事件ID": "alert_id",
    "事件时间": "event_time",
    "告警时间": "event_time",
    "发生时间": "event_time",
    "时间": "event_time",
    "数据源": "source_id",
    "来源": "source_id",
    "来源ID": "source_id",
    "源ID": "source_id",
    "设备ID": "source_id",
    "产品": "source_product",
    "源产品": "source_product",
    "设备类型": "source_product",
    "安全产品": "source_product",
    "用户": "user",
    "账号": "user",
    "用户名": "user",
    "主机": "host",
    "主机名": "host",
    "受害主机": "host",
    "进程": "process_name",
    "进程名": "process_name",
    "命令行": "process_cmdline",
    "进程命令行": "process_cmdline",
    "文件Hash": "file_hash",
    "文件哈希": "file_hash",
    "登录结果": "login_result",
    "结果": "login_result",
    "地理位置": "geo",
    "国家": "geo",
}

ENGLISH_ALIASES: dict[str, str] = {
    "timestamp": "event_time",
    "@timestamp": "event_time",
    "time": "event_time",
    "source": "source_id",
    "sourceid": "source_id",
    "product": "source_product",
    "vendor_product": "source_product",
    "rule_id": "ioc_or_rule_id",
    "signature_id": "ioc_or_rule_id",
    "signature": "ioc_or_rule_id",
    "xff": "xff_proxy",
    "x_forwarded_for": "xff_proxy",
    "x-forwarded-for": "xff_proxy",
    "referrer": "referer",
    "dest_port": "dst_port",
    "destination_port": "dst_port",
    "dest_ip": "dst_ip",
    "destination_ip": "dst_ip",
    "source_ip": "src_ip",
    "client_ip": "src_ip",
    "rule_name": "alert_rule",
    "process": "process_name",
    "cmdline": "process_cmdline",
    "command_line": "process_cmdline",
    "hash": "file_hash",
    "result": "login_result",
    "country": "geo",
}


def _build_field_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for stable_key in SECURITY_ALERT_V1_KEYS:
        aliases[stable_key] = stable_key
        aliases[_header_token(stable_key)] = stable_key
    for raw_key, stable_key in CHINESE_SECURITY_ALERT_FIELD_MAP.items():
        aliases[raw_key] = stable_key
        aliases[_header_token(raw_key)] = stable_key
    for raw_key, stable_key in EXTRA_FIELD_ALIASES.items():
        aliases[raw_key] = stable_key
        aliases[_header_token(raw_key)] = stable_key
    for raw_key, stable_key in ENGLISH_ALIASES.items():
        aliases[raw_key] = stable_key
        aliases[_header_token(raw_key)] = stable_key
    return aliases


def normalize_security_alert_v1(
    record: Mapping[str, Any],
    *,
    raw_ref: str,
    source_id: str | None = None,
    source_product: str | None = None,
    line_no: int | None = None,
    payload_max_chars: int = DEFAULT_PAYLOAD_MAX_CHARS,
    ingest_time: str | None = None,
) -> dict[str, Any]:
    """Normalize one SecurityAlertV1 mapping while preserving raw fields."""

    raw_fields = _clean_raw_fields(record)
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

    now = ingest_time or utc_now()
    event: dict[str, Any] = dict.fromkeys(SECURITY_ALERT_V1_KEYS)
    event.update(mapped)

    event["source_id"] = clean_value(event.get("source_id")) or source_id or "unknown"
    event["source_product"] = (
        clean_value(event.get("source_product"))
        or source_product
        or infer_source_product(event, raw_fields)
    )
    event["event_time"] = normalize_timestamp(clean_value(event.get("event_time")) or now)
    event["ingest_time"] = now
    event["security_schema"] = "SecurityAlertV1"
    event["schema_version"] = "1"
    event["event_type"] = "ids_alert"
    event["event_class"] = "alert"
    event["event_action"] = "detected"
    event["raw_ref"] = raw_ref
    event["raw_line_no"] = line_no
    event["parser_id"] = "security_alert_v1"

    _copy_security_ip_semantics(event)
    _coerce_port_fields(event)
    _apply_payload_policy(event, payload_max_chars=payload_max_chars)

    event["raw_fields"] = raw_fields
    event["attributes"] = attributes
    event["mapping_source"] = mapping_source
    event["parser_confidence"] = parser_confidence(raw_fields, mapping_source)

    dedup_hash = event_fingerprint(event)
    event["dedup_key"] = f"sha256:{dedup_hash}"
    event["event_id"] = f"evt-{dedup_hash[:24]}"
    return event


def stable_field_key(raw_key: str) -> str:
    key = _strip_key(raw_key)
    return FIELD_ALIASES.get(key) or FIELD_ALIASES.get(_header_token(key)) or _to_snake(key)


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
    return key.strip().lstrip("\ufeff")


def _header_token(key: str) -> str:
    stripped = _strip_key(key).lower()
    return re.sub(r"[\s_\-./]+", "", stripped)


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


FIELD_ALIASES = _build_field_aliases()
