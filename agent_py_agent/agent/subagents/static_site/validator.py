# 2026-05-18: strict_dom_bindings checks unguarded JS id lookups; required_dom_ids declares mandatory DOM.

from __future__ import annotations

"""Bounded static-site validation for closeout tests."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...common.value_parsing import sequence_strings
from ..execution.records import TestExecutionRecord
from .dom_checks import (
    InertControlCheckRequest,
    form_binding_hits,
    inert_control_hits,
    missing_dom_id_hits,
)
from .html_parser import StaticSiteHTMLParser
from .js_api_checks import missing_window_app_method_hits
from .path_checks import (
    broken_refs,
    inside,
    local_script_refs,
    rel,
    small_text,
    unique_paths,
)
from .records import static_site_failure_summary, static_site_record


@dataclass
class StaticSiteCheckResult:
    """Collect static-site validation facts."""

    checked_root: str
    checked_files: list[str] = field(default_factory=list)
    missing_required_files: list[str] = field(default_factory=list)
    placeholder_hits: list[str] = field(default_factory=list)
    broken_local_refs: list[str] = field(default_factory=list)
    html_structure_hits: list[str] = field(default_factory=list)
    inert_control_hits: list[str] = field(default_factory=list)
    form_binding_hits: list[str] = field(default_factory=list)
    missing_dom_id_hits: list[str] = field(default_factory=list)
    missing_js_api_hits: list[str] = field(default_factory=list)
    repair_hints: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.missing_required_files
            or self.placeholder_hits
            or self.broken_local_refs
            or self.html_structure_hits
            or self.inert_control_hits
            or self.form_binding_hits
            or self.missing_dom_id_hits
            or self.missing_js_api_hits
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_root": self.checked_root,
            "checked_files": self.checked_files,
            "missing_required_files": self.missing_required_files,
            "placeholder_hits": self.placeholder_hits,
            "broken_local_refs": self.broken_local_refs,
            "html_structure_hits": self.html_structure_hits,
            "inert_control_hits": self.inert_control_hits,
            "form_binding_hits": self.form_binding_hits,
            "missing_dom_id_hits": self.missing_dom_id_hits,
            "missing_js_api_hits": self.missing_js_api_hits,
            "repair_hints": self.repair_hints,
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class StaticSiteScanOptions:
    check_refs: bool
    check_placeholders: bool
    check_complete_html: bool
    check_controls: bool
    check_forms: bool
    strict_dom_bindings: bool
    required_dom_ids: list[str]


@dataclass
class StaticSiteScanState:
    form_ids: set[str] = field(default_factory=set)
    element_ids: set[str] = field(default_factory=set)
    script_texts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StaticSiteHtmlScanRequest:
    result: StaticSiteCheckResult
    path: Path
    site_root: Path
    options: StaticSiteScanOptions
    state: StaticSiteScanState


def run_static_site_check(test: dict[str, Any], workspace_root: Path) -> TestExecutionRecord:
    site_root, error = _resolve_site_root(test, workspace_root)
    if error:
        return static_site_record(
            test, StaticSiteCheckResult(str(site_root)), executed=False, error=error
        )
    result = _scan_site(test, site_root)
    error_text = "" if result.ok else static_site_failure_summary(result)
    return static_site_record(test, result, executed=True, error=error_text)


def _resolve_site_root(test: dict[str, Any], workspace_root: Path) -> tuple[Path, str]:
    raw = str(test.get("site_root") or ".").strip()
    workspace = workspace_root.expanduser().resolve()
    candidate = Path(raw).expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    try:
        path.relative_to(workspace)
    except ValueError:
        return path, "site_root 超出 workspace 边界"
    if not path.exists() or not path.is_dir():
        return path, "site_root 不存在或不是目录"
    return path, ""


def _scan_site(test: dict[str, Any], site_root: Path) -> StaticSiteCheckResult:
    result = StaticSiteCheckResult(checked_root=str(site_root))
    _check_required_files(result, test, site_root)
    html_files = _html_files(test, site_root, int(test.get("max_files") or 200))
    result.checked_files = [rel(path, site_root) for path in html_files]
    options = _scan_options(test)
    state = StaticSiteScanState()
    for path in html_files:
        _scan_html_file(StaticSiteHtmlScanRequest(result, path, site_root, options, state))
    _finalize_dom_checks(result, options, state)
    result.repair_hints.extend(_repair_hints(result))
    return result


def _scan_options(test: dict[str, Any]) -> StaticSiteScanOptions:
    return StaticSiteScanOptions(
        check_refs=test.get("check_local_refs", True) is not False,
        check_placeholders=test.get("forbid_placeholders", True) is not False,
        check_complete_html=test.get("require_complete_html", False) is True,
        check_controls=test.get("check_inert_controls", True) is not False,
        check_forms=test.get("check_form_bindings", True) is not False,
        strict_dom_bindings=test.get("strict_dom_bindings", True) is not False,
        required_dom_ids=sequence_strings(test.get("required_dom_ids")),
    )


def _scan_html_file(request: StaticSiteHtmlScanRequest) -> None:
    result, path, site_root = request.result, request.path, request.site_root
    options, state = request.options, request.state
    text = path.read_text(encoding="utf-8", errors="replace")
    if options.check_placeholders and _has_visible_template_placeholder(text):
        result.placeholder_hits.append(rel(path, site_root))
    if options.check_complete_html:
        result.html_structure_hits.extend(_html_structure_hits(text, path, site_root))
    parser = StaticSiteHTMLParser()
    parser.feed(text)
    state.form_ids.update(parser.form_ids)
    state.element_ids.update(parser.element_ids)
    local_script_text = "\n".join(
        small_text(item) for item in unique_paths(local_script_refs(parser.refs, path, site_root))
    )
    state.script_texts.extend([text, local_script_text])
    if options.check_refs:
        result.broken_local_refs.extend(broken_refs(parser.refs, path, site_root))
    if options.check_controls:
        result.inert_control_hits.extend(
            inert_control_hits(
                InertControlCheckRequest(
                    controls=parser.controls,
                    rel_path=rel(path, site_root),
                    html_text=f"{text}\n{local_script_text}",
                    element_ids=set(parser.element_ids),
                )
            )
        )


def _finalize_dom_checks(
    result: StaticSiteCheckResult,
    options: StaticSiteScanOptions,
    state: StaticSiteScanState,
) -> None:
    combined_script_text = "\n".join(state.script_texts)
    if options.check_forms:
        result.form_binding_hits.extend(form_binding_hits(state.form_ids, combined_script_text))
    result.missing_dom_id_hits.extend(
        missing_dom_id_hits(
            state.element_ids,
            combined_script_text,
            allow_optional_missing=not options.strict_dom_bindings,
        )
    )
    result.missing_dom_id_hits.extend(
        _missing_required_dom_id_hits(state.element_ids, options.required_dom_ids)
    )
    result.missing_js_api_hits.extend(missing_window_app_method_hits(combined_script_text))


def _check_required_files(
    result: StaticSiteCheckResult, test: dict[str, Any], site_root: Path
) -> None:
    for item in sequence_strings(test.get("required_files")):
        path = (site_root / item).resolve()
        if not inside(path, site_root) or not path.exists() or not path.is_file():
            result.missing_required_files.append(item)


def _html_files(test: dict[str, Any], site_root: Path, max_files: int) -> list[Path]:
    scoped = _scoped_html_files(test, site_root)
    if scoped:
        return scoped[: max(1, min(max_files, 500))]
    limit = max(1, min(max_files, 500))
    return sorted(path for path in site_root.rglob("*.html") if path.is_file())[:limit]


def _scoped_html_files(test: dict[str, Any], site_root: Path) -> list[Path]:
    paths: list[Path] = []
    for item in sequence_strings(test.get("html_files") or test.get("check_files")):
        candidate = (site_root / item).resolve()
        if (
            inside(candidate, site_root)
            and candidate.is_file()
            and candidate.suffix.lower() in {".html", ".htm"}
        ):
            paths.append(candidate)
    return sorted(dict.fromkeys(paths))


def _missing_required_dom_id_hits(element_ids: set[str], required_dom_ids: list[str]) -> list[str]:
    return [
        f"required_dom_id:{item}"
        for item in sorted(set(required_dom_ids))
        if item not in element_ids
    ]


def _html_structure_hits(text: str, html_file: Path, site_root: Path) -> list[str]:
    rel_path = rel(html_file, site_root)
    lower = text.lower()
    hits: list[str] = []
    required = (
        ("doctype", "<!doctype"),
        ("html_open", "<html"),
        ("html_close", "</html>"),
        ("head_close", "</head>"),
        ("body_open", "<body"),
        ("body_close", "</body>"),
    )
    for label, marker in required:
        if marker not in lower:
            hits.append(f"{rel_path}:{label}")
    for tag in ("style", "script"):
        opens = len(re.findall(rf"<{tag}\b", lower))
        closes = lower.count(f"</{tag}>")
        if opens != closes:
            hits.append(f"{rel_path}:unbalanced_{tag}")
    for tag in ("html", "body"):
        closes = lower.count(f"</{tag}>")
        if closes > 1:
            hits.append(f"{rel_path}:duplicate_{tag}_close")
    html_close = lower.find("</html>")
    if html_close != -1 and re.search(r"<[A-Za-z][^>]*>", lower[html_close + len("</html>") :]):
        hits.append(f"{rel_path}:trailing_markup_after_html_close")
    if (
        lower.find("<body") != -1
        and lower.find("</head>") != -1
        and lower.find("<body") < lower.find("</head>")
    ):
        hits.append(f"{rel_path}:body_before_head_close")
    return hits


def _repair_hints(result: StaticSiteCheckResult) -> list[str]:
    hints: list[str] = []
    if result.html_structure_hits:
        hints.append(
            "html_structure: repair or regenerate a complete HTML skeleton before DOM/id fixes"
        )
    if result.inert_control_hits:
        hints.append(
            "inert_controls: add real href targets, onclick handlers, or matching anchor sections for listed controls"
        )
    if result.form_binding_hits:
        hints.append(
            "form_bindings: create the referenced form id or update validateForm(...) to the existing form id"
        )
    if result.missing_dom_id_hits:
        hints.append(
            "missing_dom_ids: add the referenced id to a real element or remove the stale unguarded JS lookup"
        )
    if result.missing_js_api_hits:
        hints.append(
            "missing_js_api: export referenced window.app methods or update inline handlers/templates"
        )
    return hints[:6]


def _has_visible_template_placeholder(text: str) -> bool:
    visible_text = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1>", "", text, flags=re.IGNORECASE | re.DOTALL
    )
    return "${" in visible_text
