from __future__ import annotations

from collections.abc import Iterable
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


def has_placeholder_path_segment(value: object) -> bool:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return False
    try:
        parts = Path(text).parts
    except (OSError, RuntimeError):
        return True
    return any(_is_placeholder_segment(part) for part in parts)


def clean_path_contract_ref(value: object) -> str:
    text = str(value or "").strip()
    if not text or "://" in text or has_placeholder_path_segment(text):
        return ""
    return current_model_ref(text)


def clean_path_contract_refs(values: Iterable[object]) -> list[str]:
    refs: list[str] = []
    for value in values:
        ref = clean_path_contract_ref(value)
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def is_non_model_visible_locator_root(task: object, value: object) -> bool:
    """Return true for lookup/index roots that must not enter model write contracts."""

    canonical = resolved_path_text(getattr(task, "agent_run_workspace_dir", ""))
    locator = resolved_path_text(getattr(task, "task_dir", ""))
    resolved = resolved_path_text(value)
    if not canonical or not locator or not resolved:
        return False
    if canonical == locator:
        return False
    return resolved == locator or resolved.startswith(f"{locator}/")


def resolved_path_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return ""


def path_name(value: Any) -> str:
    return Path(str(value or "")).name


def _is_placeholder_segment(part: str) -> bool:
    text = str(part or "").strip()
    return len(text) >= 2 and text[0] in {"[", "【"} and text[-1] in {"]", "】"}


__all__ = [
    "clean_path_contract_ref",
    "clean_path_contract_refs",
    "current_model_ref",
    "current_model_ref_list",
    "current_model_text",
    "has_placeholder_path_segment",
    "is_non_model_visible_locator_root",
    "path_name",
    "resolved_path_text",
]
