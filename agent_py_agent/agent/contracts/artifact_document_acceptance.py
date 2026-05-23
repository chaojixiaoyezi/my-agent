# LLM: Document artifact acceptance bridges file-format integrity and document-quality contracts.
# 模块用途: 放置 DOCX/TXT 和 document_quality_contract 验收适配，避免主 artifact 分发器膨胀。

from __future__ import annotations

import json
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from .artifact_acceptance_models import ArtifactAcceptanceReport, ArtifactFinding
from .gates.document_content_quality import document_content_quality_findings


# LLM: validate_docx_artifact checks Word package integrity before content-quality gates run.
# 函数用途: DOCX 必须能打开且包含 word/document.xml，随后按可选机器合同检查内容厚度。
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
        ok=not any(item.severity == "hard" for item in findings),
        artifact_ref=str(path),
        artifact_kind="docx",
        findings=findings,
    )


# LLM: validate_text_artifact gives plain text the same explicit document-quality contract path.
# 函数用途: TXT 至少检查非空，若声明 document_quality_contract 则执行通用内容门。
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
        ok=not any(item.severity == "hard" for item in findings),
        artifact_ref=str(path),
        artifact_kind="txt",
        findings=findings,
    )


# LLM: document_quality_artifact_findings converts gate findings into artifact acceptance findings.
# 函数用途: 让 Markdown/PDF/TXT/DOCX 共用同一个 document_quality_contract 适配层。
def document_quality_artifact_findings(
    path: Path,
    validation_contract: dict[str, object] | None,
    *,
    workspace_root: Path,
) -> list[ArtifactFinding]:
    contract = _document_quality_contract(validation_contract)
    if not contract:
        return []
    return [
        ArtifactFinding(
            code=finding.code,
            severity="hard",
            message=finding.message or "Document quality contract failed.",
            location=_finding_location(finding.evidence),
            value=json.dumps(finding.evidence, ensure_ascii=False, sort_keys=True),
        )
        for finding in document_content_quality_findings(path, workspace_root=workspace_root, contract=contract)
    ]


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
