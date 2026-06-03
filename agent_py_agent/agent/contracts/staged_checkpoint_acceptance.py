
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..common.value_parsing import sequence_strings
from .artifact_collection_contract import collection_contract_finding_dicts
from .contract_trace import trace_entry, with_contract_trace
from .evidence_contract import (
    EvidenceContractRequest,
    evaluate_evidence_contract,
)
from .gates.delivery_quality import delivery_quality_metric_findings
from .staged_checkpoint_contract_options import staged_checkpoint_contexts
from .staged_checkpoint_evidence_payloads import claims, source_refs
from .staged_checkpoint_files import artifact_path, json_checkpoint_status


@dataclass(frozen=True)
class StagedEvidenceOptions:
    phase: str = "staged"
    emit_path_findings: bool = True


@dataclass(frozen=True)
class StagedEvidenceRequest:
    ref: str
    task_workspace: Path
    evidence_contract: dict[str, object]
    options: StagedEvidenceOptions = StagedEvidenceOptions()


@dataclass(frozen=True)
class StagedCheckpointOptions:
    required_columns: list[str] | None = None
    required_sheets_min: int = 0
    validation_contract: dict[str, object] | None = None


_DEFAULT_STAGED_CHECKPOINT_OPTIONS = StagedCheckpointOptions()


def staged_checkpoint_findings(
    items: list[dict[str, object]],
    task_workspace: Path,
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    preferred_paths = {str(item.get("preferred_path") or item.get("path") or "") for item in items}
    for item in items:
        contexts = staged_checkpoint_contexts(item, preferred_paths)
        for context in contexts:
            findings.extend(
                one_staged_checkpoint_findings(
                    context.ref,
                    task_workspace,
                    StagedCheckpointOptions(
                        context.required_columns,
                        context.required_sheets_min,
                        context.validation_contract,
                    ),
                )
            )
            findings.extend(
                staged_json_evidence_findings(
                    StagedEvidenceRequest(
                        context.ref,
                        task_workspace,
                        context.evidence_contract,
                        StagedEvidenceOptions(emit_path_findings=False),
                    )
                )
            )
        if contexts:
            findings.extend(collection_contract_finding_dicts(contexts[0].validation_contract, task_workspace))
    return findings


def one_staged_checkpoint_findings(
    ref: str,
    task_workspace: Path,
    options: StagedCheckpointOptions = _DEFAULT_STAGED_CHECKPOINT_OPTIONS,
) -> list[dict[str, object]]:
    try:
        path = artifact_path(ref, task_workspace)
    except ValueError:
        return [_path_outside_workspace_finding(ref, task_workspace, "Staged checkpoint path is outside task workspace.")]
    if not path.exists():
        return [_finding("STAGED_ARTIFACT_MISSING", ref, path, {"message": "Staged checkpoint does not exist."})]
    if path.is_file() and path.stat().st_size <= 0:
        return [_finding("STAGED_ARTIFACT_EMPTY", ref, path, {"message": "Staged checkpoint is empty."})]
    if path.suffix.lower() != ".json":
        return _artifact_validation_findings(ref, path, task_workspace, options.validation_contract or {})
    return _json_checkpoint_findings(
        ref,
        path,
        options,
    )


def _artifact_validation_findings(
    ref: str,
    path: Path,
    task_workspace: Path,
    validation_contract: dict[str, object],
) -> list[dict[str, object]]:
    from .artifact_acceptance import validate_artifact
    from .artifact_acceptance_models import ArtifactAcceptanceRequest

    checkpoint_contract = dict(validation_contract)
    checkpoint_contract.pop("validator", None)
    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=path,
            workspace_root=task_workspace,
            validation_contract=checkpoint_contract,
        )
    )
    return [
        with_contract_trace(
            {
                **finding.to_dict(),
                "stage_ref": ref,
                "artifact_kind": report.artifact_kind,
            },
            (
                trace_entry("staged_checkpoint_acceptance", ref=ref, path=path),
                trace_entry("artifact_acceptance", code=finding.code),
            ),
        )
        for finding in report.findings
    ]


def staged_json_evidence_findings(request: StagedEvidenceRequest) -> list[dict[str, object]]:
    ref = request.ref
    task_workspace = request.task_workspace
    evidence_contract = request.evidence_contract
    if not evidence_contract:
        return []
    options = request.options
    try:
        path = artifact_path(ref, task_workspace)
    except ValueError:
        if not options.emit_path_findings:
            return []
        return [_path_outside_workspace_finding(ref, task_workspace, "Staged evidence path is outside task workspace.")]
    value = _staged_json_dict(path)
    if not isinstance(value, dict):
        return []
    source_records = source_refs(value.get("source_refs"))
    claim_records = claims(value.get("claims"))
    findings = _evidence_finding_dicts(
        _evaluate_staged_evidence(evidence_contract, source_records, claim_records, phase=options.phase),
        ref,
        path,
    )
    findings.extend(
        _metric_finding_dicts(
            delivery_quality_metric_findings(evidence_contract.get("metric_contracts"), source_records, claim_records),
            ref,
            path,
        )
    )
    return findings


