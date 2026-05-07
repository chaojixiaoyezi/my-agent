# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

"""Entity, uniqueness, and gap-detail helpers for detector classifiers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .field_access import JsonDict, _field, _text
from .field_extractors import (
    _destination_ip,
    _domain,
    _host,
    _parent_process_name,
    _process_name,
    _source_ip,
    _source_product,
    _user,
    _victim_ip,
)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _same_auth_scope 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 same auth scope 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
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


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _same_user 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 same user 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _same_user(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Return True if left and right have the same username."""
    left_user = _user(left)
    return bool(left_user and left_user == _user(right))


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _same_source 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 same source 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _same_source(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Return True if left and right have the same source IP."""
    left_src = _source_ip(left)
    return bool(left_src and left_src == _source_ip(right))


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _same_asset 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 same asset 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _same_asset(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Return True if left and right share any asset candidate."""
    left_assets = set(_asset_candidates(left))
    right_assets = set(_asset_candidates(right))
    return bool(left_assets and right_assets and left_assets.intersection(right_assets))


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _asset_candidates 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 asset candidates 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _asset_candidates(event: Mapping[str, Any]) -> list[str]:
    """Return deduplicated asset identifiers for event."""
    from .classifiers import _is_egress_event

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


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _primary_asset 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 primary asset 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _primary_asset(event: Mapping[str, Any]) -> str:
    """Return the first asset candidate for event."""
    candidates = _asset_candidates(event)
    return candidates[0] if candidates else ""


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _destination 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 destination 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _destination(event: Mapping[str, Any]) -> str:
    """Return the destination IP or domain from event."""
    return _destination_ip(event) or _domain(event)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _entities_from_events 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 entities from events 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _entities_from_events(events: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Build an entity dict from a sequence of events."""
    entities: dict[str, list[str]] = {}
    for event in events:
        _extend_event_entities(entities, event)
    return _normalize_entities(entities)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _extend_event_entities 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 extend event entities 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _extend_event_entities(entities: dict[str, list[str]], event: Mapping[str, Any]) -> None:
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


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _normalize_entities 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 normalize entities 涉及的字段，让后续匹配和存储使用同一形态。
def _normalize_entities(entities: Mapping[str, Sequence[Any]]) -> dict[str, list[str]]:
    """Deduplicate and sort entity values."""
    normalized: dict[str, list[str]] = {}
    for key, values in entities.items():
        clean_key = _text(key)
        if not clean_key:
            continue
        normalized[clean_key] = _unique_texts(_text(value) for value in values if _text(value))
    return {key: values for key, values in sorted(normalized.items()) if values}


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _unique_texts 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 unique texts 涉及的字段，让后续匹配和存储使用同一形态。
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


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _unique_json_values 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 unique json values 涉及的字段，让后续匹配和存储使用同一形态。
def _unique_json_values(values: Sequence[Any]) -> list[Any]:
    """Return deduplicated values using JSON serialisation as the key."""
    import json

    from ...models import QueryPlan

    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        item = _json_value(value)
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        if marker in seen or item in ("", None, [], {}):
            continue
        seen.add(marker)
        result.append(item)
    return result


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _json_value 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 json value 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _json_value(value: Any) -> Any:
    from ...models import QueryPlan

    if isinstance(value, QueryPlan):
        return value.to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    return _text(value)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _gap_details 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 gap details 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _gap_details(
    gaps: Sequence[str],
    window: Sequence[str],
    entities: Mapping[str, Sequence[Any]],
    evidence_events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build structured gap detail dicts from textual gap strings."""
    products = _unique_texts(_source_product(event) for event in evidence_events if _source_product(event))
    primary_entity = _first_entity(entities)
    return [_build_gap_detail(gap, products, window, primary_entity) for gap in gaps]


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _build_gap_detail 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 build gap detail 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _build_gap_detail(
    gap: str,
    products: Sequence[str],
    window: Sequence[str],
    primary_entity: dict[str, str],
) -> dict[str, Any]:
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


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _apply_gap_telemetry 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 apply gap telemetry 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _apply_gap_telemetry(detail: dict[str, Any], text: str) -> None:
    """Apply telemetry or field classification to a gap detail based on text content."""
    lower = text.lower()
    _set_by_keywords(
        detail,
        lower,
        (
            (("edr", "process", "host"), "missing_telemetry", "edr_process"),
            (("outbound", "dns", "proxy", "netflow", "network"), "missing_telemetry", "network_egress"),
            (("file",), "missing_telemetry", "file_activity"),
            (("mfa",), "missing_telemetry", "identity_mfa"),
            (("geoip", "country"), "missing_field", "country"),
            (("asn",), "missing_field", "asn"),
        ),
    )


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _set_by_keywords 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 set by keywords 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _set_by_keywords(detail: dict[str, Any], text: str, rules: tuple[tuple[tuple[str, ...], str, str], ...]) -> None:
    """Set detail fields based on keyword match rules."""
    for keywords, key, value in rules:
        if any(kw in text for kw in keywords):
            detail[key] = value
            return


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _first_entity 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 first entity 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _first_entity(entities: Mapping[str, Sequence[Any]]) -> dict[str, str]:
    """Return the first non-empty entity from entities."""
    for key in ("victim_ip", "host", "user", "attacker_ip", "src_ip", "dst_ip"):
        values = entities.get(key)
        if values:
            return {"field": key, "value": _text(values[0])}
    return {}


__all__ = [
    "_asset_candidates",
    "_destination",
    "_entities_from_events",
    "_first_entity",
    "_gap_details",
    "_normalize_entities",
    "_primary_asset",
    "_same_asset",
    "_same_auth_scope",
    "_same_source",
    "_same_user",
    "_unique_json_values",
    "_unique_texts",
]
