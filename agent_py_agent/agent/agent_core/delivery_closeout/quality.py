
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...contracts.gates import (
    DeliveryQualityTraceScope,
    GateDecision,
    GateFinding,
    append_delivery_quality_gate_trace,
    evaluate_delivery_quality_gate,
)
from ...contracts.recovery_actions import RecoveryAction
from .artifacts import _required_artifacts


def delivery_quality_decision(
    *,
    contract: dict[str, Any],
    report: dict[str, Any],
    workspace_root: Path,
    contract_hash: str,
) -> GateDecision:
    quality_contract = delivery_quality_contract(contract)
    if not quality_contract:
        return GateDecision.allow("delivery_quality", evidence={"declared": False})
    payload = load_delivery_quality_payload(contract, workspace_root)
    if payload is None:
        return GateDecision.allow(
            "delivery_quality",
            recommended_action=RecoveryAction.RECORD_QUALITY_PAYLOAD.value,
            evidence={"declared": True, "warning_codes": ["DELIVERY_QUALITY_PAYLOAD_MISSING"]},
        )
    payload.setdefault("artifacts", quality_artifact_records(report, contract_hash=contract_hash))
    decision = evaluate_delivery_quality_gate(payload, quality_contract, contract_hash=contract_hash)
    append_delivery_quality_gate_trace(
        workspace_root / ".agent_delivery" / "delivery_quality_gate.jsonl",
        decision,
        DeliveryQualityTraceScope(
            run_id=str(report.get("run_id") or ""),
            task_id=str(report.get("task_id") or ""),
            contract_hash=contract_hash,
        ),
    )
    if not decision.allowed and not _quality_enforcement_required(quality_contract):
        return GateDecision.allow(
            "delivery_quality",
            recommended_action=RecoveryAction.REVIEW_QUALITY_FINDINGS.value,
            evidence={
                "declared": True,
                "warning_codes": list(decision.finding_codes),
                "advisory_status": decision.status,
            },
        )
    return decision


def delivery_quality_contract(contract: dict[str, Any]) -> dict[str, Any]:
    for key in ("delivery_quality_contract", "quality_contract", "data_contract"):
        value = contract.get(key)
        if isinstance(value, dict):
            return dict(value)
    return _artifact_validation_quality_contract(contract)


