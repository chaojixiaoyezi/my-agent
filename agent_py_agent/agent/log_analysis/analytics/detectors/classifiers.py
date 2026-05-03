"""LLM: Event classification predicates, entity comparison, and weak-signal helpers.

给人看的解释：
本模块提供事件分类函数（判断是否为 WAF/VPN/认证/告警等）、
实体比较函数（判断两个事件是否涉及同一用户/IP/资产）、
以及弱信号提取和实体聚合等高级分析工具。
依赖 field_access.py 和 field_extractors.py 中的基础函数。
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from typing import Any

from .field_access import (
    SUSPICIOUS_CHILD_PROCESSES,
    WEB_PARENT_PROCESSES,
    JsonDict,
    _field,
    _present,
    _text,
    _to_int,
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
    """LLM: Return True if *event* is an alert or high-severity event.

    新手说明:
    判断事件是否为告警事件（类别是 alert，或有告警字段，或严重级别为 high/critical）。
    """
    if _event_class(event) == "alert":
        return True
    if _present(_field(event, "alert_type", "threat_name", "alert_rule", "ioc_or_rule_id")):
        return True
    return _severity(event) in {"high", "critical"}


def _is_waf_event(event: Mapping[str, Any]) -> bool:
    """LLM: Return True if *event* is a WAF / web-injection alert.

    新手说明:
    判断事件是否为 WAF（Web 应用防火墙）告警。
    """
    product = _source_product(event)
    alert_text = " ".join(
        _text(_field(event, name))
        for name in ("alert_type", "threat_name", "alert_rule", "api_threat_type", "owasp_type")
    ).lower()
    has_web_fields = _present(_field(event, "uri", "api", "url", "payload", "http.url"))
    return "waf" in product or ("web" in alert_text and has_web_fields) or ("injection" in alert_text and has_web_fields)


def _is_http_success_or_error(event: Mapping[str, Any]) -> bool:
    """LLM: Return True if *event* has an HTTP 2xx or 5xx status code.

    新手说明:
    判断事件是否为 HTTP 成功（2xx）或服务端错误（5xx）。
    """
    status = _to_int(_field(event, "status_code", "http_status", "http.status_code", "response_status"))
    if status is None:
        return False
    return 200 <= status < 300 or status >= 500


def _is_suspicious_web_process_event(event: Mapping[str, Any]) -> bool:
    """LLM: Return True if *event* shows a web parent spawning a suspicious child.

    新手说明:
    判断是否为 Web 服务进程产生了可疑子进程（如 nginx 启动 bash）。
    """
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
    """LLM: Return True if *event* is a file write to a web-accessible path.

    新手说明:
    判断是否为向 Web 目录写入了可疑文件（如写入 .php/.jsp 到 /var/www）。
    """
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
    """LLM: Return True if *event* is an outbound network/egress event.

    新手说明:
    判断是否为出站网络事件（非内网目标的外连）。
    """
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
    """LLM: Return True if *value* is a private/link-local IP address.

    新手说明:
    判断 IP 是否为内网地址（10.x/172.16.x/192.168.x 等）。
    """
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
    """LLM: Return True if *event* is a VPN login/session event.

    新手说明:
    判断是否为 VPN 事件。
    """
    product = _source_product(event)
    return "vpn" in product or ("vpn" in _event_action(event) and _is_auth_event(event))


def _is_auth_event(event: Mapping[str, Any]) -> bool:
    """LLM: Return True if *event* is an authentication/identity event.

    新手说明:
    判断是否为认证事件（登录、鉴权等）。
    """
    event_class = _event_class(event)
    action = _event_action(event)
    product = _source_product(event)
    return event_class in {"auth", "authentication", "identity"} or "login" in action or "auth" in action or "vpn" in product


def _is_success(event: Mapping[str, Any]) -> bool:
    """LLM: Return True if *event* outcome indicates success.

    新手说明:
    判断事件结果是否为"成功"。
    """
    return _outcome(event) in {"success", "succeeded", "successful", "allowed", "ok", "accepted", "pass"}


def _is_failure(event: Mapping[str, Any]) -> bool:
    """LLM: Return True if *event* outcome indicates failure.

    新手说明:
    判断事件结果是否为"失败"。
    """
    return _outcome(event) in {"failure", "failed", "fail", "denied", "blocked", "rejected", "invalid"}


# ---------------------------------------------------------------------------
# Entity comparison helpers
# ---------------------------------------------------------------------------

def _same_auth_scope(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """LLM: Return True if *left* and *right* share user+IP, user, or IP scope.

    新手说明:
    判断两个事件是否属于同一认证范围（同用户+同IP，或至少同用户/同IP）。
    """
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
    """LLM: Return True if *left* and *right* have the same username.

    新手说明:
    判断两个事件是否涉及同一用户。
    """
    left_user = _user(left)
    return bool(left_user and left_user == _user(right))


def _same_source(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """LLM: Return True if *left* and *right* have the same source IP.

    新手说明:
    判断两个事件是否来自同一源 IP。
    """
    left_src = _source_ip(left)
    return bool(left_src and left_src == _source_ip(right))


def _same_asset(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """LLM: Return True if *left* and *right* share any asset candidate.

    新手说明:
    判断两个事件是否涉及同一资产（IP 或主机名有交集）。
    """
    left_assets = set(_asset_candidates(left))
    right_assets = set(_asset_candidates(right))
    return bool(left_assets and right_assets and left_assets.intersection(right_assets))


def _asset_candidates(event: Mapping[str, Any]) -> list[str]:
    """LLM: Return deduplicated asset identifiers for *event*.

    新手说明:
    从事件中收集所有可能的资产标识（IP、主机名等），去重后返回。
    """
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
    """LLM: Return the first asset candidate for *event*.

    新手说明:
    返回事件的首选资产标识。
    """
    candidates = _asset_candidates(event)
    return candidates[0] if candidates else ""


def _destination(event: Mapping[str, Any]) -> str:
    """LLM: Return the destination IP or domain from *event*.

    新手说明:
    获取事件的目标地址（优先 IP，其次域名）。
    """
    return _destination_ip(event) or _domain(event)


# ---------------------------------------------------------------------------
# Weak-signal & entity helpers
# ---------------------------------------------------------------------------

def _weak_signal(event: JsonDict) -> JsonDict | None:
    """LLM: Classify *event* as a weak signal and return its metadata, or None.

    新手说明:
    判断事件是否属于"弱信号"（WAF告警、VPN新登录、认证失败等），返回信号类型。
    """
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
    """LLM: Build an entity dict from a sequence of events.

    新手说明:
    从一组事件中提取所有实体（IP、用户、域名等），合并成字典。
    """
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
    """LLM: Deduplicate and sort entity values.

    新手说明:
    对实体字典去重、排序，清理空值。
    """
    normalized: dict[str, list[str]] = {}
    for key, values in entities.items():
        clean_key = _text(key)
        if not clean_key:
            continue
        normalized[clean_key] = _unique_texts(_text(value) for value in values if _text(value))
    return {key: values for key, values in sorted(normalized.items()) if values}


def _unique_texts(values: Sequence[Any] | Any) -> list[str]:
    """LLM: Return deduplicated, order-preserving text values.

    新手说明:
    对值列表去重并保留顺序，空值跳过。
    """
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
    """LLM: Return deduplicated values using JSON serialisation as the key.

    新手说明:
    用 JSON 序列化做去重，适用于字典和复杂对象。
    """
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
    """LLM: Build structured gap detail dicts from textual gap strings.

    新手说明:
    把文本形式的"信息缺口"转成结构化字典，标注缺少的遥测类型。
    """
    products = _unique_texts(_source_product(event) for event in evidence_events if _source_product(event))
    primary_entity = _first_entity(entities)
    details: list[dict[str, Any]] = []
    for gap in gaps:
        text = _text(gap)
        detail: dict[str, Any] = {
            "description": text,
            "source_products": products,
            "time_window": list(window),
            "entity": primary_entity,
        }
        lower = text.lower()
        if "edr" in lower or "process" in lower or "host" in lower:
            detail["missing_telemetry"] = "edr_process"
        elif "outbound" in lower or "dns" in lower or "proxy" in lower or "netflow" in lower or "network" in lower:
            detail["missing_telemetry"] = "network_egress"
        elif "file" in lower:
            detail["missing_telemetry"] = "file_activity"
        elif "mfa" in lower:
            detail["missing_telemetry"] = "identity_mfa"
        elif "geoip" in lower or "country" in lower:
            detail["missing_field"] = "country"
        elif "asn" in lower:
            detail["missing_field"] = "asn"
        details.append(detail)
    return details


def _first_entity(entities: Mapping[str, Sequence[Any]]) -> dict[str, str]:
    """LLM: Return the first non-empty entity from *entities*.

    新手说明:
    按优先级返回第一个有值的实体（victim_ip > host > user > ...）。
    """
    for key in ("victim_ip", "host", "user", "attacker_ip", "src_ip", "dst_ip"):
        values = entities.get(key)
        if values:
            return {"field": key, "value": _text(values[0])}
    return {}
