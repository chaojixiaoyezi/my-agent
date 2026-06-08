
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...artifacts.registry import (
    REGISTRY_RELATIVE_PATH,
    ArtifactRegistration,
    ArtifactRegistryRecord,
    register_artifact,
    registry_path,
    resolve_artifact_record_report,
)
from ...contracts.artifact_acceptance import (
    ArtifactAcceptanceRequest,
    validate_artifact,
    validation_workspace_root_for_item,
)
from ...contracts.gates import artifact_provenance_from_archive
from ...contracts.staged_checkpoint import staged_checkpoint_findings
from .._runtime_params import ToolLoopExecuteParams
from ..artifact_locator import locate_artifact
from ..target_coverage_ledger import collect_target_coverage_records, target_coverage_status
from .groups import (
    ArtifactGroupValidationRequest,
    _artifact_path,
    validate_artifact_group_record,
)

CLOSEOUT_DIR = ".agent_delivery"
CLOSEOUT_REPORT = "closeout.json"


@dataclass(frozen=True)
class DeliveryContractValidationRequest:
    contract: dict[str, Any]
    artifacts: list[dict[str, Any]]
    workspace_root: Path
    params: ToolLoopExecuteParams
    archive_tool_calls: list[Any] | None = None
    coverage_workspace_root: Path | None = None


@dataclass(frozen=True)
class ArtifactPathFailureRequest:
    item: dict[str, Any]
    raw_path: str
    code: str
    workspace_root: Path | None = None
    locator_findings: list[dict[str, object]] | None = None
    registry_read_errors: list[dict[str, object]] | None = None


@dataclass(frozen=True)
class ArtifactValidationReportRequest:
    item: dict[str, Any]
    path: Path
    workspace_root: Path
    reference_roots: tuple[Path, ...]
    registry_record: ArtifactRegistryRecord | None
    registry_read_errors: list[dict[str, object]]
    archive_tool_calls: list[Any]
    run_id: str


@dataclass(frozen=True)
class ArtifactItemValidationRequest:
    item: dict[str, Any]
    workspace_root: Path
    archive_tool_calls: list[Any]
    run_id: str
    reference_roots: tuple[Path, ...]


@dataclass(frozen=True)
class TargetCoverageReportRequest:
    report: dict[str, Any]
    validation: DeliveryContractValidationRequest
    archive_tool_calls: list[Any]
    results: list[dict[str, Any]]


def _required_artifacts(contract: dict[str, Any]) -> list[dict[str, Any]]:
    raw = contract.get("artifacts")
    if not isinstance(raw, list):
        return []
    return [
        item
        for item in raw
        if isinstance(item, dict)
        and item.get("required") is not False
        and not _artifact_declares_input_role(item)
    ]


def _artifact_declares_input_role(item: dict[str, Any]) -> bool:
    roles = [
        str(item.get(key) or "").strip().lower()
        for key in ("artifact_role", "role")
        if str(item.get(key) or "").strip()
    ]
    intent = item.get("artifact_intent")
    if isinstance(intent, dict):
        roles.extend(
            str(intent.get(key) or "").strip().lower()
            for key in ("artifact_role", "role")
            if str(intent.get(key) or "").strip()
        )
    return any(role in _INPUT_ARTIFACT_ROLES for role in roles)


_INPUT_ARTIFACT_ROLES = frozenset({"input", "source", "reference", "read_only", "evidence", "lookup", "search"})


