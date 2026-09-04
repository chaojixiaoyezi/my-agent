
from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from .artifact_acceptance_models import (
    ArtifactAcceptanceReport,
    ArtifactAcceptanceRequest,
    ArtifactFinding,
    advisory_artifact_findings,
    artifact_ref_payload,
    kind_for_path,
)
from .artifact_capabilities import artifact_capability
from .artifact_collection_contract import collection_contract_findings
from .artifact_html_contract import html_contract_findings, record_resource_ref
from .artifact_html_refs import image_ref_findings, scan_html_refs
from .artifact_openers import (
    JsonView,
    TabularView,
    open_csv,
    open_docx,
    open_fallback,
    open_json,
    open_pdf,
    open_xlsx,
    opener_for,
)
from .artifact_staged_evidence import staged_source_evidence_findings
from .artifact_static_site_contract import validate_static_site_artifact
from .artifact_structured_contracts import (
    csv_contract_findings,
    json_contract_findings,
    markdown_integrity_findings,
    markdown_local_reference_findings,
    markdown_section_findings,
    text_size_findings,
)
from .artifact_xlsx_contract import (
    required_columns_with_blank_values,
)
from .gates.document_content import document_content_quality_findings

ArtifactValidator = Callable[[ArtifactAcceptanceRequest], ArtifactAcceptanceReport]


def validate_html_artifact(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    path = Path(request.path)
    if _outside_workspace(path, request.workspace_root):
        return _outside_workspace_report(path)
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
            *image_ref_findings(
                refs,
                path=path,
                workspace_root=request.workspace_root,
                reference_roots=request.reference_roots,
            ),
            *html_contract_findings(text, refs.resources, request.validation_contract),
        ]
    )
    return ArtifactAcceptanceReport(
        ok=not any(item.severity == "hard" for item in findings),
        artifact_ref=str(path),
        artifact_kind="html",
        findings=findings,
    )


