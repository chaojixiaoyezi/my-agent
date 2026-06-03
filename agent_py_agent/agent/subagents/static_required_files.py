
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..common.value_parsing import dedupe_strings, sequence_strings
from .required_file_terms import (
    labeled_required_file_terms_from_text,
    required_file_terms_from_text,
)

_STATIC_FILE_SUFFIXES = {".html", ".htm", ".css", ".js"}
_REQUIRED_DOM_IDS_RE = re.compile(r"\brequired_dom_ids?\s*[:=]\s*([^\n。；;]+)", re.IGNORECASE)


def static_required_files_from_texts(texts: list[object]) -> list[str]:
    matches = [
        match
        for value in texts
        for match in [
            *required_file_terms_from_text(str(value or ""), extensions=r"html?|css|js"),
            *labeled_required_file_terms_from_text(str(value or ""), extensions=r"html?|css|js"),
        ]
    ]
    return dedupe_strings(matches)[:50]


def required_static_dom_ids_from_texts(texts: list[object]) -> list[str]:
    ids: list[str] = []
    for value in texts:
        for match in _REQUIRED_DOM_IDS_RE.findall(str(value or "")):
            ids.extend(_dom_id_items(match))
    return dedupe_strings(ids)[:50]


def required_static_files_for_task(task: Any) -> list[str]:
    files = sequence_strings(_task_attributes(task).get("required_files"), allow_scalar=True)
    return _scope_to_allowed_write_files(files, getattr(task, "allowed_write_roots", []) or [])


def required_static_dom_ids_for_task(task: Any) -> list[str]:
    return dedupe_strings(sequence_strings(_task_attributes(task).get("required_dom_ids"), allow_scalar=True))[:50]


def _task_attributes(task: Any) -> dict[str, Any]:
    attributes = getattr(task, "attributes", {})
    return attributes if isinstance(attributes, dict) else {}


def static_site_root_hints_for_task(task: Any) -> list[str]:
    hints: list[str] = []
    for value in getattr(task, "allowed_write_roots", []) or []:
        raw = str(value or "").strip()
        if raw and raw not in hints:
            hints.append(raw)
    return hints


def _scope_to_allowed_write_files(files: list[str], allowed_write_roots: list[object]) -> list[str]:
    allowed = _allowed_static_file_names(allowed_write_roots)
    if not allowed:
        return files
    scoped = [item for item in files if _matches_allowed_file(item, allowed)]
    return scoped or files


def _allowed_static_file_names(values: list[object]) -> set[str]:
    names: set[str] = set()
    for value in values:
        path = Path(str(value or "").strip().replace("\\", "/"))
        if path.suffix.lower() not in _STATIC_FILE_SUFFIXES:
            continue
        names.add(path.name)
        parts = [part for part in path.parts if part not in {"", "."}]
        if len(parts) >= 2:
            names.add("/".join(parts[-2:]))
    return names


def _matches_allowed_file(item: str, allowed: set[str]) -> bool:
    normalized = str(item or "").replace("\\", "/").lstrip("./")
    return normalized in allowed or Path(normalized).name in allowed


def _dom_id_items(value: str) -> list[str]:
    items: list[str] = []
    for raw in re.split(r"[\s,，]+", value):
        text = raw.strip()
        if text and all(ch.isalnum() or ch in {"-", "_", ":"} for ch in text):
            items.append(text)
    return items