def _validate_contract_artifacts(request: DeliveryContractValidationRequest) -> dict[str, Any]:
    archive_tool_calls = _request_archive_tool_calls(request)
    reference_roots = _artifact_reference_roots(request)
    results = [
        _validate_artifact_item(
            ArtifactItemValidationRequest(
                item=item,
                workspace_root=request.workspace_root,
                archive_tool_calls=archive_tool_calls,
                run_id=str(getattr(request.params, "run_id", "") or ""),
                reference_roots=reference_roots,
            )
        )
        for item in request.artifacts
    ]
    report = {
        "schema_version": "main_agent_delivery_closeout.v1",
        "ok": all(item["ok"] for item in results),
        "case_id": str(request.contract.get("case_id") or ""),
        "request_id": request.params.request_id,
        "run_id": request.params.run_id,
        "task_id": request.params.task_id,
        "workspace_root": str(request.workspace_root),
        "canonical_artifact_registry_ref": _relative_report_ref(
            registry_path(request.workspace_root),
            request.workspace_root,
        ),
        "artifacts": results,
    }
    registry_read_errors = registry_read_errors_from_artifacts(results)
    if registry_read_errors:
        report["registry_read_errors"] = registry_read_errors
    _apply_target_coverage_report(
        TargetCoverageReportRequest(
            report=report,
            validation=request,
            archive_tool_calls=archive_tool_calls,
            results=results,
        )
    )
    return report


def _apply_target_coverage_report(request: TargetCoverageReportRequest) -> None:
    report = request.report
    validation = request.validation
    coverage_contract = validation.contract.get("target_coverage_contract")
    if not isinstance(coverage_contract, dict):
        return
    coverage_root = validation.coverage_workspace_root or validation.workspace_root
    coverage_records = collect_target_coverage_records(
        [*request.archive_tool_calls, *request.results],
        workspace_root=coverage_root,
    )
    report["target_coverage_status"] = target_coverage_status(
        coverage_contract,
        coverage_records=coverage_records,
        workspace_root=coverage_root,
    )
    freshness = _target_coverage_freshness_status(request.results, report["target_coverage_status"], coverage_records)
    if not freshness:
        return
    report["target_coverage_freshness_status"] = freshness
    if freshness.get("should_block") is True:
        report["ok"] = False


def _target_coverage_freshness_status(
    artifacts: list[dict[str, Any]],
    coverage_status: dict[str, object],
    coverage_records: list[dict[str, object]],
) -> dict[str, object]:
    if coverage_status.get("should_block") is True:
        return {}
    if str(coverage_status.get("enforcement") or "").strip() != "required":
        return {}
    if int(coverage_status.get("expected_count") or 0) <= 0:
        return {}
    latest_coverage = _latest_required_coverage_created_at(coverage_records)
    artifact_rows = _artifact_created_at_rows(artifacts)
    if latest_coverage is None or not artifact_rows:
        return {
            "checked": False,
            "should_block": False,
            "reason": "created_at_evidence_missing",
        }
    stale = [
        {
            "artifact_id": row["artifact_id"],
            "path": row["path"],
            "artifact_created_at": _isoformat_utc(row["created_at"]),
        }
        for row in artifact_rows
        if row["created_at"] < latest_coverage
    ]
    return {
        "checked": True,
        "should_block": bool(stale),
        "latest_required_coverage_created_at": _isoformat_utc(latest_coverage),
        "stale_artifacts": stale,
        "recommended_next_action": (
            "update_final_artifacts_after_latest_required_coverage_then_submit"
            if stale
            else "submit_or_finish"
        ),
    }


def _latest_required_coverage_created_at(records: list[dict[str, object]]) -> datetime | None:
    values = [
        parsed
        for record in records
        if str(record.get("tool") or "").strip() == "read_file"
        and str(record.get("status") or "").strip() == "covered"
        if (parsed := _parse_created_at(record.get("created_at"))) is not None
    ]
    return max(values) if values else None