def _artifact_validation_quality_contract(contract: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for item in _required_artifacts(contract):
        validation = item.get("validation_contract")
        if not isinstance(validation, dict):
            continue
        quality = _validation_quality_contract(validation)
        if quality:
            merged = _merge_quality_contract(merged, quality)
    return merged


def _validation_quality_contract(validation: dict[str, Any]) -> dict[str, Any]:
    quality: dict[str, Any] = {}
    for key in ("evidence_contract", "language_contract"):
        value = validation.get(key)
        if isinstance(value, dict):
            quality[key] = dict(value)
    metrics = validation.get("metric_contracts")
    if isinstance(metrics, list):
        quality["metric_contracts"] = [dict(item) for item in metrics if isinstance(item, dict)]
    return quality


def _merge_quality_contract(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    if evidence := _merge_evidence_contract(merged.get("evidence_contract"), incoming.get("evidence_contract")):
        merged["evidence_contract"] = evidence
    if language := _merge_language_contract(merged.get("language_contract"), incoming.get("language_contract")):
        merged["language_contract"] = language
    metrics = _dict_list(merged.get("metric_contracts"))
    metrics.extend(_dict_list(incoming.get("metric_contracts")))
    if metrics:
        merged["metric_contracts"] = metrics
    return merged


def _quality_enforcement_required(contract: dict[str, Any]) -> bool:
    value = str(contract.get("enforcement") or contract.get("mode") or "").strip().lower()
    return value in {"required", "hard", "block", "blocking"}


def _merge_evidence_contract(base: object, incoming: object) -> dict[str, Any]:
    if not isinstance(base, dict) and not isinstance(incoming, dict):
        return {}
    merged = dict(base) if isinstance(base, dict) else {}
    new = dict(incoming) if isinstance(incoming, dict) else {}
    merged.update(new)
    merged["required_fields"] = _merged_string_list(base, incoming, "required_fields")
    if allowed := _merged_string_list(base, incoming, "allowed_value_types"):
        merged["allowed_value_types"] = allowed
    merged["require_verified"] = bool(_dict_bool(base, "require_verified") or _dict_bool(incoming, "require_verified"))
    merged["require_methodology_for_estimates"] = bool(
        _dict_bool(base, "require_methodology_for_estimates")
        or _dict_bool(incoming, "require_methodology_for_estimates")
    )
    merged["min_confidence"] = max(_dict_float(base, "min_confidence"), _dict_float(incoming, "min_confidence"))
    return {key: value for key, value in merged.items() if value not in (None, "", [])}


def _merge_language_contract(base: object, incoming: object) -> dict[str, Any]:
    if not isinstance(base, dict) and not isinstance(incoming, dict):
        return {}
    merged = dict(base) if isinstance(base, dict) else {}
    new = dict(incoming) if isinstance(incoming, dict) else {}
    merged.update(new)
    merged["fields"] = _merged_string_list(base, incoming, "fields")
    return {key: value for key, value in merged.items() if value not in (None, "", [])}


def _merged_string_list(base: object, incoming: object, key: str) -> list[str]:
    values: list[str] = []
    for source in (base, incoming):
        if isinstance(source, dict):
            values.extend(str(item).strip() for item in source.get(key, []) if str(item).strip())
    return sorted(set(values))


def _dict_list(value: object) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _dict_bool(value: object, key: str) -> bool:
    return bool(value.get(key)) if isinstance(value, dict) else False


def _dict_float(value: object, key: str) -> float:
    if not isinstance(value, dict):
        return 0.0
    try:
        return float(value.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def load_delivery_quality_payload(contract: dict[str, Any], workspace_root: Path) -> dict[str, Any] | None:
    ref = delivery_quality_payload_ref(contract)
    if not ref:
        return None
    path = workspace_relative_json_path(ref, workspace_root)
    if path is None or not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, dict) else None


def delivery_quality_payload_ref(contract: dict[str, Any]) -> str:
    for key in ("delivery_quality_payload_ref", "quality_payload_ref", "source_data_ref"):
        if ref := str(contract.get(key) or "").strip():
            return ref
    for item in _required_artifacts(contract):
        ref = staging_payload_ref(item)
        if ref:
            return ref
    return ""


def staging_payload_ref(item: dict[str, Any]) -> str:
    validation_contract = item.get("validation_contract")
    if not isinstance(validation_contract, dict):
        return ""
    staging = validation_contract.get("staging_contract")
    if not isinstance(staging, dict):
        return ""
    if ref := str(staging.get("source_json_ref") or "").strip():
        return ref
    return _first_json_ref(staging.get("checkpoint_refs"))


def workspace_relative_json_path(ref: str, workspace_root: Path) -> Path | None:
    candidate = Path(ref).expanduser()
    path = candidate.resolve(strict=False) if candidate.is_absolute() else (workspace_root / candidate).resolve()
    try:
        path.relative_to(workspace_root)
    except ValueError:
        return None
    return path if path.suffix.lower() == ".json" else None


def quality_artifact_records(report: dict[str, Any], *, contract_hash: str) -> list[dict[str, Any]]:
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    return [_quality_artifact_record(item, contract_hash=contract_hash) for item in artifacts if isinstance(item, dict)]


def _quality_artifact_record(item: dict[str, Any], *, contract_hash: str) -> dict[str, Any]:
    acceptance = item.get("acceptance_report")
    payload = acceptance.get("artifact_ref_payload") if isinstance(acceptance, dict) else {}
    return {
        "artifact_ref": str(item.get("path") or ""),
        "hash": str(payload.get("hash") if isinstance(payload, dict) else ""),
        "validated_contract_hash": contract_hash,
    }


def _first_json_ref(refs: object) -> str:
    if not isinstance(refs, list):
        return ""
    for ref in refs:
        text = str(ref or "").strip()
        if text.lower().endswith(".json"):
            return text
    return ""


__all__ = [
    "delivery_quality_contract",
    "delivery_quality_decision",
    "delivery_quality_payload_ref",
    "load_delivery_quality_payload",
    "quality_artifact_records",
    "staging_payload_ref",
    "workspace_relative_json_path",
]
