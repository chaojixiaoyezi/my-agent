# LLM: Subagent identity persistence helpers keep scope metadata separate from main save/load flow.
# 模块用途: 归一化 RuntimeIdentity 预留字段，保证多入口身份和配置隔离元数据只做审计记录。
from __future__ import annotations

"""Normalize runtime identity reserve fields for subagent task persistence."""

from dataclasses import fields

from ..models import RuntimeIdentity


# LLM: normalize_runtime_identity accepts legacy JSON while preserving audit-only scope semantics.
# 函数用途: 读取旧 task.json 或松散 dict 时补齐默认值，不授予工具权限、不写长期记忆、不提升配置。
def normalize_runtime_identity(value: object) -> RuntimeIdentity:
    if isinstance(value, RuntimeIdentity):
        return value
    if not isinstance(value, dict):
        return RuntimeIdentity()
    payload = {key: value[key] for key in _field_names(RuntimeIdentity) if key in value}
    payload["reserved"] = payload.get("reserved") if isinstance(payload.get("reserved"), dict) else {}
    return RuntimeIdentity(**payload)


# LLM: _field_names keeps identity JSON normalization aligned with the dataclass contract.
# 函数用途: 从 dataclass 读取允许字段，避免外部 JSON 注入未声明的权限或配置键。
def _field_names(model: type) -> set[str]:
    return {item.name for item in fields(model)}