def _artifact_created_at_rows(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in artifacts:
        if item.get("ok") is not True:
            continue
        provenance = item.get("provenance")
        if not isinstance(provenance, dict):
            continue
        created_at = _parse_created_at(provenance.get("created_at"))
        if created_at is None:
            continue
        rows.append({
            "artifact_id": str(item.get("artifact_id") or ""),
            "path": str(item.get("path") or ""),
            "created_at": created_at,
        })
    return rows


def _parse_created_at(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _request_archive_tool_calls(request: DeliveryContractValidationRequest) -> list[Any]:
    if request.archive_tool_calls is not None:
        return list(request.archive_tool_calls)
    return list(getattr(request.params, "archive_tool_calls", []) or [])


def _artifact_reference_roots(request: DeliveryContractValidationRequest) -> tuple[Path, ...]:
    roots: list[Path] = []
    for value in _artifact_reference_root_values(request):
        _append_reference_root(roots, value)
    return tuple(roots)


def _artifact_reference_root_values(request: DeliveryContractValidationRequest) -> list[object]:
    values: list[object] = [request.coverage_workspace_root]
    task_workspace = request.contract.get("task_workspace")
    if isinstance(task_workspace, dict):
        for key in ("source_workspace_root", "relative_input_root"):
            values.append(task_workspace.get(key))
    for key in ("source_roots", "reference_roots"):
        raw_values = request.contract.get(key)
        if isinstance(raw_values, list):
            values.extend(raw_values)
    return values


def _append_reference_root(roots: list[Path], value: object) -> None:
    text = str(value or "").strip()
    if not text:
        return
    try:
        root = Path(text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return
    if root.exists() and root not in roots:
        roots.append(root)


def registry_read_errors_from_artifacts(results: list[dict[str, Any]]) -> list[dict[str, object]]:
    errors: list[dict[str, object]] = []
    for item in results:
        value = item.get("registry_read_errors")
        if isinstance(value, list):
            errors.extend(error for error in value if isinstance(error, dict))
    return errors


def with_registry_read_errors(
    artifact_report: dict[str, Any],
    errors: list[dict[str, object]],
) -> dict[str, Any]:
    if not errors:
        return artifact_report
    updated = dict(artifact_report)
    updated["registry_read_errors"] = errors
    acceptance = updated.get("acceptance_report")
    if isinstance(acceptance, dict):
        updated["acceptance_report"] = {**acceptance, "registry_read_errors": errors}
    return updated


def _existing_report(workspace_root: Path) -> dict[str, Any]:
    path = workspace_root / CLOSEOUT_DIR / CLOSEOUT_REPORT
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _validate_artifact_item(request: ArtifactItemValidationRequest) -> dict[str, Any]:
    item = request.item
    workspace_root = request.workspace_root
    raw_path = str(item.get("preferred_path") or item.get("path") or "")
    registry_record, registry_read_errors = _registry_record_for_item(item, workspace_root)
    if registry_record and _is_registry_group(registry_record):
        result = validate_artifact_group_record(
            ArtifactGroupValidationRequest(
                item=item,
                record=registry_record,
                workspace_root=workspace_root,
                archive_tool_calls=request.archive_tool_calls,
                run_id=request.run_id,
            )
        )
        return with_registry_read_errors(result, registry_read_errors)
    located = None if registry_record else locate_artifact(item, workspace_root)
    path = Path(registry_record.path) if registry_record else located.path if located else None
    if path is None:
        return _path_failure(
            ArtifactPathFailureRequest(
                item=item,
                raw_path=raw_path,
                code=str(located.findings[0]["code"]) if located and located.findings else "ARTIFACT_PATH_INVALID",
                workspace_root=workspace_root,
                locator_findings=located.findings if located else [],
                registry_read_errors=registry_read_errors,
            )
        )
    return _validated_artifact_from_path(
        ArtifactValidationReportRequest(
            item=item,
            path=path,
            workspace_root=workspace_root,
            reference_roots=request.reference_roots,
            registry_record=registry_record,
            registry_read_errors=registry_read_errors,
            archive_tool_calls=request.archive_tool_calls,
            run_id=request.run_id,
        )
    )


def _validated_artifact_from_path(request: ArtifactValidationReportRequest) -> dict[str, Any]:
    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=request.path,
            workspace_root=validation_workspace_root_for_item(request.item, request.path, request.workspace_root),
            validation_contract=_validation_contract(request.item),
            reference_roots=request.reference_roots,
        )
    ).to_dict()
    report = with_staged_checkpoint_findings(report, request.item, request.workspace_root)
    if partial_finding := _latest_partial_write_finding(request.path, request.archive_tool_calls, request.workspace_root):
        findings = report.get("findings")
        merged_findings = list(findings) if isinstance(findings, list) else []
        merged_findings.append(partial_finding)
        report = {**report, "ok": False, "findings": merged_findings}
    registry_record = request.registry_record
    registered = register_artifact(
        ArtifactRegistration(
            workspace_root=request.workspace_root,
            path=request.path,
            artifact_id=str(request.item.get("artifact_id") or (registry_record.artifact_id if registry_record else "")),
            run_id=request.run_id,
            task_id=str(request.item.get("task_id") or ""),
            agent_id=str(request.item.get("agent_id") or request.run_id or ""),
            kind=str(request.item.get("kind") or report.get("artifact_kind") or ""),
            source="delivery_closeout",
            created_by_tool="closeout",
            status="ready" if bool(report.get("ok")) else "invalid",
            metadata=_registry_validation_metadata(report),
        )
    )
    artifact = {
        "artifact_id": str(request.item.get("artifact_id") or ""),
        "kind": str(request.item.get("kind") or report.get("artifact_kind") or ""),
        "path": str(request.path),
        "ok": bool(report.get("ok")),
        "acceptance_report": report,
        "registry_ref": registered.to_dict(),
    }
    artifact = with_registry_read_errors(artifact, request.registry_read_errors)
    artifact["provenance"] = artifact_provenance_from_archive(
        artifact,
        request.archive_tool_calls,
        run_id=request.run_id,
        workspace_root=request.workspace_root,
    )
    return artifact


def with_staged_checkpoint_findings(
    report: dict[str, Any],
    item: dict[str, Any],
    workspace_root: Path,
) -> dict[str, Any]:
    staged_findings = staged_checkpoint_findings([item], workspace_root)
    if not staged_findings:
        return report
    findings = report.get("findings")
    merged_findings = list(findings) if isinstance(findings, list) else []
    merged_findings.extend(_public_staged_finding(finding) for finding in staged_findings)
    updated = dict(report)
    updated["findings"] = merged_findings
    updated["ok"] = bool(report.get("ok"))
    return updated


def _public_staged_finding(finding: dict[str, object]) -> dict[str, str]:
    public_keys = {"code", "severity", "message", "location", "value"}
    details = {key: value for key, value in finding.items() if key not in public_keys}
    value = finding.get("value")
    if value is None and details:
        value = json.dumps(details, ensure_ascii=False, sort_keys=True)
    return {
        "code": str(finding.get("code") or ""),
        "severity": "warning",
        "message": str(finding.get("message") or ""),
        "location": str(finding.get("location") or ""),
        "value": str(value or ""),
    }


def _registry_record_for_item(
    item: dict[str, Any],
    workspace_root: Path,
) -> tuple[ArtifactRegistryRecord | None, list[dict[str, object]]]:
    lookup = resolve_artifact_record_report(
        workspace_root,
        str(item.get("artifact_id") or ""),
        path=str(item.get("preferred_path") or item.get("path") or ""),
    )
    record = lookup.record
    if record is None or record.status != "ready":
        return None, list(lookup.errors)
    path = Path(record.path)
    if path.is_file() or _is_registry_group(record):
        return record, list(lookup.errors)
    return None, list(lookup.errors)


def _is_registry_group(record: ArtifactRegistryRecord) -> bool:
    return str(record.metadata.get("artifact_type") or "") == "file_group"


def _path_failure(request: ArtifactPathFailureRequest) -> dict[str, Any]:
    registry_ref = (
        _relative_report_ref(registry_path(request.workspace_root), request.workspace_root)
        if request.workspace_root is not None
        else str(REGISTRY_RELATIVE_PATH)
    )
    finding = {
        "code": request.code,
        "severity": "hard",
        "message": (
            "Artifact was not found in the canonical artifact registry or declared output roots. "
            "Do not create a separate artifact manifest; register or write the real deliverable."
        ),
        "location": request.raw_path,
        "value": request.raw_path,
        "registry_ref": registry_ref,
        "artifact_id": str(request.item.get("artifact_id") or ""),
        "declared_kind": str(request.item.get("kind") or ""),
        "action_zh": (
            "只认统一产物账本 data/artifacts/registry.jsonl。请写入真实交付物，"
            "让写入/生成工具登记到这本账；不要在产物目录或 .agent_delivery 里另写 closeout.json/"
            "artifacts_manifest.json 来冒充完成。如果这个任务本来不需要文件，请使用 message/no_artifact 交付模式。"
        ),
    }
    if request.locator_findings:
        finding["locator_findings"] = request.locator_findings
    if request.registry_read_errors:
        finding["registry_read_errors"] = request.registry_read_errors
    return {
        "artifact_id": str(request.item.get("artifact_id") or ""),
        "kind": str(request.item.get("kind") or ""),
        "path": request.raw_path,
        "ok": False,
        "acceptance_report": {"ok": False, "artifact_ref": request.raw_path, "artifact_kind": "", "findings": [finding]},
    }


def _validation_contract(item: dict[str, Any]) -> dict[str, object]:
    value = item.get("validation_contract")
    return dict(value) if isinstance(value, dict) else {}


def _latest_partial_write_finding(
    path: Path,
    archive_tool_calls: list[Any],
    workspace_root: Path,
) -> dict[str, str]:
    record = _latest_write_record_for_path(path, archive_tool_calls, workspace_root)
    parameters = record.get("parameters") if isinstance(record, dict) else {}
    if isinstance(parameters, dict) and parameters.get("__partial_unclosed_write") is True:
        return {
            "code": "ARTIFACT_LAST_WRITE_PARTIAL_UNCLOSED",
            "severity": "hard",
            "message": "Final artifact path was last written by an incomplete partial write chunk.",
            "location": str(path),
            "value": str(record.get("call_id") or record.get("scoped_call_id") or ""),
            "action_zh": "最终交付物最后一次写入是半截分片；请用完整 write_file 覆盖或补成完整文件后再提交验收。",
        }
    return {}


def _latest_write_record_for_path(
    path: Path,
    archive_tool_calls: list[Any],
    workspace_root: Path,
) -> dict[str, Any]:
    expected = _canonical_artifact_path(path, workspace_root)
    latest: dict[str, Any] = {}
    for record in archive_tool_calls:
        if not isinstance(record, dict):
            continue
        if str(record.get("tool") or record.get("tool_name") or "").strip() != "write_file":
            continue
        parameters = record.get("parameters")
        parameters = parameters if isinstance(parameters, dict) else {}
        raw_path = str(parameters.get("path") or record.get("source_input") or record.get("path") or "").strip()
        if not raw_path:
            continue
        if _canonical_artifact_path(Path(raw_path), workspace_root) == expected:
            latest = record
    return latest


def _canonical_artifact_path(path: Path, workspace_root: Path) -> str:
    try:
        expanded = path.expanduser()
        if not expanded.is_absolute():
            expanded = workspace_root / expanded
        return str(expanded.resolve(strict=False))
    except OSError:
        return str(path)


def _registry_validation_metadata(report: dict[str, Any]) -> dict[str, Any]:
    findings = report.get("findings")
    finding_rows = [item for item in findings if isinstance(item, dict)] if isinstance(findings, list) else []
    return {
        "acceptance_ok": bool(report.get("ok")),
        "finding_codes": [str(item.get("code") or "") for item in finding_rows if item.get("code")],
        "hard_finding_codes": [
            str(item.get("code") or "")
            for item in finding_rows
            if str(item.get("severity") or "") == "hard" and item.get("code")
        ],
    }


def _write_report(workspace_root: Path, report: dict[str, Any]) -> Path:
    path = workspace_root / CLOSEOUT_DIR / CLOSEOUT_REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _relative_report_ref(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)).replace("\\", "/")
    except ValueError:
        return str(path)
