from __future__ import annotations

"""SecurityAlertV1 field alias tables for parser normalization."""

import re

SECURITY_ALERT_V1_BASE_KEYS: tuple[str, ...] = ("alert_id", "event_time", "source_id", "source_product")

SECURITY_ALERT_V1_ALERT_KEYS: tuple[str, ...] = (
    "alert_type", "threat_name", "ioc_or_rule_id", "uri", "xff_proxy", "payload", "domain", "referer",
    "dst_port", "protocol", "victim_asset_group", "attacker_asset_group", "victim_ip", "attacker_ip",
    "src_ip", "dst_ip", "detection_location", "detection_field", "match_operator", "matched_value",
    "device_serial_number", "alert_rule", "api", "api_threat_type", "owasp_type",
)

SECURITY_ALERT_V1_EXTRA_KEYS: tuple[str, ...] = (
    "user", "host", "process_name", "process_cmdline", "file_hash", "login_result", "geo",
)

SECURITY_ALERT_V1_KEYS: tuple[str, ...] = (
    *SECURITY_ALERT_V1_BASE_KEYS,
    *SECURITY_ALERT_V1_ALERT_KEYS,
    *SECURITY_ALERT_V1_EXTRA_KEYS,
)

CHINESE_SECURITY_ALERT_FIELD_MAP: dict[str, str] = {
    "告警类型": "alert_type", "威胁名称": "threat_name", "IOC/规则ID": "ioc_or_rule_id", "URI": "uri",
    "XFF代理": "xff_proxy", "Payload": "payload", "域名": "domain", "referer": "referer", "目的端口": "dst_port",
    "协议": "protocol", "受害资产组": "victim_asset_group", "攻击资产组": "attacker_asset_group", "受害IP": "victim_ip",
    "攻击IP": "attacker_ip", "源IP": "src_ip", "目的IP": "dst_ip", "检测位置": "detection_location", "检测字段": "detection_field",
    "匹配": "match_operator", "值": "matched_value", "设备序列号": "device_serial_number", "告警规则": "alert_rule",
    "API": "api", "API威胁类型": "api_threat_type", "OWASP类型": "owasp_type",
}

EXTRA_FIELD_ALIASES: dict[str, str] = {
    "告警ID": "alert_id", "告警编号": "alert_id", "事件ID": "alert_id", "事件时间": "event_time", "告警时间": "event_time",
    "发生时间": "event_time", "时间": "event_time", "数据源": "source_id", "来源": "source_id", "来源ID": "source_id",
    "源ID": "source_id", "设备ID": "source_id", "产品": "source_product", "源产品": "source_product", "设备类型": "source_product",
    "安全产品": "source_product", "用户": "user", "账号": "user", "用户名": "user", "主机": "host", "主机名": "host",
    "受害主机": "host", "进程": "process_name", "进程名": "process_name", "命令行": "process_cmdline", "进程命令行": "process_cmdline",
    "文件Hash": "file_hash", "文件哈希": "file_hash", "登录结果": "login_result", "结果": "login_result", "地理位置": "geo", "国家": "geo",
}

ENGLISH_ALIASES: dict[str, str] = {
    "timestamp": "event_time", "@timestamp": "event_time", "time": "event_time", "source": "source_id", "sourceid": "source_id",
    "product": "source_product", "vendor_product": "source_product", "rule_id": "ioc_or_rule_id", "signature_id": "ioc_or_rule_id",
    "signature": "ioc_or_rule_id", "xff": "xff_proxy", "x_forwarded_for": "xff_proxy", "x-forwarded-for": "xff_proxy",
    "referrer": "referer", "dest_port": "dst_port", "destination_port": "dst_port", "dest_ip": "dst_ip", "destination_ip": "dst_ip",
    "source_ip": "src_ip", "client_ip": "src_ip", "rule_name": "alert_rule", "process": "process_name", "cmdline": "process_cmdline",
    "command_line": "process_cmdline", "hash": "file_hash", "result": "login_result", "country": "geo",
}


def build_field_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for stable_key in SECURITY_ALERT_V1_KEYS:
        aliases[stable_key] = stable_key
        aliases[header_token(stable_key)] = stable_key
    for mapping in (CHINESE_SECURITY_ALERT_FIELD_MAP, EXTRA_FIELD_ALIASES, ENGLISH_ALIASES):
        for raw_key, stable_key in mapping.items():
            aliases[raw_key] = stable_key
            aliases[header_token(raw_key)] = stable_key
    return aliases


def header_token(key: str) -> str:
    stripped = strip_key(key).lower()
    return re.sub(r"[\s_\-./]+", "", stripped)


def strip_key(key: str) -> str:
    return key.strip().lstrip("\ufeff")


FIELD_ALIASES = build_field_aliases()

__all__ = [
    "CHINESE_SECURITY_ALERT_FIELD_MAP", "FIELD_ALIASES", "SECURITY_ALERT_V1_KEYS", "header_token", "strip_key",
]
