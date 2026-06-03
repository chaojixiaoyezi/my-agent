
from __future__ import annotations

from dataclasses import fields

from ....common.value_parsing import sequence_strings
from ...models import (
    ContextManifest,
    EvidencePacket,
    Finding,
    QualityContract,
    StatusReport,
)


def _field_names(model: type) -> set[str]:
    return {item.name for item in fields(model)}


def _normalize_nested_model(model: type, item: dict[str, object]):
    payload = {key: item[key] for key in _field_names(model) if key in item}
    return model(**payload)


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
        payload[key] = sequence_strings(payload.get(key), allow_scalar=True)
    return QualityContract(**payload)


def _normalize_context_manifest(value: object) -> ContextManifest:
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


def _normalize_evidence_packet(value: object) -> EvidencePacket:
    if isinstance(value, EvidencePacket):
        return value
    if not isinstance(value, dict):
        return EvidencePacket()
    payload = {key: value[key] for key in _field_names(EvidencePacket) if key in value}
    for key in ["evidence_refs", "artifact_refs", "counter_evidence_refs", "unresolved_risks"]:
        payload[key] = sequence_strings(payload.get(key), allow_scalar=True)
    payload["confidence"] = _float_value(payload.get("confidence"))
    payload["created_at"] = _float_value(payload.get("created_at"))
    return EvidencePacket(**payload)


def _normalize_finding(value: object) -> Finding:
    if isinstance(value, Finding):
        return value
    if not isinstance(value, dict):
        return Finding()
    payload = {key: value[key] for key in _field_names(Finding) if key in value}
    for key in ["evidence_packet_ids", "evidence_refs", "counter_evidence_refs"]:
        payload[key] = sequence_strings(payload.get(key), allow_scalar=True)
    payload["confidence"] = _float_value(payload.get("confidence"))
    payload["created_at"] = _float_value(payload.get("created_at"))
    return Finding(**payload)


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
        payload[key] = sequence_strings(payload.get(key), allow_scalar=True)
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


def _string_list_value(value: object) -> list[str]:
    return [str(item) for item in _list_value(value) if item not in (None, "")]


def _float_value(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}
