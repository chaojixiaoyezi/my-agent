
from __future__ import annotations

import re
from dataclasses import dataclass

_VALIDATE_FORM_CALL_RE = re.compile(r"validateForm\(\s*['\"]([^'\"]+)['\"]\s*\)")
_GET_ELEMENT_BY_ID_RE = re.compile(r"getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)")
_QUERY_SELECTOR_ID_RE = re.compile(r"querySelector(?:All)?\(\s*['\"]#([A-Za-z0-9_-]+)['\"]\s*\)")
_TEMPLATE_ID_ATTR_RE = re.compile(r"\bid\s*=\s*['\"]([A-Za-z0-9_-]+)['\"]")
_ASSIGNED_GET_ELEMENT_RE = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*document\.getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)\s*;"
)


@dataclass(frozen=True)
class InertControlCheckRequest:
    controls: list[dict[str, object]]
    rel_path: str
    html_text: str
    element_ids: set[str]


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
