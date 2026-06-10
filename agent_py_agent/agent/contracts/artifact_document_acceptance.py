
from __future__ import annotations

import json
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from .artifact_acceptance_models import (
    ArtifactAcceptanceReport,
    ArtifactFinding,
    advisory_artifact_findings,
)
from .gates.document_content import document_content_quality_findings


def validate_docx_artifact(
    path: Path,
    validation_contract: dict[str, object] | None = None,
    *,
    workspace_root: Path | None = None,
) -> ArtifactAcceptanceReport:
    try:
        with ZipFile(path) as docx:
            names = set(docx.namelist())
    except (BadZipFile, OSError) as exc:
        return _report_with_finding(path, "docx", ArtifactFinding("DOCX_INVALID", "hard", f"Invalid DOCX package: {exc}"))
    if "word/document.xml" not in names:
        finding = ArtifactFinding("DOCX_MISSING_DOCUMENT_XML", "hard", "DOCX lacks word/document.xml.")
        return _report_with_finding(path, "docx", finding)
    findings = document_quality_artifact_findings(path, validation_contract, workspace_root=workspace_root or path.parent)
    return ArtifactAcceptanceReport(
        ok=True,
        artifact_ref=str(path),
        artifact_kind="docx",
        findings=findings,
    )


def validate_text_artifact(
    path: Path,
    validation_contract: dict[str, object] | None = None,
    *,
    workspace_root: Path | None = None,
) -> ArtifactAcceptanceReport:
    if path.stat().st_size <= 0:
        return _report_with_finding(path, "txt", ArtifactFinding("ARTIFACT_EMPTY", "hard", "Artifact is empty."))
    findings = document_quality_artifact_findings(path, validation_contract, workspace_root=workspace_root or path.parent)
    return ArtifactAcceptanceReport(
        ok=True,
        artifact_ref=str(path),
        artifact_kind="txt",
        findings=findings,
    )


def document_quality_artifact_findings(
    path: Path,
    validation_contract: dict[str, object] | None,
    *,
    workspace_root: Path,
) -> list[ArtifactFinding]:
    contract = _document_quality_contract(validation_contract)
    if not contract:
        return []
    return advisory_artifact_findings([
        ArtifactFinding(
            code=finding.code,
            severity="hard",
            message=finding.message or "Document quality contract failed.",
            location=_finding_location(finding.evidence),
            value=json.dumps(finding.evidence, ensure_ascii=False, sort_keys=True),
        )
        for finding in document_content_quality_findings(path, workspace_root=workspace_root, contract=contract)
    ])


def _document_quality_contract(validation_contract: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(validation_contract, dict):
        return {}
    value = validation_contract.get("document_quality_contract")
    return dict(value) if isinstance(value, dict) else {}


def _finding_location(evidence: dict[str, object]) -> str:
    location = evidence.get("location")
    if isinstance(location, dict):
        return json.dumps(location, ensure_ascii=False, sort_keys=True)
    return ""


def _report_with_finding(path: Path, kind: str, finding: ArtifactFinding) -> ArtifactAcceptanceReport:
    return ArtifactAcceptanceReport(ok=False, artifact_ref=str(path), artifact_kind=kind, findings=[finding])


__all__ = [
    "document_quality_artifact_findings",
    "validate_docx_artifact",
    "validate_text_artifact",
]
