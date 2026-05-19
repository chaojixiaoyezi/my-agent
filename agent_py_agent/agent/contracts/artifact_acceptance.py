# LLM: Artifact acceptance validators turn model self-checks into machine-verifiable findings.
# 模块用途: 验收 HTML 等产物的常见质量问题，给真实 E2E、QA 和修复流程提供结构化 findings。

from __future__ import annotations

import csv
import json
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from .artifact_acceptance_models import (
    ArtifactAcceptanceReport,
    ArtifactAcceptanceRequest,
    ArtifactFinding,
    artifact_ref_payload,
    kind_for_path,
)
from .artifact_html_contract import html_contract_findings, record_resource_ref
from .artifact_html_refs import image_ref_findings, placeholder_link_findings, scan_html_refs
from .artifact_static_site_contract import validate_static_site_artifact


# LLM: validate_html_artifact performs generic HTML checks that model self-reports often miss.
# 函数用途: 验收 HTML 产物里的占位链接、外部图片引用和缺失本地图片，返回结构化 findings。
def validate_html_artifact(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    path = Path(request.path)
    if not path.exists():
        finding = ArtifactFinding(
            code="ARTIFACT_MISSING",
            severity="hard",
            message="HTML artifact does not exist.",
            location=str(path),
        )
        return ArtifactAcceptanceReport(ok=False, artifact_ref=str(path), findings=[finding])
    text = path.read_text(encoding="utf-8", errors="replace")
    refs = scan_html_refs(text)
    findings = _finding_records(
        [
            *placeholder_link_findings(refs),
            *image_ref_findings(refs, path=path, workspace_root=request.workspace_root),
            *html_contract_findings(text, refs.resources, request.validation_contract),
        ]
    )
    return ArtifactAcceptanceReport(
        ok=not any(item.severity == "hard" for item in findings),
        artifact_ref=str(path),
        artifact_kind="html",
        findings=findings,
    )


# LLM: validate_artifact is the public dispatcher for artifact QA across formats.
# 函数用途: 根据文件后缀选择 HTML/JSON/CSV/XLSX/PDF/通用验收器，统一返回结构化 findings。
def validate_artifact(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    path = Path(request.path)
    if _validator_name(request.validation_contract) == "static_site_check":
        return validate_static_site_artifact(request)
    if path.suffix.lower() in {".html", ".htm"}:
        return validate_html_artifact(request)
    if not path.exists():
        return _missing_report(path, kind=kind_for_path(path))
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _validate_json(path)
    if suffix == ".csv":
        return _validate_csv(path)
    if suffix == ".xlsx":
        return _validate_xlsx(path)
    if suffix == ".pdf":
        return _validate_pdf(path)
    return _validate_generic(path)


# LLM: _validator_name reads the validator selector from validation_contract only.
# 函数用途: 获取结构化 validator 名称；普通 prompt 文本不会参与产物验收路由。
def _validator_name(validation_contract: dict[str, object] | None) -> str:
    value = (validation_contract or {}).get("validator")
    return str(value or "").strip().lower()


# LLM: _missing_report preserves one missing-file shape for every validator.
# 函数用途: 产物不存在时生成稳定 ARTIFACT_MISSING finding，供修复流程识别。
def _missing_report(path: Path, *, kind: str) -> ArtifactAcceptanceReport:
    finding = ArtifactFinding(
        code="ARTIFACT_MISSING",
        severity="hard",
        message="Artifact does not exist.",
        location=str(path),
    )
    return ArtifactAcceptanceReport(ok=False, artifact_ref=str(path), artifact_kind=kind, findings=[finding])


# LLM: _validate_json checks machine-readable reports before downstream agents trust them.
# 函数用途: 验证 JSON 产物可解析且顶层是对象或数组。
def _validate_json(path: Path) -> ArtifactAcceptanceReport:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        finding = ArtifactFinding(
            code="JSON_INVALID",
            severity="hard",
            message=f"Invalid JSON: {exc.msg}",
            location=str(exc.pos),
        )
        return _report_with_finding(path, "json", finding)
    if not isinstance(value, (dict, list)):
        finding = ArtifactFinding(
            code="JSON_UNEXPECTED_TOP_LEVEL",
            severity="hard",
            message="JSON top-level must be object or array.",
        )
        return _report_with_finding(path, "json", finding)
    return ArtifactAcceptanceReport(ok=True, artifact_ref=str(path), artifact_kind="json")


# LLM: _validate_csv ensures table-like outputs have at least a header and one data row.
# 函数用途: 验证 CSV 能被标准库解析，并且不是空表。
def _validate_csv(path: Path) -> ArtifactAcceptanceReport:
    try:
        rows = list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines()))
    except csv.Error as exc:
        finding = ArtifactFinding(code="CSV_INVALID", severity="hard", message=f"Invalid CSV: {exc}")
        return _report_with_finding(path, "csv", finding)
    if len(rows) < 2 or not any(cell.strip() for cell in rows[0]):
        finding = ArtifactFinding(
            code="CSV_EMPTY_OR_HEADERLESS",
            severity="hard",
            message="CSV must include a header and data row.",
        )
        return _report_with_finding(path, "csv", finding)
    return ArtifactAcceptanceReport(ok=True, artifact_ref=str(path), artifact_kind="csv")