def validate_artifact(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    path = Path(request.path)
    if _outside_workspace(path, request.workspace_root):
        return _outside_workspace_report(path)
    if not path.exists():
        return _missing_report(path, kind=_request_capability(request).kind)
    validator = _resolve_artifact_validator(
        request,
        named_validators=_named_validators(),
        kind_validators=_kind_validators(),
        default_validator=_validate_generic_request,
    )
    return validator(request)


def _resolve_artifact_validator(
    request: ArtifactAcceptanceRequest,
    *,
    named_validators: dict[str, ArtifactValidator],
    kind_validators: dict[str, ArtifactValidator],
    default_validator: ArtifactValidator,
) -> ArtifactValidator:
    validator_name = _validator_name_from_contract(request.validation_contract)
    if validator_name and validator_name in named_validators:
        return named_validators[validator_name]
    validation = request.validation_contract or {}
    capability = artifact_capability(
        Path(request.path),
        declared_kind=str(validation.get("artifact_kind") or validation.get("kind") or ""),
        declared_mime=str(validation.get("mime_type") or validation.get("mime") or ""),
    )
    artifact_kind = capability.validator_key or capability.kind or kind_for_path(Path(request.path))
    return kind_validators.get(artifact_kind, default_validator)


def _validator_name_from_contract(validation_contract: dict[str, object] | None) -> str:
    value = (validation_contract or {}).get("validator")
    return str(value or "").strip().lower()


def _named_validators() -> dict[str, ArtifactValidator]:
    return {
        "artifact_acceptance": validate_by_artifact_kind,
        "static_site_check": validate_static_site_artifact,
        "spreadsheet_acceptance": _validate_xlsx_request,
        "document_acceptance": _validate_pdf_request,
    }


def _kind_validators() -> dict[str, ArtifactValidator]:
    return {
        "html": validate_html_artifact,
        "htm": validate_html_artifact,
        "json": _validate_json_request,
        "md": _validate_markdown_request,
        "markdown": _validate_markdown_request,
        "txt": _validate_text_request,
        "csv": _validate_csv_request,
        "xlsx": _validate_xlsx_request,
        "pdf": _validate_pdf_request,
        "docx": _validate_docx_request,
    }


def validate_by_artifact_kind(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    capability = _request_capability(request)
    return _kind_validators().get(capability.validator_key or capability.kind, _validate_generic_request)(request)


def _request_capability(request: ArtifactAcceptanceRequest):
    validation = request.validation_contract or {}
    declared_kind = str(validation.get("artifact_kind") or validation.get("kind") or "").strip()
    declared_mime = str(validation.get("mime_type") or validation.get("mime") or "").strip()
    return artifact_capability(request.path, declared_kind=declared_kind, declared_mime=declared_mime)


def _missing_report(path: Path, *, kind: str) -> ArtifactAcceptanceReport:
    finding = ArtifactFinding(
        code="ARTIFACT_MISSING",
        severity="hard",
        message="Artifact does not exist.",
        location=str(path),
    )
    return ArtifactAcceptanceReport(ok=False, artifact_ref=str(path), artifact_kind=kind, findings=[finding])


def _outside_workspace(path: Path, workspace_root: Path | None) -> bool:
    if workspace_root is None:
        return False
    try:
        path.resolve(strict=False).relative_to(Path(workspace_root).resolve(strict=False))
        return False
    except ValueError:
        return True


def validation_workspace_root_for_item(
    item: dict[str, object],
    path: Path,
    workspace_root: Path,
) -> Path:
    resolved_path = path.expanduser().resolve(strict=False)
    workspace = workspace_root.expanduser().resolve(strict=False)
    if _path_is_under(resolved_path, workspace):
        return workspace
    return _matching_allowed_output_root(item, resolved_path, workspace) or workspace


def _matching_allowed_output_root(item: dict[str, object], resolved_path: Path, workspace: Path) -> Path | None:
    roots = item.get("allowed_output_roots")
    if not isinstance(roots, list):
        return None
    for raw in roots:
        root = _resolve_allowed_output_root(raw, workspace, resolved_path)
        if root is not None and _path_is_under(resolved_path, root):
            return root
    return None


def _resolve_allowed_output_root(raw: object, workspace: Path, resolved_path: Path) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        candidate = Path(text).expanduser()
        root = candidate.resolve(strict=False) if candidate.is_absolute() else (workspace / candidate).resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    if root == resolved_path and root.suffix:
        return root.parent
    return root


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _outside_workspace_report(path: Path) -> ArtifactAcceptanceReport:
    finding = ArtifactFinding("ARTIFACT_PATH_OUTSIDE_WORKSPACE", "hard", "Artifact path is outside workspace_root.", str(path))
    return ArtifactAcceptanceReport(ok=False, artifact_ref=str(path), artifact_kind=kind_for_path(path), findings=[finding])


def _validate_json_request(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    return _validate_json(Path(request.path), request.validation_contract)


def _validate_markdown_request(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    return _validate_markdown(
        Path(request.path),
        request.validation_contract,
        workspace_root=request.workspace_root,
        reference_roots=request.reference_roots,
    )


def _validate_text_request(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    path = Path(request.path)
    if path.stat().st_size <= 0:
        return _report_with_finding(path, "txt", ArtifactFinding("ARTIFACT_EMPTY", "hard", "Artifact is empty."))
    findings = document_quality_artifact_findings(
        path, request.validation_contract, workspace_root=request.workspace_root or path.parent
    )
    return ArtifactAcceptanceReport(ok=True, artifact_ref=str(path), artifact_kind="txt", findings=findings)


def _validate_csv_request(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    return _validate_csv(Path(request.path), request.validation_contract)


def _validate_csv(path: Path, validation_contract: dict[str, object] | None = None) -> ArtifactAcceptanceReport:
    contract = validation_contract or {}
    opened = open_csv(path)
    if opened.finding is not None:
        return _report_with_finding(path, "csv", opened.finding)
    rows = opened.view.rows if isinstance(opened.view, TabularView) else []
    if not rows or not any(cell.strip() for cell in rows[0]):
        finding = ArtifactFinding(
            code="CSV_HEADER_MISSING",
            severity="hard",
            message="CSV must include a non-empty header row.",
        )
        return _report_with_finding(path, "csv", finding)
    data_rows = [row for row in rows[1:] if any(cell.strip() for cell in row)]
    min_data_rows = _csv_min_data_rows(contract)
    if len(data_rows) < min_data_rows:
        finding = ArtifactFinding(
            code="CSV_INSUFFICIENT_DATA_ROWS",
            severity="hard",
            message="CSV data row count is below validation_contract.min_data_rows.",
            value=f"{len(data_rows)}<{min_data_rows}",
        )
        return _report_with_finding(path, "csv", finding)
    findings = csv_contract_findings(path, rows, contract)
    return ArtifactAcceptanceReport(
        ok=not any(item.severity == "hard" for item in findings),
        artifact_ref=str(path),
        artifact_kind="csv",
        findings=findings,
    )


def _csv_min_data_rows(validation_contract: dict[str, object]) -> int:
    try:
        return max(0, int(validation_contract.get("min_data_rows", 1)))
    except (TypeError, ValueError):
        return 1


def _validate_xlsx_request(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    return _validate_xlsx(Path(request.path), request.validation_contract, workspace_root=request.workspace_root)


def _validate_pdf_request(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    return _validate_pdf(
        Path(request.path),
        request.validation_contract,
        workspace_root=request.workspace_root,
    )


def _validate_docx_request(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    path = Path(request.path)
    opened = open_docx(path)
    if opened.finding is not None:
        return _report_with_finding(path, "docx", opened.finding)
    findings = document_quality_artifact_findings(
        path, request.validation_contract, workspace_root=request.workspace_root or path.parent
    )
    return ArtifactAcceptanceReport(ok=True, artifact_ref=str(path), artifact_kind="docx", findings=findings)


def _validate_generic_request(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    path = Path(request.path)
    kind = _request_capability(request).kind or kind_for_path(path)
    contract = request.validation_contract or {}
    # 运行时发现的插件打开器优先（不改源码、不重启即可深度校验新格式）。
    plugin_opener = opener_for(kind)
    if plugin_opener is not None and kind not in _BUILTIN_OPENER_KINDS:
        return _validate_via_plugin_opener(path, kind, plugin_opener, contract)
    # 合同声明了结构要求时，用通用兜底打开器按"长相"尽量检查；否则回第 0 层。
    if _has_generic_structure_requirements(contract):
        return _validate_generic_structure(path, kind, contract)
    return _validate_generic(path)


_BUILTIN_OPENER_KINDS = {"csv", "xlsx", "pdf", "docx", "json"}


def _has_generic_structure_requirements(contract: dict[str, object]) -> bool:
    return any(
        contract.get(key)
        for key in ("min_size", "required_sections", "required_strings", "required_regex", "required_files")
    )


def _validate_via_plugin_opener(path: Path, kind: str, opener, contract: dict[str, object]) -> ArtifactAcceptanceReport:
    try:
        opened = opener(path)
    except Exception as exc:  # 插件打开器异常隔离为结构化打不开 finding
        finding = ArtifactFinding(code="ARTIFACT_OPENER_FAILED", severity="hard", message=f"opener for {kind} failed: {exc}")
        return _report_with_finding(path, kind, finding)
    if getattr(opened, "finding", None) is not None:
        return _report_with_finding(path, kind, opened.finding)
    findings = _generic_view_findings(path, getattr(opened, "view", None), contract)
    return ArtifactAcceptanceReport(
        ok=not any(item.severity == "hard" for item in findings),
        artifact_ref=str(path),
        artifact_kind=kind,
        findings=findings,
    )


def _validate_generic_structure(path: Path, kind: str, contract: dict[str, object]) -> ArtifactAcceptanceReport:
    if path.stat().st_size <= 0:
        return _report_with_finding(path, kind, ArtifactFinding("ARTIFACT_EMPTY", "hard", "Artifact is empty."))
    opened = open_fallback(path)
    findings = _generic_view_findings(path, opened.view, contract)
    return ArtifactAcceptanceReport(
        ok=not any(item.severity == "hard" for item in findings),
        artifact_ref=str(path),
        artifact_kind=kind,
        findings=findings,
    )


def _generic_view_findings(path: Path, view: object, contract: dict[str, object]) -> list[ArtifactFinding]:
    """格式无关的声明字段校验：吃任意打开器视图的文本/zip 成员，永不随格式增长。"""
    text = _view_text(view)
    zip_names = _view_zip_names(view)
    findings: list[ArtifactFinding] = []
    findings.extend(text_size_findings(path, text, contract))
    findings.extend(markdown_section_findings(path, text, contract))
    findings.extend(_required_text_findings(path, text, contract))
    findings.extend(_forbidden_text_findings(path, text, contract))
    findings.extend(_required_member_findings(path, zip_names, contract))
    return findings


def _view_text(view: object) -> str:
    for attr in ("text",):
        value = getattr(view, attr, None)
        if isinstance(value, str):
            return value
    rows = getattr(view, "rows", None)
    if isinstance(rows, list):
        return "\n".join("\t".join(str(cell) for cell in row) for row in rows)
    return ""


def _view_zip_names(view: object) -> set[str]:
    for attr in ("zip_names", "names"):
        value = getattr(view, attr, None)
        if isinstance(value, set):
            return value
    return set()


def _required_member_findings(path: Path, zip_names: set[str], contract: dict[str, object]) -> list[ArtifactFinding]:
    required = contract.get("required_files")
    if not isinstance(required, list) or not zip_names:
        return []
    missing = [str(name) for name in required if str(name) and str(name) not in zip_names]
    if not missing:
        return []
    return [ArtifactFinding(
        code="ARTIFACT_REQUIRED_MEMBER_MISSING",
        severity="hard",
        message="Archive is missing required members.",
        location=str(path),
        value=",".join(missing),
    )]


def _validate_json(path: Path, validation_contract: dict[str, object] | None = None) -> ArtifactAcceptanceReport:
    opened = open_json(path)
    if opened.finding is not None:
        return _report_with_finding(path, "json", opened.finding)
    value = opened.view.value if isinstance(opened.view, JsonView) else None
    findings = json_contract_findings(path, value, validation_contract or {})
    return ArtifactAcceptanceReport(
        ok=not any(item.severity == "hard" for item in findings),
        artifact_ref=str(path),
        artifact_kind="json",
        findings=findings,
    )


def _validate_markdown(
    path: Path,
    validation_contract: dict[str, object] | None = None,
    *,
    workspace_root: Path | None = None,
    reference_roots: tuple[Path | str, ...] = (),
) -> ArtifactAcceptanceReport:
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text:
        finding = ArtifactFinding(code="ARTIFACT_EMPTY", severity="hard", message="Artifact is empty.")
        return _report_with_finding(path, "md", finding)
    findings = [
        *text_size_findings(path, text, validation_contract or {}),
        *markdown_section_findings(path, text, validation_contract or {}),
    ]
    findings.extend(markdown_integrity_findings(path, text))
    findings.extend(markdown_local_reference_findings(path, text, reference_roots=reference_roots))
    findings.extend(_required_text_findings(path, text, validation_contract or {}))
    findings.extend(_forbidden_text_findings(path, text, validation_contract or {}))
    findings.extend(document_quality_artifact_findings(path, validation_contract, workspace_root=workspace_root or path.parent))
    return ArtifactAcceptanceReport(
        ok=not any(item.severity == "hard" for item in findings),
        artifact_ref=str(path),
        artifact_kind="md",
        findings=findings,
    )


def _required_text_findings(path: Path, text: str, validation_contract: dict[str, object]) -> list[ArtifactFinding]:
    findings: list[ArtifactFinding] = []
    for token in _required_string_items(validation_contract):
        if token not in text:
            findings.append(
                ArtifactFinding(
                    code="ARTIFACT_REQUIRED_TEXT_MISSING",
                    severity="hard",
                    message="Artifact text is missing a required string from validation_contract.",
                    location=str(path),
                    value=token[:200],
                )
            )
    for pattern in _required_regex_items(validation_contract):
        try:
            matched = re.search(pattern, text, flags=re.MULTILINE) is not None
        except re.error as exc:
            findings.append(
                ArtifactFinding(
                    code="ARTIFACT_REQUIRED_REGEX_INVALID",
                    severity="hard",
                    message=f"validation_contract required regex is invalid: {exc}",
                    location=str(path),
                    value=pattern[:200],
                )
            )
            continue
        if not matched:
            findings.append(
                ArtifactFinding(
                    code="ARTIFACT_REQUIRED_REGEX_MISSING",
                    severity="hard",
                    message="Artifact text does not match a required regex from validation_contract.",
                    location=str(path),
                    value=pattern[:200],
                )
            )
    return findings


def _forbidden_text_findings(path: Path, text: str, validation_contract: dict[str, object]) -> list[ArtifactFinding]:
    findings: list[ArtifactFinding] = []
    for token in _forbidden_string_items(validation_contract):
        if token in text:
            findings.append(
                ArtifactFinding(
                    code="ARTIFACT_FORBIDDEN_TEXT_PRESENT",
                    severity="hard",
                    message="Artifact text contains a forbidden string from validation_contract.",
                    location=str(path),
                    value=token[:200],
                )
            )
    for pattern in _forbidden_regex_items(validation_contract):
        try:
            matched = re.search(pattern, text, flags=re.MULTILINE) is not None
        except re.error as exc:
            findings.append(
                ArtifactFinding(
                    code="ARTIFACT_FORBIDDEN_REGEX_INVALID",
                    severity="hard",
                    message=f"validation_contract forbidden regex is invalid: {exc}",
                    location=str(path),
                    value=pattern[:200],
                )
            )
            continue
        if matched:
            findings.append(
                ArtifactFinding(
                    code="ARTIFACT_FORBIDDEN_REGEX_MATCHED",
                    severity="hard",
                    message="Artifact text matches a forbidden regex from validation_contract.",
                    location=str(path),
                    value=pattern[:200],
                )
            )
    return findings


def _required_string_items(validation_contract: dict[str, object]) -> list[str]:
    return _string_items(
        validation_contract,
        ("required_strings",),
    )


def _required_regex_items(validation_contract: dict[str, object]) -> list[str]:
    return _string_items(
        validation_contract,
        ("required_regex",),
    )


def _forbidden_string_items(validation_contract: dict[str, object]) -> list[str]:
    return _string_items(
        validation_contract,
        ("forbidden_strings",),
    )


def _forbidden_regex_items(validation_contract: dict[str, object]) -> list[str]:
    return _string_items(
        validation_contract,
        ("forbidden_regex",),
    )


def _string_items(validation_contract: dict[str, object], keys: tuple[str, ...]) -> list[str]:
    items: list[str] = []
    for key in keys:
        items.extend(_string_items_value(validation_contract.get(key)))
    return list(dict.fromkeys(items))


def _string_items_value(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value if str(item).strip()]
    return []


def _validate_xlsx(
    path: Path,
    validation_contract: dict[str, object] | None = None,
    *,
    workspace_root: Path | None = None,
) -> ArtifactAcceptanceReport:
    opened = open_xlsx(path)
    if opened.finding is not None:
        return _report_with_finding(path, "xlsx", opened.finding)
    view = opened.view if isinstance(opened.view, TabularView) else TabularView()
    findings = advisory_artifact_findings([
        *_xlsx_structure_findings(path, view, validation_contract or {}),
        *staged_source_evidence_findings(validation_contract or {}, workspace_root or path.parent),
        *collection_contract_findings(validation_contract or {}, workspace_root or path.parent),
    ])
    return ArtifactAcceptanceReport(
        ok=True,
        artifact_ref=str(path),
        artifact_kind="xlsx",
        findings=findings,
    )


def _xlsx_structure_findings(path: Path, view: TabularView, contract: dict[str, object]) -> list[ArtifactFinding]:
    findings: list[ArtifactFinding] = []
    required_sheets = _positive_int(contract.get("required_sheets_min"))
    if required_sheets > 0:
        actual = len([name for name in view.names if name.startswith("xl/worksheets/") and name.endswith(".xml")])
        if actual < required_sheets:
            findings.append(ArtifactFinding(
                code="XLSX_TOO_FEW_SHEETS",
                severity="hard",
                message=f"Workbook has {actual} sheets, expected at least {required_sheets}.",
                location=str(path),
                value=str(actual),
            ))
    required_columns = _required_columns(contract.get("required_columns"))
    if required_columns:
        missing = [column for column in required_columns if column not in view.text]
        if missing:
            findings.append(ArtifactFinding(
                code="XLSX_MISSING_REQUIRED_COLUMNS",
                severity="hard",
                message="Workbook is missing required columns.",
                location=str(path),
                value=",".join(missing),
            ))
        blank_columns = required_columns_with_blank_values(view.tables, required_columns)
        if blank_columns:
            findings.append(ArtifactFinding(
                code="XLSX_REQUIRED_COLUMN_EMPTY_VALUES",
                severity="hard",
                message="Workbook has blank values in required columns.",
                location=str(path),
                value=",".join(blank_columns),
            ))
    return findings


def _required_columns(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _positive_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _validate_pdf(
    path: Path,
    validation_contract: dict[str, object] | None = None,
    *,
    workspace_root: Path | None = None,
) -> ArtifactAcceptanceReport:
    opened = open_pdf(path)
    if opened.finding is not None:
        return _report_with_finding(path, "pdf", opened.finding)
    findings = advisory_artifact_findings([
        *collection_contract_findings(validation_contract or {}, workspace_root or path.parent),
        *document_quality_artifact_findings(path, validation_contract, workspace_root=workspace_root or path.parent),
    ])
    return ArtifactAcceptanceReport(
        ok=True,
        artifact_ref=str(path),
        artifact_kind="pdf",
        findings=findings,
    )


def _validate_generic(path: Path) -> ArtifactAcceptanceReport:
    if path.stat().st_size <= 0:
        finding = ArtifactFinding(code="ARTIFACT_EMPTY", severity="hard", message="Artifact is empty.")
        return _report_with_finding(path, kind_for_path(path), finding)
    finding = binary_signature_finding(path)
    if finding is not None:
        return _report_with_finding(path, kind_for_path(path), finding)
    return ArtifactAcceptanceReport(ok=True, artifact_ref=str(path), artifact_kind=kind_for_path(path))


def binary_signature_finding(path: Path) -> ArtifactFinding | None:
    expected = {
        "png": (b"\x89PNG\r\n\x1a\n",),
        "zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
        "gz": (b"\x1f\x8b",),
        "gzip": (b"\x1f\x8b",),
    }.get(kind_for_path(path))
    if expected is None:
        return None
    data = path.read_bytes()[:8]
    if any(data.startswith(signature) for signature in expected):
        return None
    return ArtifactFinding(
        code="ARTIFACT_INVALID_SIGNATURE",
        severity="hard",
        message="Artifact bytes do not match the expected file signature.",
        location=str(path),
        value=kind_for_path(path),
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


def _finding_records(items: list[dict[str, str]]) -> list[ArtifactFinding]:
    return [ArtifactFinding(**item) for item in items]


__all__ = [
    "ArtifactAcceptanceReport",
    "ArtifactAcceptanceRequest",
    "ArtifactFinding",
    "artifact_ref_payload",
    "validate_artifact",
    "validate_html_artifact",
    "validation_workspace_root_for_item",
]
