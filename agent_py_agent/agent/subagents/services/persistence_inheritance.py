# LLM: Persistence helpers for inheritance manifests stay separate from the main save flow.
# 模块用途: 归一化和写入子代理继承清单，避免 persistence 主文件继续膨胀。

from __future__ import annotations

"""Persistence helpers for subagent inheritance manifests."""

import json
from dataclasses import asdict, fields
from pathlib import Path

from ..models import InheritanceManifest, SubAgentTask


# LLM: normalize_inheritance_manifest keeps old task JSON readable after manifest schema evolves.
# 函数用途: 归一化继承清单输入，避免旧字段或空值破坏 SubAgentTask 加载。
def normalize_inheritance_manifest(value: object) -> InheritanceManifest:
    if isinstance(value, InheritanceManifest):
        return value
    if not isinstance(value, dict):
        return InheritanceManifest()
    payload = {key: value[key] for key in _field_names(InheritanceManifest) if key in value}
    for key in ["inherited", "overridden", "dropped", "policy", "reserved"]:
        payload[key] = _dict_value(payload.get(key))
    payload["created_at"] = _float_value(payload.get("created_at"))
    return InheritanceManifest(**payload)


# LLM: write_inheritance_manifest writes the audit artifact without changing runtime context.
# 函数用途: 将继承清单写成机器可读 JSON，供上级代理、接管代理和审计读取。
def write_inheritance_manifest(task: SubAgentTask) -> None:
    if not task.inheritance_manifest_json:
        return
    Path(task.inheritance_manifest_json).write_text(
        json.dumps(asdict(task.inheritance_manifest), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# LLM: _field_names keeps inheritance JSON normalization aligned with dataclass fields.
# 函数用途: 获取 InheritanceManifest 的合法字段，过滤旧文件里的未知键。
def _field_names(model: type) -> set[str]:
    return {item.name for item in fields(model)}


# LLM: _dict_value normalizes manifest buckets and policy payloads.
# 函数用途: 读取 dict 字段，非 dict 输入统一退回空字典。
def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


# LLM: _float_value tolerates bad created_at values in historical manifests.
# 函数用途: 将创建时间归一化为 float，失败时返回 0。
def _float_value(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
