
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_LEGACY_SUBAGENT_PATH_PATTERN = re.compile(
    r"(?P<path>(?:~|[A-Za-z]:|/|\.{1,2}/)?[^\s\"'<>，。；；、,]*data/subagents/[^\s\"'<>，。；；、,]*)"
)


def is_legacy_subagent_path(value: str) -> bool:
    text = str(value or "").replace("\\", "/")
    return "data/subagents/" in text


def current_model_ref(value: Any, *, basename_for_legacy: bool = False) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not is_legacy_subagent_path(text):
        return text
    if basename_for_legacy:
        return Path(text.replace("\\", "/")).name
    return ""


def current_model_text(value: Any, *, basename_for_legacy: bool = True) -> str:
    """Return model-visible free text with deprecated subagent paths removed."""
    text = str(value or "")
    if not text or not is_legacy_subagent_path(text):
        return text
    return scrub_legacy_subagent_paths_in_text(text, basename_for_legacy=basename_for_legacy)


def scrub_legacy_subagent_paths_in_text(value: str, *, basename_for_legacy: bool = True) -> str:
    text = str(value or "")
    if not text or not is_legacy_subagent_path(text):
        return text

    def repl(match: re.Match[str]) -> str:
        path = match.group("path").replace("\\", "/")
        if basename_for_legacy:
            return Path(path).name or "[internal_legacy_subagent_path_hidden]"
        return "[internal_legacy_subagent_path_hidden]"

    return _LEGACY_SUBAGENT_PATH_PATTERN.sub(repl, text)


def current_model_ref_list(values: Any, *, basename_for_legacy: bool = False, limit: int = 0) -> list[str]:
    if not isinstance(values, list | tuple | set):
        return []
    refs: list[str] = []
    seen: set[str] = set()
    for value in values:
        ref = current_model_ref(value, basename_for_legacy=basename_for_legacy)
        if not ref or ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
        if limit and len(refs) >= limit:
            break
    return refs


__all__ = [
    "current_model_ref",
    "current_model_ref_list",
    "current_model_text",
    "is_legacy_subagent_path",
    "scrub_legacy_subagent_paths_in_text",
]
