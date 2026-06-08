# Staged checkpoint context
from __future__ import annotations

from dataclasses import dataclass

from ..common.value_parsing import sequence_strings


@dataclass(frozen=True)
class StagedCheckpointContext:
    ref: str
    required_columns: list[str]
    required_sheets_min: int
    evidence_contract: dict[str, object]
    validation_contract: dict[str, object]


def staged_checkpoint_contexts(
    item: dict[str, object],
    preferred_paths: set[str],
) -> list[StagedCheckpointContext]:
    contract = item.get("validation_contract")
    staging = contract.get("staging_contract") if isinstance(contract, dict) else None
    refs = staging.get("checkpoint_refs") if isinstance(staging, dict) else None
    if not isinstance(contract, dict) or not isinstance(staging, dict) or not isinstance(refs, list):
        return []
    source_ref = str(staging.get("source_json_ref") or staging.get("source_ref") or "").strip()
    return [
        StagedCheckpointContext(
            ref=ref_text,
            required_columns=sequence_strings(contract.get("required_columns")),
            required_sheets_min=_positive_int(contract.get("required_sheets_min")),
            evidence_contract=_evidence_contract(contract, ref_text, source_ref),
            validation_contract=dict(contract),
        )
        for ref in refs
        if (ref_text := str(ref).strip()) and ref_text not in preferred_paths
    ]


def _evidence_contract(contract: dict[str, object], ref: str, source_ref: str) -> dict[str, object]:
    if source_ref and ref != source_ref:
        return {}
    evidence = contract.get("evidence_contract")
    result = dict(evidence) if isinstance(evidence, dict) else {}
    metrics = contract.get("metric_contracts")
    if isinstance(metrics, list):
        result["metric_contracts"] = [dict(item) for item in metrics if isinstance(item, dict)]
    return result