def _staged_json_dict(path: Path) -> dict[str, object] | None:
    if not path.exists() or path.suffix.lower() != ".json":
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _evaluate_staged_evidence(
    evidence_contract: dict[str, object],
    source_records: list[dict[str, object]],
    claim_records: list[dict[str, object]],
    *,
    phase: str = "staged",
) -> object:
    return evaluate_evidence_contract(
        EvidenceContractRequest(
            source_refs=source_records,
            claims=claim_records,
            required_fields=sequence_strings(evidence_contract.get("required_fields")),
            allowed_value_types=sequence_strings(evidence_contract.get("allowed_value_types")) or ["exact"],
            min_confidence=_float_value(evidence_contract.get("min_confidence")),
            require_methodology_for_estimates=bool(evidence_contract.get("require_methodology_for_estimates", False)),
            require_verified=_requires_verified(evidence_contract, phase),
        )
    )


def _evidence_finding_dicts(report: object, ref: str, path: Path) -> list[dict[str, object]]:
    return [
        with_contract_trace(
            {
                "code": str(item.get("code") or "EVIDENCE_CONTRACT_FAILED"),
                "severity": str(item.get("severity") or "hard"),
                "stage_ref": ref,
                "location": str(path),
                "message": str(item.get("message") or "Evidence contract failed."),
                **{key: val for key, val in item.items() if key not in {"code", "severity", "message"}},
            },
            (
                trace_entry("staged_json_evidence", ref=ref, path=path),
                trace_entry("evidence_contract", code=str(item.get("code") or "")),
            ),
        )
        for item in report.findings
    ]


def _metric_finding_dicts(findings: object, ref: str, path: Path) -> list[dict[str, object]]:
    return [
        with_contract_trace(
            {
                "code": item.code,
                "severity": item.severity,
                "stage_ref": ref,
                "location": str(path),
                "message": item.message or "Metric quality contract failed.",
                **({"evidence": item.evidence} if item.evidence else {}),
            },
            (
                trace_entry("staged_metric_quality", ref=ref, path=path),
                trace_entry("metric_contract", code=str(item.code)),
            ),
        )
        for item in findings
    ]


def _json_checkpoint_findings(
    ref: str,
    path: Path,
    options: StagedCheckpointOptions,
) -> list[dict[str, object]]:
    status = json_checkpoint_status(
        path,
        required_columns=options.required_columns,
        required_sheets_min=options.required_sheets_min,
    )
    code = status["code"]
    if code == "STAGED_JSON_INVALID":
        return [_finding(code, ref, path, {"message": "Staged JSON checkpoint is invalid or truncated.", "parse_error": status.get("parse_error", "")})]
    if code == "STAGED_JSON_NO_ROWS":
        return [_finding(code, ref, path, {"message": "Staged JSON checkpoint has no data rows."})]
    if code != "OK":
        return [_finding(code, ref, path, dict(status))]
    return []


def _float_value(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _requires_verified(evidence_contract: dict[str, object], phase: str) -> bool:
    if phase == "staged":
        return bool(
            evidence_contract.get(
                "staging_require_verified",
                evidence_contract.get("require_verified_during_staging", False),
            )
        )
    return bool(evidence_contract.get("require_verified", True))


def _display_path(ref: str, task_workspace: Path) -> Path:
    preferred = Path(str(ref or ""))
    return preferred.resolve(strict=False) if preferred.is_absolute() else (task_workspace / preferred).resolve(strict=False)


def _path_outside_workspace_finding(ref: str, task_workspace: Path, message: str) -> dict[str, object]:
    return _finding(
        "STAGED_ARTIFACT_PATH_OUTSIDE_WORKSPACE",
        ref,
        _display_path(ref, task_workspace),
        {"message": message},
    )


def _finding(code: str, ref: str, path: Path, detail: dict[str, object] | None = None) -> dict[str, object]:
    payload = detail or {}
    return with_contract_trace(
        {
            "code": code,
            "severity": "hard",
            "stage_ref": ref,
            "location": str(path),
            "message": str(payload.get("message") or "Staged checkpoint failed."),
            **{key: value for key, value in payload.items() if key != "message"},
        },
        (trace_entry("staged_checkpoint_acceptance", ref=ref, path=path, code=code),),
    )


__all__ = [
    "StagedEvidenceOptions",
    "StagedEvidenceRequest",
    "StagedCheckpointOptions",
    "artifact_path",
    "json_checkpoint_status",
    "one_staged_checkpoint_findings",
    "staged_json_evidence_findings",
    "staged_checkpoint_findings",
]
