
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...artifacts.registry import (
    ArtifactGroupRegistration,
    ArtifactRegistryRecord,
    register_artifact_group,
)
from ...contracts.artifact_format_lint import lint_artifact_format
from ...contracts.gates import artifact_provenance_from_archive


@dataclass(frozen=True)
class ArtifactGroupValidationRequest:
    item: dict[str, Any]
    record: ArtifactRegistryRecord
    workspace_root: Path
    archive_tool_calls: list[Any] | None = None
    run_id: str = ""


@dataclass(frozen=True)
class GroupArtifactPayloadRequest:
    item: dict[str, Any]
    artifact_ref: str
    valid_paths: list[Path]
    ok: bool
    reports: list[dict[str, Any]]
    findings: list[dict[str, Any]]


def validate_artifact_group_record(request: ArtifactGroupValidationRequest) -> dict[str, Any]:
    raw_paths = _group_member_paths(request.record)
    paths = [_artifact_path(path, request.workspace_root) for path in raw_paths]
    valid_paths = [path for path in paths if path is not None]
    reports = _member_reports(request.item, request.workspace_root, valid_paths)
    findings = _invalid_member_findings(raw_paths, paths)
    hard_reports = [report for report in reports if not bool(report.get("ok"))]
    ok = not findings and not hard_reports and bool(valid_paths)
    registered = register_artifact_group(
        ArtifactGroupRegistration(
            workspace_root=request.workspace_root,
            paths=valid_paths,
            artifact_id=str(request.item.get("artifact_id") or ""),
            run_id=request.run_id,
            task_id=str(request.item.get("task_id") or ""),
            agent_id=str(request.item.get("agent_id") or request.run_id or ""),
            kind="group",
            source="artifact_registry",
            created_by_tool="closeout",
            status="ready" if ok else "invalid",
            metadata=_group_registry_metadata(request.item, reports, findings),
        )
    )
    artifact = _group_artifact_payload(
        GroupArtifactPayloadRequest(request.item, registered.path, valid_paths, ok, reports, findings)
    )
    artifact["registry_ref"] = registered.to_dict()
    artifact["provenance"] = artifact_provenance_from_archive(
        artifact,
        list(request.archive_tool_calls or []),
        run_id=request.run_id,
        workspace_root=request.workspace_root,
    )
    return artifact


def _group_member_paths(record: ArtifactRegistryRecord) -> list[str]:
    members = record.metadata.get("members")
    if not isinstance(members, list):
        return []
    return [
        str(row.get("path") or "").strip()
        for row in members
        if isinstance(row, dict) and str(row.get("path") or "").strip()
    ]


def _member_reports(
    item: dict[str, Any],
    workspace_root: Path,
    valid_paths: list[Path],
) -> list[dict[str, Any]]:
    return [
        lint_artifact_format(
            path=path,
            workspace_root=workspace_root,
            validation_contract=_member_validation_contract(item, path),
        ).to_dict()
        for path in valid_paths
    ]


def _invalid_member_findings(raw_paths: list[str], paths: list[Path | None]) -> list[dict[str, Any]]:
    return [
        {
            "code": "ARTIFACT_GROUP_MEMBER_PATH_INVALID",
            "severity": "hard",
            "message": "Artifact group member path is missing or outside the workspace.",
            "location": raw,
            "value": raw,
        }
        for raw, path in zip(raw_paths, paths, strict=False)
        if path is None
    ]


def _group_artifact_payload(request: GroupArtifactPayloadRequest) -> dict[str, Any]:
    return {
        "artifact_id": str(request.item.get("artifact_id") or ""),
        "kind": "group",
        "path": request.artifact_ref,
        "paths": [str(path) for path in request.valid_paths],
        "ok": request.ok,
        "acceptance_report": {
            "ok": request.ok,
            "artifact_ref": request.artifact_ref,
            "artifact_kind": "group",
            "member_reports": request.reports,
            "findings": [*request.findings, *_group_member_findings(request.reports)],
        },
    }


def _member_validation_contract(item: dict[str, Any], path: Path) -> dict[str, object]:
    contract = _validation_contract(item)
    if "kind" not in contract and path.suffix:
        contract = {**contract, "kind": path.suffix.lower().lstrip(".")}
    return contract


def _group_registry_metadata(
    item: dict[str, Any],
    reports: list[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    member_findings = _group_member_findings(reports)
    return {
        "declared_artifact_id": str(item.get("artifact_id") or ""),
        "member_count": len(reports),
        "acceptance_ok": not findings and not member_findings and bool(reports),
        "finding_codes": [
            str(row.get("code") or "")
            for row in [*findings, *member_findings]
            if row.get("code")
        ],
    }


def _group_member_findings(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(finding)
        for report in reports
        for finding in _finding_rows(report)
    ]


def _finding_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    findings = report.get("findings")
    if not isinstance(findings, list):
        return []
    return [dict(finding) for finding in findings if isinstance(finding, dict)]


def _artifact_path(raw_path: str, workspace_root: Path) -> Path | None:
    if not raw_path:
        return None
    workspace = Path(workspace_root).expanduser().resolve(strict=False)
    candidate = Path(raw_path).expanduser()
    path = candidate.resolve(strict=False) if candidate.is_absolute() else (workspace / candidate).resolve(strict=False)
    try:
        path.relative_to(workspace)
    except ValueError:
        return None
    return path


def _validation_contract(item: dict[str, Any]) -> dict[str, object]:
    value = item.get("validation_contract")
    return dict(value) if isinstance(value, dict) else {}


__all__ = ["ArtifactGroupValidationRequest", "validate_artifact_group_record"]
