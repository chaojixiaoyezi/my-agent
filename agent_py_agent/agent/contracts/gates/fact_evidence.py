
from __future__ import annotations

from typing import Any

from ...common.value_parsing import sequence_strings
from ...common.value_parsing import text_value as _text
from ..evidence_contract import EvidenceContractRequest, evaluate_evidence_contract
from ..staged_checkpoint_evidence_payloads import claims, source_refs
from .models import GateDecision, GateFinding


def evaluate_fact_evidence_gate(
    payload: dict[str, Any],
    contract: dict[str, Any],
    *,
    archive_tool_calls: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> GateDecision:
    if not contract:
        return GateDecision.allow("fact_evidence", evidence={"declared": False})
    source_records = source_refs(payload.get("source_refs"))
    claim_records = claims(payload.get("claims"))
    evidence_contract = _evidence_contract(contract)
    findings = [
        *_evidence_contract_findings(evidence_contract, source_records, claim_records),
        *_tool_backing_findings(
            payload.get("source_refs"),
            payload.get("claims"),
            archive_tool_calls or (),
            required=bool(contract.get("require_tool_backed_sources")),
        ),
    ]
    evidence = {
        "declared": True,
        "source_count": len(source_records),
        "claim_count": len(claim_records),
        "archive_tool_call_count": len([item for item in archive_tool_calls or () if isinstance(item, dict)]),
    }
    if findings:
        return GateDecision.repair("fact_evidence", findings, evidence=evidence)
    return GateDecision.allow("fact_evidence", evidence=evidence)


def _evidence_contract(contract: dict[str, Any]) -> dict[str, Any]:
    value = contract.get("evidence_contract")
    return dict(value) if isinstance(value, dict) else dict(contract)


def _evidence_contract_findings(
    contract: dict[str, Any],
    source_records: object,
    claim_records: object,
) -> list[GateFinding]:
    report = evaluate_evidence_contract(
        EvidenceContractRequest(
            source_refs=list(source_records),
            claims=list(claim_records),
            required_fields=sequence_strings(contract.get("required_fields")),
            allowed_value_types=sequence_strings(contract.get("allowed_value_types")) or ["exact"],
            min_confidence=_float_value(contract.get("min_confidence")),
            require_methodology_for_estimates=bool(contract.get("require_methodology_for_estimates", False)),
            require_verified=bool(contract.get("require_verified", True)),
        )
    )
    return [_gate_finding(item) for item in report.findings]


def _tool_backing_findings(
    raw_sources: object,
    raw_claims: object,
    archive_tool_calls: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    required: bool,
) -> list[GateFinding]:
    if not required:
        return []
    sources_by_id = _raw_sources_by_id(raw_sources)
    referenced_source_ids = _referenced_source_ids(raw_claims)
    archive_index = _archive_index(archive_tool_calls)
    findings: list[GateFinding] = []
    for source_id in sorted(referenced_source_ids):
        source = sources_by_id.get(source_id)
        if not isinstance(source, dict):
            continue
        if not _source_has_tool_backing(source, archive_index):
            findings.append(
                GateFinding(
                    "FACT_SOURCE_TOOL_BACKING_MISSING",
                    severity="hard",
                    evidence={
                        "source_id": source_id,
                        "required_state": {"tool_call_ref": "present_in_archive_tool_calls"},
                        "current_state": _source_backing_state(source),
                    },
                )
            )
    return findings


def _raw_sources_by_id(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in value:
        if isinstance(item, dict) and (source_id := str(item.get("source_id") or "").strip()):
            result[source_id] = dict(item)
    return result


def _referenced_source_ids(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    source_ids: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            source_ids.update(sequence_strings(item.get("source_ids")))
    return source_ids


def _archive_index(records: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> dict[str, set[str]]:
    ids: set[str] = set()
    refs: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        _collect_archive_ids(record, ids)
        _collect_archive_refs(record, refs)
    return {"ids": ids, "refs": refs}


def _collect_archive_ids(record: dict[str, Any], ids: set[str]) -> None:
    runtime_gate = record.get("runtime_gate")
    evidence = runtime_gate.get("evidence") if isinstance(runtime_gate, dict) else {}
    for source in (record, evidence if isinstance(evidence, dict) else {}):
        _collect_id_keys(source, ids)


def _collect_id_keys(source: dict[str, Any], ids: set[str]) -> None:
    for key in ("scoped_call_id", "tool_call_id", "call_id", "id", "operation_id", "idempotency_key"):
        if value := _text(source.get(key)):
            ids.add(value)


def _collect_archive_refs(record: dict[str, Any], refs: set[str]) -> None:
    for source in (record, _mapping(record.get("parameters")), _mapping(record.get("tool_result_envelope"))):
        _collect_ref_keys(source, refs, ("artifact_ref", "path", "output_path", "source_json_path", "source_ref", "uri", "url"))
    tool_refs = record.get("tool_result_refs")
    if isinstance(tool_refs, list):
        for item in tool_refs:
            _collect_ref_keys(_mapping(item), refs, ("artifact_ref", "path", "uri", "url"))


def _collect_ref_keys(source: dict[str, Any], refs: set[str], keys: tuple[str, ...]) -> None:
    for key in keys:
        if value := _text(source.get(key)):
            refs.add(value)


def _source_has_tool_backing(source: dict[str, Any], archive_index: dict[str, set[str]]) -> bool:
    backing_ids = archive_index["ids"]
    backing_refs = archive_index["refs"]
    if _mapping_has_backing_id(source, backing_ids):
        return True
    reserved = source.get("reserved")
    if isinstance(reserved, dict) and _mapping_has_backing_id(reserved, backing_ids):
        return True
    for key in ("artifact_ref", "uri"):
        if (value := _text(source.get(key))) and value in backing_refs:
            return True
    return False


def _mapping_has_backing_id(source: dict[str, Any], backing_ids: set[str]) -> bool:
    for key in ("tool_call_ref", "tool_call_id", "call_id", "operation_id"):
        if (value := _text(source.get(key))) and value in backing_ids:
            return True
    return False


def _source_backing_state(source: dict[str, Any]) -> dict[str, str]:
    reserved = source.get("reserved")
    reserved_map = reserved if isinstance(reserved, dict) else {}
    return {
        "tool_call_ref": _text(source.get("tool_call_ref") or reserved_map.get("tool_call_ref")),
        "artifact_ref": _text(source.get("artifact_ref")),
        "uri": _text(source.get("uri")),
    }


def _gate_finding(item: dict[str, Any]) -> GateFinding:
    public = {"code", "severity", "message"}
    return GateFinding(
        _text(item.get("code")) or "FACT_EVIDENCE_FINDING",
        severity=_text(item.get("severity")) or "hard",
        message=_text(item.get("message")),
        evidence={key: value for key, value in item.items() if key not in public},
    )


def _float_value(value: object, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}

__all__ = ["evaluate_fact_evidence_gate"]
