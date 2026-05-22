# LLM: Web project integrity checks keep static-site validation out of generic artifact helpers.
# 模块用途: 在 HTML/CSS/JS 写入后运行通用静态站点检查，返回统一 ArtifactIntegrityDecision。

from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifact_integrity import ArtifactIntegrityDecision, ArtifactIntegrityIssue

_WEB_ASSET_SUFFIXES = {".html", ".htm", ".css", ".js", ".mjs", ".cjs"}
_STATIC_SITE_ISSUE_CODES = {
    "missing_required_files": "STATIC_SITE_MISSING_REQUIRED_FILES",
    "placeholder_hits": "STATIC_SITE_PLACEHOLDER_HITS",
    "broken_local_refs": "STATIC_SITE_BROKEN_LOCAL_REFS",
    "html_structure_hits": "STATIC_SITE_HTML_STRUCTURE_HITS",
    "inert_control_hits": "STATIC_SITE_INERT_CONTROL_HITS",
    "form_binding_hits": "STATIC_SITE_FORM_BINDING_HITS",
    "missing_dom_id_hits": "STATIC_SITE_MISSING_DOM_ID_HITS",
    "missing_js_api_hits": "STATIC_SITE_MISSING_JS_API_HITS",
}


def check_web_project_post_write(path: Path, workspace_root: Path) -> ArtifactIntegrityDecision:
    target = Path(path).expanduser().resolve(strict=False)
    workspace = Path(workspace_root).expanduser().resolve(strict=False)
    if target.suffix.lower() not in _WEB_ASSET_SUFFIXES:
        return ArtifactIntegrityDecision(ok=True)
    site_root = _nearest_site_root(target, workspace)
    if site_root is None:
        return ArtifactIntegrityDecision(ok=True)
    from ..subagents.static_site_validator import run_static_site_check

    record = run_static_site_check(_static_site_request(site_root, workspace), workspace)
    return _web_project_decision(record.validation_result)


def _static_site_request(site_root: Path, workspace: Path) -> dict[str, object]:
    return {
        "validation_method": "static_site_check",
        "site_root": _site_root_ref(site_root, workspace),
        "require_complete_html": True,
        "strict_dom_bindings": True,
        "check_local_refs": True,
        "forbid_placeholders": True,
        "check_inert_controls": True,
        "check_form_bindings": True,
        "max_files": 200,
    }


def _nearest_site_root(path: Path, workspace_root: Path) -> Path | None:
    try:
        path.relative_to(workspace_root)
    except ValueError:
        return None
    start = path.parent if path.is_file() or path.suffix else path
    for candidate in (start, *start.parents):
        if not _inside(candidate, workspace_root):
            break
        if any(item.is_file() for item in candidate.glob("*.html")):
            return candidate
        if candidate == workspace_root:
            break
    return None


def _inside(candidate: Path, workspace_root: Path) -> bool:
    try:
        candidate.relative_to(workspace_root)
        return True
    except ValueError:
        return False


def _site_root_ref(site_root: Path, workspace_root: Path) -> str:
    try:
        return str(site_root.relative_to(workspace_root)).replace("\\", "/") or "."
    except ValueError:
        return str(site_root)


def _web_project_decision(validation_result: dict[str, Any]) -> ArtifactIntegrityDecision:
    issues = [
        ArtifactIntegrityIssue(code=code, message=f"{field} failed.", count=len(values))
        for field, code in _STATIC_SITE_ISSUE_CODES.items()
        if isinstance((values := validation_result.get(field)), list) and values
    ]
    return ArtifactIntegrityDecision(ok=not issues, kind="web_project", issues=issues)
