# LLM: Role template resolution maps natural LLM role strings to active template ids.
# 模块用途: 承接 role 模板目录的运行时匹配逻辑，避免 role_templates.py 继续变成大文件。

from __future__ import annotations

from typing import Any


# LLM: resolve_role_template_id is the safety net between free-form role text and template files.
# 函数用途: 先精确匹配模板 id，再按完整 token 匹配前后缀；找不到时按 fallback 回退。
def resolve_role_template_id(store: Any, role: str, *, fallback: str | None = None) -> str:
    cleaned = _clean_role_id(role)
    if not cleaned:
        return _valid_fallback(store, fallback)
    exact = store.get(cleaned)
    if exact is not None:
        return exact.id
    match = _best_template_id_match(store, cleaned)
    if match:
        return match
    return _valid_fallback(store, fallback)


# LLM: _best_template_id_match prefers the longest role template id embedded as a token.
# 函数用途: 让 slide_ppt_polisher_lead 命中 ppt_polisher，同时让 qa_tester 命中 tester。
def _best_template_id_match(store: Any, cleaned_role: str) -> str:
    matches = [
        template.id
        for template in store.all()
        if _contains_template_token(cleaned_role, template.id)
    ]
    return max(matches, key=len, default="")


# LLM: _contains_template_token avoids accidental substring hits inside unrelated words.
# 函数用途: 判断模板 id 是否作为下划线分隔的完整片段出现在自然角色名里。
def _contains_template_token(cleaned_role: str, template_id: str) -> bool:
    haystack = f"_{cleaned_role}_"
    needle = f"_{_clean_role_id(template_id)}_"
    return needle in haystack


# LLM: _valid_fallback only returns fallback ids that exist in the active template store.
# 函数用途: 防止配置或调用方写错 fallback 后继续引用不存在模板。
def _valid_fallback(store: Any, fallback: str | None) -> str:
    cleaned = _clean_role_id(fallback)
    if cleaned and store.get(cleaned):
        return cleaned
    return ""


# LLM: _clean_role_id mirrors role template id normalization without importing role_templates.
# 函数用途: 统一 role 名的大小写和连接符，供解析模块独立工作。
def _clean_role_id(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_")
