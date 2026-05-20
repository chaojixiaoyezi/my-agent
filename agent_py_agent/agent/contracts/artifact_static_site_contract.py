# LLM: Static-site artifact adapter keeps directory web-project QA out of generic artifact validation.
# 模块用途: 将 validation_contract.validator=static_site_check 的目录产物转成通用 ArtifactAcceptanceReport。

from __future__ import annotations

from pathlib import Path

from ..subagents.static_site_validator import run_static_site_check
from .artifact_acceptance_models import (
    ArtifactAcceptanceReport,
    ArtifactAcceptanceRequest,
    ArtifactFinding,
)


# LLM: validate_static_site_artifact adapts directory web-project checks into the shared report contract.
# 函数用途: 执行目录级网页验收，不能让 web_project 因为目录存在就按 generic 通过。
def validate_static_site_artifact(request: ArtifactAcceptanceRequest) -> ArtifactAcceptanceReport:
    workspace = Path(request.workspace_root or request.path.parent).expanduser().resolve()
    path = Path(request.path).expanduser().resolve()
    record = run_static_site_check(
        _static_site_test_payload(path, workspace, request.validation_contract or {}),
        workspace,
    )
    findings = _static_site_findings(record.validation_result)
    if not record.passed and not findings:
        findings = [
            ArtifactFinding(
                code="STATIC_SITE_CHECK_FAILED",
                severity="hard",
                message=record.error or "Static site check failed.",
                location=str(path),
            )
        ]
    return ArtifactAcceptanceReport(
        ok=record.passed,
        artifact_ref=str(path),
        artifact_kind="web_project",
        findings=findings,
    )


# LLM: _static_site_test_payload passes only machine validation fields into static_site_check.
# 函数用途: 把 validation_contract.required_files 等结构化字段转成 validator 输入，不读取任务自然语言。
def _static_site_test_payload(
    path: Path,
    workspace_root: Path,
    validation_contract: dict[str, object],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "artifact-static-site-check",
        "validation_method": "static_site_check",
        "site_root": _site_root_ref(path, workspace_root),
    }
    for key in _STATIC_SITE_VALIDATION_KEYS:
        if key in validation_contract:
            payload[key] = validation_contract[key]
    return payload


_STATIC_SITE_VALIDATION_KEYS = (
    "required_files",
    "html_files",
    "check_files",
    "required_dom_ids",
    "check_local_refs",
    "forbid_placeholders",
    "require_complete_html",
    "check_inert_controls",
    "check_form_bindings",
    "strict_dom_bindings",
    "max_files",
)


# LLM: _site_root_ref keeps static-site refs portable when the path is inside the workspace.
# 函数用途: site_root 优先写工作区相对路径；不在工作区内时保留绝对路径并让 validator 拒绝越界。
def _site_root_ref(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)).replace("\\", "/") or "."
    except ValueError:
        return str(path)


# LLM: _static_site_findings turns validator fact lists into ArtifactFinding records.
# 函数用途: 将 static_site_check 的缺文件、坏链接、失效控件等机器事实映射为通用 artifact findings。
def _static_site_findings(validation_result: dict[str, object]) -> list[ArtifactFinding]:
    findings: list[ArtifactFinding] = []
    for field, code in _STATIC_SITE_FINDING_CODES.items():
        values = validation_result.get(field)
        if isinstance(values, list):
            findings.extend(_field_findings(field, code, values))
    return findings


_STATIC_SITE_FINDING_CODES = {
    "missing_required_files": "STATIC_SITE_MISSING_REQUIRED_FILES",
    "placeholder_hits": "STATIC_SITE_PLACEHOLDER_HITS",
    "broken_local_refs": "STATIC_SITE_BROKEN_LOCAL_REFS",
    "html_structure_hits": "STATIC_SITE_HTML_STRUCTURE_HITS",
    "inert_control_hits": "STATIC_SITE_INERT_CONTROL_HITS",
    "form_binding_hits": "STATIC_SITE_FORM_BINDING_HITS",
    "missing_dom_id_hits": "STATIC_SITE_MISSING_DOM_ID_HITS",
}


# LLM: _field_findings renders one validator fact list into bounded hard findings.
# 函数用途: 为 static_site_check 的每个失败项生成统一 finding，后续修复流程不用解析自然语言。
def _field_findings(field: str, code: str, values: list[object]) -> list[ArtifactFinding]:
    return [
        ArtifactFinding(
            code=code,
            severity="hard",
            message=f"Static site check reported {field}.",
            location=field,
            value=str(value),
        )
        for value in values
    ]


__all__ = ["validate_static_site_artifact"]
