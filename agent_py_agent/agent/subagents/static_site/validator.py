# 2026-05-18: strict_dom_bindings checks unguarded JS id lookups; required_dom_ids declares mandatory DOM.

from __future__ import annotations

"""Bounded static-site validation for closeout tests."""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ...common.value_parsing import sequence_strings
from ..execution.executor_helpers import _test_name, _utc_now_iso
from ..execution.records import TestExecutionRecord

REMOTE_SCHEMES = {"http", "https", "mailto", "tel", "data", "javascript"}
_VALIDATE_FORM_CALL_RE = re.compile(r"validateForm\(\s*['\"]([^'\"]+)['\"]\s*\)")
_GET_ELEMENT_BY_ID_RE = re.compile(r"getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)")
_QUERY_SELECTOR_ID_RE = re.compile(r"querySelector(?:All)?\(\s*['\"]#([A-Za-z0-9_-]+)['\"]\s*\)")
_TEMPLATE_ID_ATTR_RE = re.compile(r"\bid\s*=\s*['\"]([A-Za-z0-9_-]+)['\"]")
_ASSIGNED_GET_ELEMENT_RE = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*document\.getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)\s*;"
)


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


@dataclass(frozen=True)
class InertControlCheckRequest:
    controls: list[dict[str, object]]
    rel_path: str
    html_text: str
    element_ids: set[str]


