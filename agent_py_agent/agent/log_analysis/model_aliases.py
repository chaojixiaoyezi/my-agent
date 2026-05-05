from __future__ import annotations

"""Field aliases shared by log-analysis data models."""

SECURITY_ALERT_V1_FIELD_ALIASES: dict[str, str] = {
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


__all__ = ["SECURITY_ALERT_V1_FIELD_ALIASES"]
