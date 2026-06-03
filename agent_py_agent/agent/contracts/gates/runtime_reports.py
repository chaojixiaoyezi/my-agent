
from __future__ import annotations

from typing import Any

from .models import GateDecision, GateFinding


def evaluate_acceptance_closeout_gate(report: dict[str, Any]) -> GateDecision:
    status = str(report.get("final_status") or report.get("status") or "").strip().upper()
    verification = str(report.get("verification_status") or "").strip().upper()
    if status in {"DONE", "SUCCEEDED", "VERIFIED"} and verification not in {"PASSED", "VERIFIED"}:
        return GateDecision.deny(
            "acceptance_closeout",
            "ACCEPTANCE_VERIFICATION_MISSING",
            evidence={"final_status": status, "verification_status": verification},
        )
    runtime_gate = report.get("runtime_gate")
    if not isinstance(runtime_gate, dict):
        return GateDecision.deny("acceptance_closeout", "ACCEPTANCE_RUNTIME_GATE_MISSING")
    if runtime_gate.get("allowed") is not True:
        findings = gate_payload_findings(runtime_gate, fallback="ACCEPTANCE_RUNTIME_GATE_FAILED")
        return GateDecision.repair("acceptance_closeout", findings, evidence={"final_status": status})
    return GateDecision.allow("acceptance_closeout", evidence={"final_status": status, "verification_status": verification})


def evaluate_final_closeout_gate(report: dict[str, Any]) -> GateDecision:
    findings: list[GateFinding] = []
    child_gates = [
        "run_contract_gate",
        "runtime_gate",
        "state_gate",
        "delivery_quality_gate",
        *([] if "fact_evidence_gate" not in report else ["fact_evidence_gate"]),
        "acceptance_gate",
    ]
    for key in child_gates:
        _require_allowed_child_gate(report, key, findings)
    if findings:
        return GateDecision.repair("final_closeout", findings, evidence={"missing_count": len(findings)})
    return GateDecision.allow(
        "final_closeout",
        evidence={"child_gates": child_gates},
    )


