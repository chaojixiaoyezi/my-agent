# LLM: DOM checks keep static-site validation focused without needing a browser.
# 模块用途: 检查表单绑定、DOM id 引用和明显失效控件，供 static_site_validator 复用。

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


# LLM: InertControlCheckRequest bundles control-scan inputs to keep validator signatures small.
# 类用途: 保存控件列表、相对文件名、HTML/脚本文本和已有 id 集合。
@dataclass(frozen=True)
class InertControlCheckRequest:
    controls: list[dict[str, object]]
    rel_path: str
    html_text: str
    element_ids: set[str]


# LLM: form_binding_hits catches generated JS bound to non-existent form ids.
# 函数用途: 检查 `validateForm('id')` 目标是否存在，防止登录/注册提交按钮假可用。
def form_binding_hits(form_ids: set[str], script_text: str) -> list[str]:
    hits: list[str] = []
    for form_id in sorted(set(_VALIDATE_FORM_CALL_RE.findall(script_text or ""))):
        if form_id not in form_ids:
            hits.append(f"validateForm:{form_id}")
    return hits


# LLM: missing_dom_id_hits catches unguarded JS selectors that point at absent local ids.
# 函数用途: 检查 `getElementById` 和 `querySelector('#id')` 的目标是否存在；已空值保护的可选 hook 不算硬失败。
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


# LLM: template_declared_dom_ids treats JS-rendered static templates as available DOM candidates.
# 函数用途: 静态站点常用 JS 模板渲染页面片段；验收时把脚本文本中的 id="..." 纳入候选集合，避免误报动态静态页。
def template_declared_dom_ids(script_text: str) -> set[str]:
    return {item for item in _TEMPLATE_ID_ATTR_RE.findall(script_text or "") if item}


# LLM: inert_control_hits finds controls that are unavailable or have no target/handler.
# 函数用途: 标记默认不可用控件和明显无动作控件；表单 submit/reset 和全局监听页面会跳过误报。
def inert_control_hits(request: InertControlCheckRequest) -> list[str]:
    hits: list[str] = []
    has_script_handlers = "addEventListener" in request.html_text
    for control in request.controls:
        hits.extend(_inert_control_hit(control, request, has_script_handlers))
    return hits


# LLM: _missing_get_element_hits treats explicit null guards as an optional UI hook contract.
# 函数用途: 缺失 id 只有在未保护使用时才失败；强制必须存在的元素应通过 required_dom_ids 声明。
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


# LLM: _missing_query_selector_hits reports unguarded selector ids that are absent from local HTML.
# 函数用途: 检查 querySelector/querySelectorAll 里的 #id 是否真实存在。
def _missing_query_selector_hits(element_ids: set[str], script_text: str) -> list[str]:
    return [
        f"querySelector:{target}"
        for target in sorted(set(_QUERY_SELECTOR_ID_RE.findall(script_text or "")))
        if target not in element_ids
    ]


# LLM: _get_element_assignment_vars maps DOM ids back to optional local variables.
# 函数用途: 识别 `const x = document.getElementById('id')` 的变量名。
def _get_element_assignment_vars(script_text: str) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for match in _ASSIGNED_GET_ELEMENT_RE.finditer(script_text or ""):
        var_name, target = match.groups()
        mapping.setdefault(target, []).append(var_name)
    return mapping


# LLM: _is_optionally_guarded_dom_lookup avoids false failures for intentionally optional UI hooks.
# 函数用途: 如果缺失 id 只通过 `var && ...` 或 `if (var)` 保护使用，就不当成硬失败。
def _is_optionally_guarded_dom_lookup(script_text: str, target: str) -> bool:
    for var_name in _get_element_assignment_vars(script_text).get(target, []):
        escaped = re.escape(var_name)
        if re.search(rf"\b{escaped}\s*&&", script_text) or re.search(rf"if\s*\(\s*{escaped}\s*\)", script_text):
            return True
    return False


# LLM: _inert_control_hit renders one generic control finding at most.
# 函数用途: 根据 disabled/tag/href/onclick/type 判断单个控件是否默认不可用或明显无动作。
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


# LLM: _anchor_is_inert separates real in-page anchors from placeholder or missing hash links.
# 函数用途: 判断 a 标签是否明显不会跳转到有效目标；href="#" 和不存在的 "#id" 都算失效控件。
def _anchor_is_inert(href: str, element_ids: set[str]) -> bool:
    cleaned = href.strip()
    if not cleaned or cleaned == "#":
        return True
    if cleaned.startswith("#"):
        return cleaned[1:] not in element_ids
    return False
