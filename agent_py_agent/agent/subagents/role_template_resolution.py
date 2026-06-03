
from __future__ import annotations

from typing import Any


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


def _best_template_id_match(store: Any, cleaned_role: str) -> str:
    matches = [
        template.id
        for template in store.all()
        if _contains_template_token(cleaned_role, template.id)
    ]
    return max(matches, key=len, default="")


def _contains_template_token(cleaned_role: str, template_id: str) -> bool:
    haystack = f"_{cleaned_role}_"
    needle = f"_{_clean_role_id(template_id)}_"
    return needle in haystack


def _valid_fallback(store: Any, fallback: str | None) -> str:
    cleaned = _clean_role_id(fallback)
    if cleaned and store.get(cleaned):
        return cleaned
    return ""


def _clean_role_id(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_")