class StaticSiteHTMLParser(HTMLParser):
    """Extract local refs and basic controls from HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[tuple[str, str]] = []
        self.controls: list[dict[str, object]] = []
        self.element_ids: list[str] = []
        self.form_ids: list[str] = []
        self._current_control: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        for ref_attr in ("href", "src", "action"):
            if attr_map.get(ref_attr):
                self.refs.append((ref_attr, attr_map[ref_attr]))
        if attr_map.get("id"):
            self.element_ids.append(attr_map["id"])
        if tag == "form" and attr_map.get("id"):
            self.form_ids.append(attr_map["id"])
        if tag in {"button", "a", "input", "select", "textarea"}:
            control = {
                "tag": tag,
                "href": attr_map.get("href", ""),
                "onclick": attr_map.get("onclick", ""),
                "type": attr_map.get("type", ""),
                "disabled": "disabled" in attr_map,
                "text": "",
            }
            if tag == "input":
                control["text"] = attr_map.get("aria-label") or attr_map.get("placeholder") or attr_map.get("value") or ""
                self.controls.append(control)
                return
            self._current_control = control

    def handle_data(self, data: str) -> None:
        if self._current_control is not None:
            text = str(self._current_control.get("text") or "")
            self._current_control["text"] = (text + data).strip()[:80]

    def handle_endtag(self, tag: str) -> None:
        if self._current_control and self._current_control.get("tag") == tag:
            self.controls.append(dict(self._current_control))
            self._current_control = None


def run_static_site_check(test: dict[str, Any], workspace_root: Path) -> TestExecutionRecord:
    site_root, error = _resolve_site_root(test, workspace_root)
    if error:
        return static_site_record(
            test, StaticSiteCheckResult(str(site_root)), executed=False, error=error
        )
    result = _scan_site(test, site_root)
    error_text = "" if result.ok else static_site_failure_summary(result)
    return static_site_record(test, result, executed=True, error=error_text)


def static_site_record(
    test: dict[str, Any],
    result: Any,
    *,
    executed: bool,
    error: str,
) -> TestExecutionRecord:
    return TestExecutionRecord(
        test_name=_test_name(test),
        executed=executed,
        exit_code=0 if result.ok and executed else 1,
        executed_at=_utc_now_iso(),
        error=error,
        validation_method="static_site_check",
        validation_result=result.to_dict(),
    )


def static_site_failure_summary(result: Any) -> str:
    parts: list[str] = []
    for field_name in (
        "missing_required_files",
        "placeholder_hits",
        "broken_local_refs",
        "html_structure_hits",
        "inert_control_hits",
        "form_binding_hits",
        "missing_dom_id_hits",
        "missing_js_api_hits",
    ):
        values = getattr(result, field_name, [])
        if values:
            parts.append(f"{field_name}={len(values)}")
    return "; ".join(parts)


def broken_refs(refs: list[tuple[str, str]], html_file: Path, site_root: Path) -> list[str]:
    broken: list[str] = []
    for attr, ref in refs:
        target = local_ref_target(ref, html_file, site_root)
        if target is not None and not target.exists():
            broken.append(f"{rel(html_file, site_root)}:{attr}={ref}")
    return broken


def local_ref_target(ref: str, html_file: Path, site_root: Path) -> Path | None:
    cleaned = ref.strip()
    if not cleaned or cleaned.startswith("#") or cleaned.startswith("//"):
        return None
    parsed = urlsplit(cleaned)
    if parsed.scheme.lower() in REMOTE_SCHEMES:
        return None
    path_part = parsed.path.strip()
    if not path_part:
        return None
    candidate = _local_candidate(path_part, html_file, site_root)
    if candidate.is_dir():
        candidate = candidate / "index.html"
    return candidate if inside(candidate, site_root) else None


def local_script_refs(refs: list[tuple[str, str]], html_file: Path, site_root: Path) -> list[Path]:
    paths: list[Path] = []
    for attr, ref in refs:
        if attr != "src":
            continue
        target = local_ref_target(ref, html_file, site_root)
        if target is not None and target.suffix.lower() == ".js" and target.exists():
            paths.append(target)
    return paths


def unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def small_text(path: Path, max_bytes: int = 262144) -> str:
    try:
        if path.stat().st_size > max_bytes:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _local_candidate(path_part: str, html_file: Path, site_root: Path) -> Path:
    if path_part.startswith("/"):
        return (site_root / path_part.lstrip("/")).resolve()
    return (html_file.parent / path_part).resolve()


def form_binding_hits(form_ids: set[str], script_text: str) -> list[str]:
    hits: list[str] = []
    for form_id in sorted(set(_VALIDATE_FORM_CALL_RE.findall(script_text or ""))):
        if form_id not in form_ids:
            hits.append(f"validateForm:{form_id}")
    return hits


def missing_dom_id_hits(
    element_ids: set[str],
    script_text: str,
    *,
    allow_optional_missing: bool = True,
) -> list[str]:
    available_ids = set(element_ids) | template_declared_dom_ids(script_text)
    hits = _missing_get_element_hits(available_ids, script_text, allow_optional_missing=allow_optional_missing)
    hits.extend(_missing_query_selector_hits(available_ids, script_text))
    return hits


def template_declared_dom_ids(script_text: str) -> set[str]:
    return {item for item in _TEMPLATE_ID_ATTR_RE.findall(script_text or "") if item}


def inert_control_hits(request: InertControlCheckRequest) -> list[str]:
    hits: list[str] = []
    has_script_handlers = "addEventListener" in request.html_text
    for control in request.controls:
        hits.extend(_inert_control_hit(control, request, has_script_handlers))
    return hits


def _missing_get_element_hits(
    element_ids: set[str],
    script_text: str,
    *,
    allow_optional_missing: bool,
) -> list[str]:
    hits: list[str] = []
    for target in sorted(set(_GET_ELEMENT_BY_ID_RE.findall(script_text or ""))):
        if target in element_ids:
            continue
        if _is_optionally_guarded_dom_lookup(script_text, target):
            continue
        hits.append(f"getElementById:{target}")
    return hits


def _missing_query_selector_hits(element_ids: set[str], script_text: str) -> list[str]:
    return [
        f"querySelector:{target}"
        for target in sorted(set(_QUERY_SELECTOR_ID_RE.findall(script_text or "")))
        if target not in element_ids
    ]


def _get_element_assignment_vars(script_text: str) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for match in _ASSIGNED_GET_ELEMENT_RE.finditer(script_text or ""):
        var_name, target = match.groups()
        mapping.setdefault(target, []).append(var_name)
    return mapping


def _is_optionally_guarded_dom_lookup(script_text: str, target: str) -> bool:
    for var_name in _get_element_assignment_vars(script_text).get(target, []):
        escaped = re.escape(var_name)
        if re.search(rf"\b{escaped}\s*&&", script_text) or re.search(rf"if\s*\(\s*{escaped}\s*\)", script_text):
            return True
    return False


def _inert_control_hit(
    control: dict[str, object],
    request: InertControlCheckRequest,
    has_script_handlers: bool,
) -> list[str]:
    tag = str(control.get("tag") or "")
    text = str(control.get("text") or "").strip()
    href = str(control.get("href") or "").strip()
    onclick = str(control.get("onclick") or "").strip()
    button_type = str(control.get("type") or "").strip().lower()
    disabled = bool(control.get("disabled"))
    if disabled and tag in {"button", "input", "select", "textarea"}:
        return [f"{request.rel_path}:{tag}:{text or '<empty>'} disabled"]
    if tag == "a" and not onclick and _anchor_is_inert(href, request.element_ids):
        return [f"{request.rel_path}:a:{text or '<empty>'} href={href or '<empty>'}"]
    if tag == "button" and not onclick and button_type not in {"submit", "reset"} and not has_script_handlers:
        return [f"{request.rel_path}:button:{text or '<empty>'}"]
    return []


def _anchor_is_inert(href: str, element_ids: set[str]) -> bool:
    cleaned = href.strip()
    if not cleaned or cleaned == "#":
        return True
    if cleaned.startswith("#"):
        return cleaned[1:] not in element_ids
    return False


def missing_window_app_method_hits(script_text: str) -> list[str]:
    refs = _referenced_window_app_methods(script_text)
    if not refs:
        return []
    exported = _exported_window_app_methods(script_text)
    return [f"app.{name}" for name in sorted(refs - exported)]


def _referenced_window_app_methods(script_text: str) -> set[str]:
    return {
        name
        for name in re.findall(r"\bapp\.([A-Za-z_$][\w$]*)\s*\(", script_text or "")
        if name not in {"addEventListener"}
    }


def _exported_window_app_methods(script_text: str) -> set[str]:
    text = script_text or ""
    names = {match.group(1) for match in re.finditer(r"\bwindow\.app\.([A-Za-z_$][\w$]*)\s*=", text)}
    for body in re.findall(r"\bwindow\.app\s*=\s*\{(?P<body>.*?)\}\s*;", text, flags=re.DOTALL):
        names.update(_object_property_names(body))
    for body in re.findall(
        r"\b(?:const|let|var)\s+app\s*=\s*\{(?P<body>.*?)\}\s*;",
        text,
        flags=re.DOTALL,
    ):
        names.update(_object_property_names(body))
    for body in re.findall(
        r"\b(?:const|let|var)\s+app\s*=\s*\{(?P<body>.*?)\}\s*;\s*window\.app\s*=\s*app\s*;",
        text,
        flags=re.DOTALL,
    ):
        names.update(_object_property_names(body))
    return names


def _object_property_names(body: str) -> set[str]:
    names: set[str] = set()
    cleaned = re.sub(r"//.*?$|/\*.*?\*/", "", body or "", flags=re.MULTILINE | re.DOTALL)
    for chunk in cleaned.split(","):
        item = chunk.strip()
        if not item:
            continue
        match = re.match(r"([A-Za-z_$][\w$]*)\s*:", item)
        if match:
            names.add(match.group(1))
            continue
        match = re.match(r"([A-Za-z_$][\w$]*)\s*(?:\(|$)", item)
        if match:
            names.add(match.group(1))
    return names


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
