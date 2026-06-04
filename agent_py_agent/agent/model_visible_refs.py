from __future__ import annotations

from pathlib import Path
from typing import Any


def current_model_ref(value: Any) -> str:
    text = str(value or "").strip()
    return text


def current_model_text(value: Any) -> str:
    return str(value or "")


def current_model_ref_list(values: Any, *, limit: int = 0) -> list[str]:
    if not isinstance(values, list | tuple | set):
        return []
    refs: list[str] = []
    seen: set[str] = set()
    for value in values:
        ref = current_model_ref(value)
        if not ref or ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
        if limit and len(refs) >= limit:
            break
    return refs


def path_name(value: Any) -> str:
    return Path(str(value or "")).name


__all__ = [
    "current_model_ref",
    "current_model_ref_list",
    "current_model_text",
    "path_name",
]
