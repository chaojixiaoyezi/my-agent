"""威胁指标的只读富化、原子同步与有界回溯碰撞；不执行网络请求。"""
from __future__ import annotations

import hashlib
import ipaddress
import math
import re
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from ..plugin_host.errors import HostAPIError
from .v13_w9_datasources import w9_get, w9_list, w9_now, w9_page, w9_write


# LLM: 只依据显式类型规范化；非法输入不悄悄变成未命中。
# 函数用途: 为本地、来源和告警实体生成相同的指标键。
def normalize_indicator(value, kind):
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError("无效指标")
    value = value.strip()
    if kind == "ip":
        return str(ipaddress.ip_address(value))
    if kind == "domain":
        value = value.rstrip(".").encode("idna").decode("ascii").lower()
        if len(value) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in value.split(".")):
            raise ValueError("无效域名")
        return value
    if kind in ("sha256", "md5"):
        if not re.fullmatch(r"[0-9a-fA-F]{64}" if kind == "sha256" else r"[0-9a-fA-F]{32}", value):
            raise ValueError("无效摘要")
        return value.lower()
    if kind == "url":
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            raise ValueError("无效地址")
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = "[" + str(ipaddress.ip_address(host)) + "]"
        port = parsed.port
        authority = host + (":" + str(port) if port is not None and (parsed.scheme.lower(), port) not in (("http", 80), ("https", 443)) else "")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("地址不能带凭据")
        return urlunsplit((parsed.scheme.lower(), authority, parsed.path or "/", parsed.query, ""))
    raise ValueError("无效指标类型")


