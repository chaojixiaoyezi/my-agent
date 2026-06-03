
from __future__ import annotations

import re
from pathlib import Path

from .models import SubAgentTask

_SAFE_FILE_SUFFIX_RE = re.compile(r"^\.[a-z0-9][a-z0-9._+-]{0,63}$")


def file_level_write_root_terms(task: SubAgentTask) -> list[str]:
    terms: list[str] = []
    task_dir = Path(str(getattr(task, "task_dir", "") or ""))
    for raw in getattr(task, "allowed_write_roots", []) or []:
        path = Path(str(raw or "").strip().replace("\\", "/"))
        if not is_contract_file_path(path) or is_internal_task_file(path, task_dir):
            continue
        add_file_root_term(terms, path.name)
        if len(path.parts) >= 2:
            add_file_root_term(terms, "/".join(path.parts[-2:]))
    return terms


def is_contract_file_path(path: Path) -> bool:
    return bool(path.name and _SAFE_FILE_SUFFIX_RE.fullmatch(path.suffix.lower()))


def is_internal_task_file(path: Path, task_dir: Path) -> bool:
    if not str(task_dir):
        return False
    try:
        return path.resolve().is_relative_to(task_dir.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def add_file_root_term(terms: list[str], value: str) -> None:
    text = str(value or "").strip()
    if text and text not in terms:
        terms.append(text)