# LLM: _validate_xlsx performs a lightweight workbook integrity check without new dependencies.
# 函数用途: 验证 xlsx 是可打开的 zip 工作簿，并且至少包含 workbook 和 worksheet 文件。
def _validate_xlsx(path: Path) -> ArtifactAcceptanceReport:
    try:
        with ZipFile(path) as workbook:
            names = set(workbook.namelist())
    except (BadZipFile, OSError) as exc:
        finding = ArtifactFinding(code="XLSX_INVALID", severity="hard", message=f"Invalid XLSX package: {exc}")
        return _report_with_finding(path, "xlsx", finding)
    if "xl/workbook.xml" not in names or not any(name.startswith("xl/worksheets/") for name in names):
        finding = ArtifactFinding(
            code="XLSX_MISSING_WORKBOOK_PARTS",
            severity="hard",
            message="XLSX lacks workbook or worksheet parts.",
        )
        return _report_with_finding(path, "xlsx", finding)
    return ArtifactAcceptanceReport(ok=True, artifact_ref=str(path), artifact_kind="xlsx")


# LLM: _validate_pdf catches obviously corrupt PDF deliverables before human review.
# 函数用途: 用轻量文件签名检查 PDF，不替代后续更强的渲染验收。
def _validate_pdf(path: Path) -> ArtifactAcceptanceReport:
    data = path.read_bytes()
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
        finding = ArtifactFinding(
            code="PDF_INVALID_SIGNATURE",
            severity="hard",
            message="PDF is missing %PDF header or EOF marker.",
        )
        return _report_with_finding(path, "pdf", finding)
    return ArtifactAcceptanceReport(ok=True, artifact_ref=str(path), artifact_kind="pdf")


# LLM: _validate_generic keeps unknown artifact types from passing when empty or missing.
# 函数用途: 对未知格式至少检查存在和非空，后续格式可以继续注册专门验收器。
def _validate_generic(path: Path) -> ArtifactAcceptanceReport:
    if path.stat().st_size <= 0:
        finding = ArtifactFinding(code="ARTIFACT_EMPTY", severity="hard", message="Artifact is empty.")
        return _report_with_finding(path, kind_for_path(path), finding)
    return ArtifactAcceptanceReport(ok=True, artifact_ref=str(path), artifact_kind=kind_for_path(path))


# LLM: _report_with_finding avoids repeating one-error report construction in validators.
# 函数用途: 构造只有一个 hard finding 的验收报告。
def _report_with_finding(path: Path, kind: str, finding: ArtifactFinding) -> ArtifactAcceptanceReport:
    return ArtifactAcceptanceReport(ok=False, artifact_ref=str(path), artifact_kind=kind, findings=[finding])


# LLM: _finding_records converts JSON-shaped helper findings into public report objects.
# 函数用途: 保持专门校验模块独立，同时让公开报告继续使用统一 ArtifactFinding 类型。
def _finding_records(items: list[dict[str, str]]) -> list[ArtifactFinding]:
    return [ArtifactFinding(**item) for item in items]


__all__ = [
    "ArtifactAcceptanceReport",
    "ArtifactAcceptanceRequest",
    "ArtifactFinding",
    "artifact_ref_payload",
    "validate_artifact",
    "validate_html_artifact",
]