# LLM: 时间比较只接受有时区的结构化时间，不从描述猜测观测时间。
# 函数用途: 校验并解析可空时间戳。
def _timestamp(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("无效时间")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间缺少时区")
    return parsed.timestamp()


# LLM: 来源对象只投影白名单，不允许内部字段或秘密穿过结果合同。
# 函数用途: 校验来源指标并补齐公开指标字段。
def _indicator(row):
    if not isinstance(row, dict):
        raise ValueError("无效来源记录")
    kind, value = row["type"], normalize_indicator(row["value"], row["type"])
    confidence = row.get("confidence")
    if confidence is not None and (type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 100):
        raise ValueError("无效置信度")
    lists = {}
    for key in ("family_tags", "actor_attribution", "sources"):
        values = row.get(key, [])
        if not isinstance(values, list) or len(values) > 50 or any(not isinstance(v, str) or not 1 <= len(v) <= 128 for v in values):
            raise ValueError("无效来源标签")
        lists[key] = sorted(set(values))
    for key in ("first_seen_at", "last_seen_at"):
        _timestamp(row.get(key))
    identifier = "ioc-" + hashlib.sha256((kind + "\0" + value).encode()).hexdigest()
    return {"indicator_id": identifier, "value": value, "type": kind, "confidence": confidence,
        **lists, "first_seen_at": row.get("first_seen_at"), "last_seen_at": row.get("last_seen_at")}


# LLM: 合并依据当前锁内记录，不用锁外快照覆盖并发写入的来源和观测。
# 函数用途: 合并同一规范化键的公开指标。
def _merge(previous, incoming):
    result = {**incoming, "indicator_id": previous["indicator_id"]}
    for key in ("family_tags", "actor_attribution", "sources"):
        result[key] = sorted(set(previous[key]) | set(incoming[key]))[:50]
    scores = [v for v in (previous["confidence"], incoming["confidence"]) if v is not None]
    result["confidence"] = max(scores) if scores else None
    for key, choose in (("first_seen_at", min), ("last_seen_at", max)):
        values = [v for v in (previous[key], incoming[key]) if v is not None]
        result[key] = choose(values, key=_timestamp) if values else None
    return result


# LLM: 可选富化不因未绑定而使本地查询不可用；已绑定失败必须明确报错。
# 函数用途: 从可信宿主读取情报来源绑定。
def _provider(call, required=False):
    from ..gateway_parts.business_api_v13 import v13_provider

    try:
        return v13_provider(call, "threat_intel")
    except HostAPIError as error:
        if required or error.code != "HOST_NOT_READY":
            raise
        return None


# LLM: 仅匹配实体结构字段，绝不按告警文案推导指标类型。
# 函数用途: 判断告警是否含一个规范化指标键。
def _has_indicator(alert, keys):
    for entity in alert.get("entities", []):
        try:
            kind = entity["type"]
            if kind == "file_hash":
                kind = "sha256" if len(entity["value"]) == 64 else "md5" if len(entity["value"]) == 32 else None
            key = (kind, normalize_indicator(entity["value"], kind))
        except (KeyError, TypeError, ValueError, UnicodeError):
            continue
        if key in keys:
            return True
    return False


# LLM: 只读本 owner 集合；过滤后由原存储签名分页，游标绑定查询和内容。
# 函数用途: 按类型、来源、置信度和文本筛选指标。
def list_indicators(call):
    rows = []
    for record in w9_list(call, "threat_indicators"):
        row = _indicator(record)
        row["indicator_id"] = record["indicator_id"]
        p = call.params
        if "type" in p and row["type"] != p["type"]:
            continue
        if "source" in p and p["source"] not in row["sources"]:
            continue
        if "min_confidence" in p and (row["confidence"] is None or row["confidence"] < p["min_confidence"]):
            continue
        if p.get("query", "").casefold() not in row["value"].casefold():
            continue
        rows.append(row)
    return w9_page(call, "threat_indicators", sorted(rows, key=lambda row: row["indicator_id"]))


# LLM: 富化是只读操作，不自动保存来源结果；无来源时明确 partial。
# 函数用途: 返回本地和可选来源的指标以及本 owner 的告警碰撞。
def lookup(call):
    kind = call.params["type"]
    try:
        value = normalize_indicator(call.params["indicator"], kind)
    except (TypeError, ValueError, UnicodeError):
        raise HostAPIError("INVALID_SCHEMA", 400) from None
    matches = []
    for row in w9_list(call, "threat_indicators"):
        normalized = _indicator(row)
        if (normalized["type"], normalized["value"]) == (kind, value):
            normalized["indicator_id"] = row["indicator_id"]
            matches.append(normalized)
    consulted, provider = ["local"], _provider(call)
    if provider is not None:
        try:
            rows = provider.lookup(value, kind)
            if not isinstance(rows, list):
                raise ValueError("无效来源结果")
            for row in rows:
                normalized = _indicator(row)
                if (normalized["type"], normalized["value"]) == (kind, value):
                    matches.append(normalized)
        except (KeyError, TypeError, ValueError, RuntimeError, OSError, UnicodeError):
            raise HostAPIError("HOST_NOT_READY", 503) from None
        consulted.append("threat_intel")
    merged = {}
    for row in matches:
        key = (row["type"], row["value"])
        merged[key] = _merge(merged[key], row) if key in merged else row
    alerts = sorted({row["alert_id"] for row in w9_list(call, "soc_alerts") if _has_indicator(row, {(kind, value)})})
    return {"indicator": value, "type": kind, "found": bool(merged), "matches": list(merged.values())[:50],
        "sightings": {"alert_ids": alerts[:100], "count": len(alerts)}, "sources_consulted": consulted,
        "coverage": "complete" if provider is not None and len(alerts) <= 100 else "partial"}


# LLM: 回溯只读告警；缺失或非法时间不能作为落在 lookback 内的证据。
# 函数用途: 按当前 owner 的回溯策略碰撞新同步指标。
def _retro_matches(call, indicators):
    settings = w9_get(call, "retro_hunt_settings", call.owner_id) or {}
    if settings.get("enabled") is not True:
        return {"enabled": False, "matched_alert_ids": []}
    keys = {(row["type"], row["value"]) for row in indicators if row["type"] in settings["indicator_types"]}
    earliest = call.clock() - settings["lookback_days"] * 86400
    matches = set()
    for row in w9_list(call, "soc_alerts"):
        try:
            observed = _timestamp(row.get("created_at"))
        except (TypeError, ValueError, AttributeError):
            continue
        if observed is not None and earliest <= observed <= call.clock() and _has_indicator(row, keys):
            matches.add(row["alert_id"])
    return {"enabled": True, "matched_alert_ids": sorted(matches)[:settings["max_matches_per_sync"]]}


# LLM: 来源拉取和 upsert 都放在幂等事务回调，重放不再次取数；先校验整页再修改。
# 函数用途: 同步一页真实来源指标并报告受策略约束的回溯命中。
def sync_sources(call):
    provider = _provider(call, required=True)

    # LLM: 合并只读取当前锁内 records；并发同步不会丢失旧来源。
    # 函数用途: 校验来源页并原子更新同一规范键的指标记录。
    def mutate(records):
        try:
            page = provider.fetch(source=call.params.get("source"), cursor=call.params.get("cursor"))
            if not isinstance(page, dict) or not isinstance(page.get("indicators"), list):
                raise ValueError("无效来源页")
            next_cursor = page.get("next_cursor")
            if next_cursor is not None and (not isinstance(next_cursor, str) or not 1 <= len(next_cursor) <= 2048):
                raise ValueError("无效来源游标")
            incoming = [_indicator(row) for row in page["indicators"]]
        except (KeyError, TypeError, ValueError, RuntimeError, OSError, UnicodeError):
            raise HostAPIError("HOST_NOT_READY", 503) from None
        keys = {(row["type"], normalize_indicator(row["value"], row["type"])): key
            for key, row in records.items() if row.get("owner_id") == call.owner_id}
        now = w9_now(call)
        changed = set()
        for row in incoming:
            key = (row["type"], row["value"])
            identifier = keys.get(key, row["indicator_id"])
            previous = records.get(identifier)
            if previous is not None and previous.get("owner_id") != call.owner_id:
                raise HostAPIError("RESOURCE_NOT_FOUND", 404)
            public = _merge(_indicator(previous), row) if previous else row
            public["indicator_id"] = identifier
            records[identifier] = {**public, "owner_id": call.owner_id,
                "revision": (previous or {}).get("revision", 0) + 1,
                "created_at": (previous or {}).get("created_at", now), "updated_at": now}
            keys[key] = identifier
            changed.add(identifier)
        return {"fetched": len(incoming), "upserted": len(changed), "next_cursor": next_cursor,
            "retro_hunt": _retro_matches(call, incoming)}

    return w9_write(call, "threat_indicators", mutate)