def _positive_int(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


__all__ = ["StagedCheckpointContext", "staged_checkpoint_contexts"]

# Tabular checkpoint shape validation
def contains_nonempty_list(value: object) -> bool:
    if isinstance(value, list):
        return bool(value) and any(list_item_has_data(item) for item in value)
    if isinstance(value, dict):
        return any(contains_nonempty_list(item) for item in value.values())
    return False


def tabular_json_shape_issue(
    value: object,
    *,
    required_columns: list[str] | None = None,
    required_sheets_min: int = 0,
) -> dict[str, str]:
    required = required_columns or []
    if not _needs_table_validation(value, required, required_sheets_min):
        return {}
    sheets = _tabular_sheet_candidates(value, required_columns=required)
    if not sheets:
        return {"code": "STAGED_JSON_NO_ROWS"}
    if count_issue := _sheet_count_issue(sheets, required_sheets_min):
        return count_issue
    return _first_sheet_shape_issue(sheets, required)


def _needs_table_validation(value: object, required_columns: list[str], required_sheets_min: int) -> bool:
    return bool(required_columns or required_sheets_min or (isinstance(value, dict) and "sheets" in value))


def _sheet_count_issue(sheets: list[dict[str, object]], required_sheets_min: int) -> dict[str, str]:
    if required_sheets_min <= 0 or len(sheets) >= required_sheets_min:
        return {}
    return {
        "code": "STAGED_JSON_TOO_FEW_SHEETS",
        "sheet_count": str(len(sheets)),
        "required_sheets_min": str(required_sheets_min),
    }


def _first_sheet_shape_issue(sheets: list[dict[str, object]], required_columns: list[str]) -> dict[str, str]:
    seen_names: set[str] = set()
    for index, sheet in enumerate(sheets):
        if name_issue := _sheet_name_issue(sheet, index, seen_names):
            return name_issue
        rows = sheet.get("rows")
        if not isinstance(rows, list) or not any(list_item_has_data(row) for row in rows):
            return {"code": "STAGED_JSON_NO_ROWS", "sheet_name": str(sheet.get("name") or "")}
        if columns_issue := _sheet_columns_issue(sheet, rows, index, required_columns=required_columns):
            return columns_issue
    return {}


def _sheet_name_issue(sheet: dict[str, object], index: int, seen_names: set[str]) -> dict[str, str]:
    name = str(sheet.get("name") or "").strip()
    if not name:
        return _shape_issue("sheet.name", index)
    if name in seen_names:
        return {"code": "STAGED_JSON_DUPLICATE_SHEET_NAMES", "sheet_name": name}
    seen_names.add(name)
    return {}


def _tabular_sheet_candidates(value: object, *, required_columns: list[str]) -> list[dict[str, object]]:
    if isinstance(value, list):
        return [{"name": "Sheet1", "rows": value}] if required_columns else []
    if not isinstance(value, dict):
        return []
    return _dict_sheet_candidates(value, required_columns)


def _dict_sheet_candidates(value: dict[str, object], required_columns: list[str]) -> list[dict[str, object]]:
    sheets = value.get("sheets")
    if isinstance(sheets, list):
        return [item for item in sheets if isinstance(item, dict)]
    if not required_columns:
        return []
    rows = value.get("rows")
    if isinstance(rows, list):
        return [{"name": str(value.get("name") or "Sheet1"), "columns": value.get("columns"), "rows": rows}]
    return [{"name": str(key), "rows": item} for key, item in value.items() if isinstance(item, list) and item]


def _sheet_columns_issue(
    sheet: dict[str, object],
    rows: list[object],
    index: int,
    *,
    required_columns: list[str],
) -> dict[str, str]:
    columns = sheet.get("columns")
    if columns is None and not required_columns:
        return {}
    normalized = _normalized_columns(columns, rows, index)
    if isinstance(normalized, dict):
        return normalized
    if required_issue := _required_columns_issue(normalized, required_columns, index):
        return required_issue
    return _first_row_columns_issue(rows, normalized, required_columns, index)


def _normalized_columns(columns: object, rows: list[object], index: int) -> list[str] | dict[str, str]:
    if columns is not None and (not isinstance(columns, list) or not columns):
        return _shape_issue("sheet.columns", index)
    values = [str(column).strip() for column in columns] if isinstance(columns, list) else _row_columns(rows)
    return _shape_issue("sheet.columns", index) if any(not column for column in values) else values


def _required_columns_issue(columns: list[str], required_columns: list[str], index: int) -> dict[str, str]:
    missing_required = [column for column in required_columns if column not in columns]
    if not missing_required:
        return {}
    return {
        "code": "STAGED_JSON_REQUIRED_COLUMNS_MISSING",
        "sheet_index": str(index),
        "missing_columns": ",".join(missing_required),
    }


def _first_row_columns_issue(
    rows: list[object],
    columns: list[str],
    required_columns: list[str],
    sheet_index: int,
) -> dict[str, str]:
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            return _shape_issue("sheet.rows", sheet_index, row_index=row_index)
        if missing := [column for column in columns if column not in row]:
            return _row_issue("STAGED_JSON_TABLE_SHAPE_INVALID", sheet_index, row_index, {"missing_columns": missing})
        if empty := [column for column in required_columns if column in columns and not _cell_value_present(row.get(column))]:
            return _row_issue("STAGED_JSON_REQUIRED_COLUMN_EMPTY_VALUES", sheet_index, row_index, {"empty_columns": empty})
    return {}


def _row_issue(code: str, sheet_index: int, row_index: int, fields: dict[str, list[str]]) -> dict[str, str]:
    issue = {"code": code, "sheet_index": str(sheet_index), "row_index": str(row_index)}
    issue.update({key: ",".join(value) for key, value in fields.items()})
    return issue


def _cell_value_present(value: object) -> bool:
    if value is None:
        return False
    return bool(str(value).strip())


def _row_columns(rows: list[object]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            columns.extend(str(key) for key in row if str(key) not in columns)
    return columns


def _shape_issue(field: str, index: int, *, row_index: int | None = None) -> dict[str, str]:
    issue = {
        "code": "STAGED_JSON_TABLE_SHAPE_INVALID",
        "field": field,
        "sheet_index": str(index),
    }
    if row_index is not None:
        issue["row_index"] = str(row_index)
    return issue


def list_item_has_data(item: object) -> bool:
    if isinstance(item, list):
        return bool(item) and any(list_item_has_data(child) for child in item)
    if isinstance(item, dict):
        nested_values = [value for value in item.values() if isinstance(value, (list, dict))]
        if nested_values:
            return any(contains_nonempty_list(value) for value in nested_values)
        return any(_scalar_has_data(value) for value in item.values())
    return _scalar_has_data(item)


def _scalar_has_data(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


__all__ = ["contains_nonempty_list", "tabular_json_shape_issue"]

# Staged checkpoint file loading
import json
from pathlib import Path


def artifact_path(ref: str, task_workspace: Path) -> Path:
    preferred = Path(str(ref or ""))
    path = preferred.resolve(strict=False) if preferred.is_absolute() else (task_workspace / preferred).resolve(strict=False)
    try:
        path.relative_to(task_workspace.resolve(strict=False))
    except ValueError as exc:
        raise ValueError("staged artifact path outside task workspace") from exc
    return path


def json_checkpoint_status(
    path: Path,
    required_columns: list[str] | None = None,
    required_sheets_min: int = 0,
) -> dict[str, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return {"code": "STAGED_JSON_INVALID", "parse_error": str(exc)}
    except json.JSONDecodeError as exc:
        return {"code": "STAGED_JSON_INVALID", "parse_error": str(exc)}
    if not contains_nonempty_list(value):
        return {"code": "STAGED_JSON_NO_ROWS"}
    if shape_issue := tabular_json_shape_issue(
        value,
        required_columns=required_columns,
        required_sheets_min=required_sheets_min,
    ):
        return shape_issue
    return {"code": "OK"}


__all__ = ["artifact_path", "json_checkpoint_status"]

# Staged checkpoint evidence payloads
from typing import Any

from ..common.value_parsing import sequence_strings
from .evidence_contract import EvidenceClaim, EvidenceSourceRef


def source_refs(value: object) -> list[EvidenceSourceRef]:
    if not isinstance(value, list):
        return []
    refs: list[EvidenceSourceRef] = []
    for item in value:
        if isinstance(item, dict):
            refs.append(_source_ref(item))
    return refs


def claims(value: object) -> list[EvidenceClaim]:
    if not isinstance(value, list):
        return []
    return [_claim(index, item) for index, item in enumerate(value) if isinstance(item, dict)]


def _source_ref(item: dict[str, Any]) -> EvidenceSourceRef:
    return EvidenceSourceRef(
        source_id=str(item.get("source_id") or ""),
        source_type=str(item.get("source_type") or ""),
        uri=str(item.get("uri") or ""),
        retrieved_at=str(item.get("retrieved_at") or ""),
        artifact_ref=str(item.get("artifact_ref") or ""),
        content_sha256=str(item.get("content_sha256") or ""),
        status=str(item.get("status") or "AVAILABLE"),
        tool_call_ref=str(item.get("tool_call_ref") or ""),
        tool_call_id=str(item.get("tool_call_id") or ""),
        operation_id=str(item.get("operation_id") or ""),
        tool_result_id=str(item.get("tool_result_id") or ""),
        http_status=_optional_int(item.get("http_status")),
        metric_kind=str(item.get("metric_kind") or ""),
        window_start=str(item.get("window_start") or ""),
        window_end=str(item.get("window_end") or ""),
        time_window=dict(item.get("time_window")) if isinstance(item.get("time_window"), dict) else {},
    )


def _claim(index: int, item: dict[str, Any]) -> EvidenceClaim:
    return EvidenceClaim(
        claim_id=str(item.get("claim_id") or f"claim-{index}"),
        field=str(item.get("field") or ""),
        value=item.get("value"),
        source_ids=sequence_strings(item.get("source_ids")),
        confidence=float(item.get("confidence", 1.0) or 0.0),
        verification_status=str(item.get("verification_status") or "VERIFIED"),
        value_type=str(item.get("value_type") or "exact"),
        methodology=str(item.get("methodology") or ""),
        metric_kind=str(item.get("metric_kind") or ""),
        observed_metric_kind=str(item.get("observed_metric_kind") or ""),
        window_start=str(item.get("window_start") or ""),
        window_end=str(item.get("window_end") or ""),
        time_window=dict(item.get("time_window")) if isinstance(item.get("time_window"), dict) else {},
        limitations=item.get("limitations") or "",
        uncertainty_notes=item.get("uncertainty_notes") or "",
        item_path=str(item.get("item_path") or ""),
        item_key=dict(item.get("item_key")) if isinstance(item.get("item_key"), dict) else {},
        item_index=_optional_int(item.get("item_index")),
        group_index=_optional_int(item.get("group_index")),
        group_name=str(item.get("group_name") or ""),
    )


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


__all__ = ["claims", "source_refs"]

# Staged checkpoint acceptance
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