def evaluate_runtime_audit_gate(records: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> GateDecision:
    findings: list[GateFinding] = []
    if not isinstance(records, (list, tuple)):
        return GateDecision.deny("runtime_audit", "AUDIT_RECORDS_MISSING")
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            findings.append(GateFinding("AUDIT_RECORD_INVALID", evidence={"index": index}))
            continue
        if is_tool_audit_record(record):
            validate_tool_audit_record(index, record, findings)
    if findings:
        return GateDecision.recovering("runtime_audit", findings, evidence={"record_count": len(records)})
    return GateDecision.allow("runtime_audit", evidence={"record_count": len(records)})


def evaluate_recovery_replay_gate(snapshot: dict[str, Any]) -> GateDecision:
    findings: list[GateFinding] = []
    _require_text(snapshot, "run_id", findings)
    _require_text(snapshot, "task_id", findings)
    _require_mapping(snapshot, "scope", findings)
    _require_text(snapshot, "effective_contract_ref", findings)
    _require_text(snapshot, "effective_contract_hash", findings)
    _require_text(snapshot, "tool_manifest_ref", findings)
    _require_text(snapshot, "runlog_ref", findings)
    if findings:
        return GateDecision.recovering("recovery_replay", findings, evidence={"missing_count": len(findings)})
    lineage = evaluate_recovery_lineage_gate(snapshot)
    if not lineage.allowed:
        return lineage
    return GateDecision.allow(
        "recovery_replay",
        evidence={
            "run_id": str(snapshot.get("run_id") or ""),
            "task_id": str(snapshot.get("task_id") or ""),
            "effective_contract_hash": str(snapshot.get("effective_contract_hash") or ""),
        },
    )


def evaluate_recovery_lineage_gate(snapshot: dict[str, Any]) -> GateDecision:
    refs = snapshot.get("previous_artifact_refs")
    if refs is None:
        return GateDecision.allow("recovery_lineage", evidence={"previous_artifact_count": 0})
    if not isinstance(refs, list):
        return GateDecision.recovering("recovery_lineage", [GateFinding("RECOVERY_ARTIFACT_REFS_INVALID")])
    findings: list[GateFinding] = []
    for index, ref in enumerate(refs):
        if not isinstance(ref, dict):
            findings.append(GateFinding("RECOVERY_ARTIFACT_LINEAGE_INVALID", evidence={"index": index}))
            continue
        source_run_id = str(ref.get("source_run_id") or ref.get("run_id") or "").strip()
        operation_id = str(ref.get("operation_id") or ref.get("source_operation_id") or "").strip()
        path = str(ref.get("path") or ref.get("artifact_ref") or "").strip()
        if not path or not source_run_id or not operation_id:
            findings.append(
                GateFinding(
                    "RECOVERY_ARTIFACT_LINEAGE_MISSING",
                    evidence={"index": index, "has_path": bool(path), "has_source_run_id": bool(source_run_id), "has_operation_id": bool(operation_id)},
                )
            )
    if findings:
        return GateDecision.recovering("recovery_lineage", findings, evidence={"previous_artifact_count": len(refs)})
    return GateDecision.allow("recovery_lineage", evidence={"previous_artifact_count": len(refs)})


def gate_payload_findings(payload: dict[str, Any], *, fallback: str) -> list[GateFinding]:
    raw = payload.get("findings")
    if not isinstance(raw, list):
        return [GateFinding(fallback)]
    findings = [_gate_payload_finding(item, fallback=fallback) for item in raw if isinstance(item, dict)]
    return findings or [GateFinding(fallback)]


def is_tool_audit_record(record: dict[str, Any]) -> bool:
    return str(record.get("type") or "tool_result") == "tool_result" or str(record.get("tool") or "").strip() != ""


def validate_tool_audit_record(index: int, record: dict[str, Any], findings: list[GateFinding]) -> None:
    if not isinstance(record.get("runtime_gate"), dict):
        findings.append(GateFinding("AUDIT_RUNTIME_GATE_MISSING", evidence={"index": index}))
    if not isinstance(record.get("parameters"), dict):
        findings.append(GateFinding("AUDIT_PARAMETERS_MISSING", evidence={"index": index}))


def _require_allowed_child_gate(report: dict[str, Any], key: str, findings: list[GateFinding]) -> None:
    payload = report.get(key)
    code_prefix = "FINAL_CLOSEOUT_" + key.removesuffix("_gate").upper()
    if not isinstance(payload, dict):
        findings.append(GateFinding(f"{code_prefix}_GATE_MISSING"))
        return
    if payload.get("allowed") is not True:
        child_findings = gate_payload_findings(payload, fallback=f"{code_prefix}_GATE_FAILED")
        findings.extend(
            GateFinding(
                f"{code_prefix}_GATE_FAILED",
                evidence={"child_gate": key, "child_code": finding.code, **finding.evidence},
            )
            for finding in child_findings
        )


def _gate_payload_finding(item: dict[str, Any], *, fallback: str) -> GateFinding:
    evidence = item.get("evidence")
    return GateFinding(
        str(item.get("code") or fallback),
        severity=str(item.get("severity") or "P1"),
        message=str(item.get("message") or ""),
        evidence=dict(evidence) if isinstance(evidence, dict) else {},
    )


def _require_text(snapshot: dict[str, Any], key: str, findings: list[GateFinding]) -> None:
    if not str(snapshot.get(key) or "").strip():
        findings.append(GateFinding(f"{key.upper()}_MISSING"))


def _require_mapping(snapshot: dict[str, Any], key: str, findings: list[GateFinding]) -> None:
    value = snapshot.get(key)
    if not isinstance(value, dict) or not value:
        findings.append(GateFinding(f"{key.upper()}_MISSING"))


__all__ = [
    "evaluate_acceptance_closeout_gate",
    "evaluate_final_closeout_gate",
    "evaluate_recovery_lineage_gate",
    "evaluate_recovery_replay_gate",
    "evaluate_runtime_audit_gate",
    "gate_payload_findings",
    "is_tool_audit_record",
    "validate_tool_audit_record",
]
