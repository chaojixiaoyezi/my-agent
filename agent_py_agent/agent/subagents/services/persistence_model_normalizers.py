# LLM: Persistence model normalizers keep disk compatibility separate from save/load orchestration.
# 模块用途: 归一化 task.json 里的嵌套 dataclass 字段，避免 persistence.py 因兼容逻辑继续膨胀。

from __future__ import annotations

from dataclasses import fields

from ..models import (
    ContextManifest,
    EvidencePacket,
    Finding,
    QualityContract,
    StatusReport,
)


# LLM: _field_names centralizes dataclass field filtering for tolerant persisted reads.
# 函数用途: 返回模型字段集合，读取旧/新 task.json 时过滤未知字段。
def _field_names(model: type) -> set[str]:
    return {item.name for item in fields(model)}


# LLM: _normalize_nested_model keeps persisted child records tolerant of reserved/future keys.
# 函数用途: 读取嵌套 dataclass 记录时只保留当前模型认识的字段，避免旧/新记录互相卡死。
def _normalize_nested_model(model: type, item: dict[str, object]):
    payload = {key: item[key] for key in _field_names(model) if key in item}
    return model(**payload)


# LLM: _string_list_value accepts scalar/list/tuple persisted forms and returns strings.
# 函数用途: 把历史不同形态的 refs/checks 字段归一成字符串列表。
def _string_list_value(value: object) -> list[str]:
    return [str(item) for item in _list_value(value) if item not in (None, "")]


# LLM: _normalize_quality_contract keeps quality contract reads backward compatible.
# 函数用途: 解析并归一化 quality_contract 的输入形态，让下游只处理稳定结构。
def _normalize_quality_contract(value: object) -> QualityContract:
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
        payload[key] = _string_list_value(payload.get(key))
    return QualityContract(**payload)


# LLM: _normalize_context_manifest preserves soft hint paths without reviving hard input gates.
# 函数用途: 解析并归一化上下文 manifest，保留 hint_read_paths 但不把它变成启动阻断。
def _normalize_context_manifest(value: object) -> ContextManifest:
    if isinstance(value, ContextManifest):
        return value
    if not isinstance(value, dict):
        return ContextManifest()
    payload = {key: value[key] for key in _field_names(ContextManifest) if key in value}
    for key in ["task_pack_refs", "required_read_paths", "hint_read_paths", "omitted_context"]:
        payload[key] = _string_list_value(payload.get(key))
    try:
        payload["token_budget"] = int(payload.get("token_budget") or 0)
    except (TypeError, ValueError):
        payload["token_budget"] = 0
    return ContextManifest(**payload)


# LLM: _normalize_context_packs accepts one pack or many packs while dropping malformed values.
# 函数用途: 读取 context_packs 时返回稳定的 dict 列表。
def _normalize_context_packs(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


# LLM: _normalize_evidence_packet keeps evidence packets tolerant of older persisted shapes.
# 函数用途: 解析并归一化证据 packet 的输入形态，让下游只处理稳定结构。
def _normalize_evidence_packet(value: object) -> EvidencePacket:
    if isinstance(value, EvidencePacket):
        return value
    if not isinstance(value, dict):
        return EvidencePacket()
    payload = {key: value[key] for key in _field_names(EvidencePacket) if key in value}
    for key in ["evidence_refs", "artifact_refs", "counter_evidence_refs", "unresolved_risks"]:
        payload[key] = _string_list_value(payload.get(key))
    payload["confidence"] = _float_value(payload.get("confidence"))
    payload["created_at"] = _float_value(payload.get("created_at"))
    return EvidencePacket(**payload)


# LLM: _normalize_finding keeps finding records readable across schema changes.
# 函数用途: 解析并归一化 finding 的输入形态，让下游只处理稳定结构。
def _normalize_finding(value: object) -> Finding:
    if isinstance(value, Finding):
        return value
    if not isinstance(value, dict):
        return Finding()
    payload = {key: value[key] for key in _field_names(Finding) if key in value}
    for key in ["evidence_packet_ids", "evidence_refs", "counter_evidence_refs"]:
        payload[key] = _string_list_value(payload.get(key))
    payload["confidence"] = _float_value(payload.get("confidence"))
    payload["created_at"] = _float_value(payload.get("created_at"))
    return Finding(**payload)


# LLM: _normalize_status_report keeps status report reads tolerant and typed.
# 函数用途: 解析并归一化状态报告的输入形态，让下游只处理稳定结构。
def _normalize_status_report(value: object) -> StatusReport:
    if isinstance(value, StatusReport):
        return value
    if not isinstance(value, dict):
        return StatusReport()
    payload = {key: value[key] for key in _field_names(StatusReport) if key in value}
    payload["version"] = int(_float_value(payload.get("version"), 0.0))
    payload["progress"] = _float_value(payload.get("progress"))
    payload["summary_delta"] = _dict_value(payload.get("summary_delta"))
    payload["budget_used"] = _dict_value(payload.get("budget_used"))
    for key in ["artifact_refs", "evidence_refs", "blockers"]:
        payload[key] = _string_list_value(payload.get(key))
    payload["updated_at"] = _float_value(payload.get("updated_at"))
    return StatusReport(**payload)


def _list_value(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _float_value(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}
