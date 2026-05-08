# LLM: Security persistence helpers keep audit-only security reserve parsing out of the main persistence service.
# 模块用途: 归一化子代理安全信号预留字段，当前只恢复结构，不执行安全策略。

from __future__ import annotations

"""Security reserve persistence helpers."""

from dataclasses import fields

from ..models import SecuritySignal


# LLM: _field_names is local to this helper so callers do not depend on persistence internals.
# 函数用途: 读取 dataclass 字段名，过滤旧 JSON 里的未知字段。
def _field_names(model: type) -> set[str]:
    return {item.name for item in fields(model)}


# LLM: normalize_security_signal is audit-only and does not enforce security policy.
# 函数用途: 把持久化的安全信号恢复成稳定结构，供后续安全模块读取。
def normalize_security_signal(value: object) -> SecuritySignal:
    if isinstance(value, SecuritySignal):
        return value
    if not isinstance(value, dict):
        return SecuritySignal()
    payload = {key: value[key] for key in _field_names(SecuritySignal) if key in value}
    for key in ["evidence_refs", "artifact_refs"]:
        payload[key] = _string_list_value(payload.get(key))
    payload["created_at"] = _float_value(payload.get("created_at"))
    payload["reserved"] = payload.get("reserved") if isinstance(payload.get("reserved"), dict) else {}
    return SecuritySignal(**payload)


# LLM: _string_list_value normalizes security evidence/artifact refs without trusting shape.
# 函数用途: 将安全信号里的引用字段转成字符串列表，过滤空值。
def _string_list_value(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if isinstance(value, tuple):
        return [str(item) for item in value if item is not None]
    return []


# LLM: _float_value tolerates missing or malformed security signal timestamps.
# 函数用途: 将安全信号创建时间转成 float，失败时返回 0。
def _float_value(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
