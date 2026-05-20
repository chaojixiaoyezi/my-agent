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
from .artifact_html_refs import image_ref_findings, scan_html_refs
from .artifact_static_site_contract import validate_static_site_artifact
from .artifact_validator_registry import (
    ArtifactValidator,
    resolve_artifact_validator,
)
from .artifact_xlsx_contract import xlsx_contract_findings
from .staged_checkpoint_acceptance import staged_json_evidence_findings


# LLM: validate_html_artifact performs generic HTML checks that model self-reports often miss.
# 函数用途: 验收 HTML 产物里的结构完整性、图片引用和合同声明的资源规则，返回结构化 findings。
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
    if not path.exists():
        return _missing_report(path, kind=kind_for_path(path))
    validator = resolve_artifact_validator(
        request,
        named_validators=_named_validators(),
        kind_validators=_kind_validators(),
        fallback=_validate_generic_request,
    )
    return validator(request)


# LLM: _named_validators keeps explicit validator selectors registered in one place.
# 函数用途: 返回按 validation_contract.validator 映射的验收器，避免 validate_artifact 继续写死分支。
def _named_validators() -> dict[str, ArtifactValidator]:
    return {
        "artifact_acceptance": validate_by_artifact_kind,
        "static_site_check": validate_static_site_artifact,
        "spreadsheet_acceptance": _validate_xlsx_request,
        "document_acceptance": _validate_pdf_request,
    }


# LLM: _kind_validators keeps default per-kind validators registered separately from contract names.
# 函数用途: 返回按 artifact kind 映射的默认验收器，让 html/xlsx/pdf 只是插件项。
def _kind_validators() -> dict[str, ArtifactValidator]:
    return {
        "html": validate_html_artifact,
        "htm": validate_html_artifact,
        "json": _validate_json_request,
        "csv": _validate_csv_request,
        "xlsx": _validate_xlsx_request,
        "pdf": _validate_pdf_request,
    }


# LLM: validate_by_artifact_kind routes through registered kind validators when the contract stays generic.
# 函数用途: 在 validator=artifact_acceptance 时仍按产物类型选默认验收器，不让上层关心具体格式。
def validate_by_artifact_kind(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    path = Path(request.path)
    return _kind_validators().get(kind_for_path(path), _validate_generic_request)(request)


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


# LLM: _validate_json_request adapts the path-based validator to the shared request shape.
# 函数用途: 保持注册表只处理 ArtifactAcceptanceRequest，不暴露内部 path-only helper。
def _validate_json_request(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    return _validate_json(Path(request.path))


# LLM: _validate_csv_request adapts the path-based validator to the shared request shape.
# 函数用途: 保持注册表只处理 ArtifactAcceptanceRequest，不暴露内部 path-only helper。
def _validate_csv_request(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    return _validate_csv(Path(request.path))


# LLM: _validate_xlsx_request passes validation_contract through the registry entrypoint.
# 函数用途: 让 xlsx 验收既可按后缀触发，也可按 validator 名称触发。
def _validate_xlsx_request(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    return _validate_xlsx(Path(request.path), request.validation_contract, workspace_root=request.workspace_root)


# LLM: _validate_pdf_request adapts the path-based validator to the shared request shape.
# 函数用途: 保持注册表只处理 ArtifactAcceptanceRequest，不暴露内部 path-only helper。
def _validate_pdf_request(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    return _validate_pdf(Path(request.path))


# LLM: _validate_generic_request keeps unknown artifact kinds on the generic fallback path.
# 函数用途: 对未注册的后缀或 validator 统一走最小存在性检查。
def _validate_generic_request(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    return _validate_generic(Path(request.path))


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
def _validate_xlsx(
    path: Path,
    validation_contract: dict[str, object] | None = None,
    *,
    workspace_root: Path | None = None,
) -> ArtifactAcceptanceReport:
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
    findings = [
        *xlsx_contract_findings(path, validation_contract),
        *_staged_source_evidence_findings(validation_contract or {}, workspace_root or path.parent),
    ]
    return ArtifactAcceptanceReport(
        ok=not any(item.severity == "hard" for item in findings),
        artifact_ref=str(path),
        artifact_kind="xlsx",
        findings=findings,
    )


# LLM: _staged_source_evidence_findings ties final workbooks back to source JSON evidence contracts.
# 函数用途: 如果 validation_contract 声明了 evidence_contract，则最终 xlsx 也必须继承 source_data.json 的来源证据验收。
def _staged_source_evidence_findings(
    validation_contract: dict[str, object],
    workspace_root: Path,
) -> list[ArtifactFinding]:
    evidence_contract = validation_contract.get("evidence_contract")
    staging = validation_contract.get("staging_contract")
    source_ref = staging.get("source_json_ref") if isinstance(staging, dict) else ""
    if not isinstance(evidence_contract, dict) or not source_ref:
        return []
    return _evidence_finding_records(
        staged_json_evidence_findings(str(source_ref), workspace_root, evidence_contract)
    )


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


# LLM: _evidence_finding_records adapts evidence-contract helper findings into public ArtifactFinding records.
# 函数用途: 把阶段证据校验的扩展字段折叠进统一 ArtifactFinding 结构，保持公开报告格式稳定。
def _evidence_finding_records(items: list[dict[str, object]]) -> list[ArtifactFinding]:
    records: list[ArtifactFinding] = []
    public_keys = {"code", "severity", "message", "location", "value"}
    for item in items:
        details = {key: value for key, value in item.items() if key not in public_keys}
        value = item.get("value")
        if value is None and details:
            value = json.dumps(details, ensure_ascii=False, sort_keys=True)
        records.append(
            ArtifactFinding(
                code=str(item.get("code") or ""),
                severity=str(item.get("severity") or "hard"),
                message=str(item.get("message") or ""),
                location=str(item.get("location") or ""),
                value=str(value or ""),
            )
        )
    return records


__all__ = [
    "ArtifactAcceptanceReport",
    "ArtifactAcceptanceRequest",
    "ArtifactFinding",
    "artifact_ref_payload",
    "validate_artifact",
    "validate_html_artifact",
]
