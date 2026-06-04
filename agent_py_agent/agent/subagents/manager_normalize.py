
"""Field normalization helpers for subagent manager data models.

新手说明:
这个模块负责把外部传入的
松散字典/列表数据规范化成强类型的数据模型实例。主要处理
QualityContract、ContextManifest 和 context_packs。
"""

from __future__ import annotations

from dataclasses import fields

from ..common.value_parsing import sequence_strings


def _field_names(model: type) -> set[str]:
    return {item.name for item in fields(model)}


def _normalize_quality_contract(value: object) -> QualityContract:
    from .models import QualityContract

    if isinstance(value, QualityContract):
        return value
    if not isinstance(value, dict):
        return QualityContract()
    payload = {key: value[key] for key in _field_names(QualityContract) if key in value}
    for key in [
        "failure_conditions",
        "forbidden_delivery",
        "must_check",
        "sampling_plan",
        "evidence_required",
        "allowed_degradation",
    ]:
        payload[key] = sequence_strings(payload.get(key), allow_scalar=True)
    return QualityContract(**payload)


def _normalize_context_manifest(value: object) -> ContextManifest:
    from .models import ContextManifest

    if isinstance(value, ContextManifest):
        return value
    if not isinstance(value, dict):
        return ContextManifest()
    payload = {key: value[key] for key in _field_names(ContextManifest) if key in value}
    for key in ["task_pack_refs", "required_read_paths", "hint_read_paths", "omitted_context"]:
        payload[key] = sequence_strings(payload.get(key), allow_scalar=True)
    try:
        payload["token_budget"] = int(payload.get("token_budget") or 0)
    except (TypeError, ValueError):
        payload["token_budget"] = 0
    return ContextManifest(**payload)


def _normalize_context_packs(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
