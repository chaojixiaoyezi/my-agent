# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Small in-memory baseline helpers for soft detectors."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _norm 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 norm 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _as_set 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 把 as set 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
def _as_set(values: Any) -> set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        return {_norm(values)} if values.strip() else set()
    if isinstance(values, Iterable):
        return {_norm(item) for item in values if _norm(item)}
    return {_norm(values)}


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 SecurityBaselines 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 SecurityBaselines 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class SecurityBaselines:
    known_countries_by_user: dict[str, set[str]] = field(default_factory=dict)
    known_asns_by_user: dict[str, set[str]] = field(default_factory=dict)
    known_devices_by_user: dict[str, set[str]] = field(default_factory=dict)
    known_login_hours_by_user: dict[str, set[int]] = field(default_factory=dict)
    known_egress_destinations_by_asset: dict[str, set[str]] = field(default_factory=dict)
    known_egress_ports_by_asset: dict[str, set[int]] = field(default_factory=dict)

    # LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 from_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 从外部数据还原 from dict 需要的领域对象，统一缺省值和兼容字段。
    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> SecurityBaselines:
        payload = payload or {}
        return cls(
            known_countries_by_user=_set_map(payload.get("known_countries_by_user")),
            known_asns_by_user=_set_map(payload.get("known_asns_by_user")),
            known_devices_by_user=_set_map(payload.get("known_devices_by_user")),
            known_login_hours_by_user=_int_set_map(payload.get("known_login_hours_by_user")),
            known_egress_destinations_by_asset=_set_map(payload.get("known_egress_destinations_by_asset")),
            known_egress_ports_by_asset=_int_set_map(payload.get("known_egress_ports_by_asset")),
        )

    # LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        return {
            "known_countries_by_user": _sorted_map(self.known_countries_by_user),
            "known_asns_by_user": _sorted_map(self.known_asns_by_user),
            "known_devices_by_user": _sorted_map(self.known_devices_by_user),
            "known_login_hours_by_user": _sorted_int_map(self.known_login_hours_by_user),
            "known_egress_destinations_by_asset": _sorted_map(self.known_egress_destinations_by_asset),
            "known_egress_ports_by_asset": _sorted_int_map(self.known_egress_ports_by_asset),
        }

    # LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 is_new_country 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 is new country 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
    def is_new_country(self, user: Any, country: Any) -> bool:
        return _is_new_value(self.known_countries_by_user, user, country)

    # LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 is_new_asn 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 is new asn 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
    def is_new_asn(self, user: Any, asn: Any) -> bool:
        return _is_new_value(self.known_asns_by_user, user, asn)

    # LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 is_new_device 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 is new device 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
    def is_new_device(self, user: Any, device: Any) -> bool:
        return _is_new_value(self.known_devices_by_user, user, device)

    # LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 is_unusual_login_hour 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 is unusual login hour 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
    def is_unusual_login_hour(self, user: Any, value: datetime | None) -> bool:
        key = _norm(user)
        if not key or value is None or key not in self.known_login_hours_by_user:
            return False
        hours = self.known_login_hours_by_user[key]
        return bool(hours) and value.hour not in hours

    # LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 is_rare_egress_destination 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 is rare egress destination 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
    def is_rare_egress_destination(self, asset: Any, destination: Any) -> bool:
        return _is_new_value(self.known_egress_destinations_by_asset, asset, destination)

    # LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 is_rare_egress_port 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 is rare egress port 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
    def is_rare_egress_port(self, asset: Any, port: Any) -> bool:
        key = _norm(asset)
        if not key or key not in self.known_egress_ports_by_asset:
            return False
        try:
            value = int(port)
        except (TypeError, ValueError):
            return False
        known = self.known_egress_ports_by_asset[key]
        return bool(known) and value not in known


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 ensure_baselines 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 ensure baselines 的输入、状态或路径，提前暴露无效数据和越界条件。
def ensure_baselines(value: SecurityBaselines | dict[str, Any] | None) -> SecurityBaselines:
    if isinstance(value, SecurityBaselines):
        return value
    if isinstance(value, dict):
        return SecurityBaselines.from_dict(value)
    return SecurityBaselines()


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _is_new_value 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 is new value 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _is_new_value(mapping: dict[str, set[str]], entity: Any, value: Any) -> bool:
    key = _norm(entity)
    clean = _norm(value)
    if not key or not clean or key not in mapping:
        return False
    known = mapping[key]
    return bool(known) and clean not in known


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _set_map 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 set map 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _set_map(payload: Any) -> dict[str, set[str]]:
    if not isinstance(payload, dict):
        return {}
    return {_norm(key): _as_set(value) for key, value in payload.items() if _norm(key)}


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _int_set_map 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 int set map 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _int_set_map(payload: Any) -> dict[str, set[int]]:
    if not isinstance(payload, dict):
        return {}
    result: dict[str, set[int]] = {}
    for key, values in payload.items():
        clean_key = _norm(key)
        if not clean_key:
            continue
        result[clean_key] = _as_int_set(values)
    return result


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _as_int_set 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 把 as int set 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
def _as_int_set(values: Any) -> set[int]:
    result: set[int] = set()
    raw_values = values if isinstance(values, Iterable) and not isinstance(values, str) else [values]
    for item in raw_values:
        try:
            result.add(int(item))
        except (TypeError, ValueError):
            continue
    return result


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _sorted_map 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 sorted map 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _sorted_map(payload: dict[str, set[str]]) -> dict[str, list[str]]:
    return {key: sorted(values) for key, values in sorted(payload.items())}


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _sorted_int_map 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 sorted int map 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _sorted_int_map(payload: dict[str, set[int]]) -> dict[str, list[int]]:
    return {key: sorted(values) for key, values in sorted(payload.items())}


__all__ = ["SecurityBaselines", "ensure_baselines"]
