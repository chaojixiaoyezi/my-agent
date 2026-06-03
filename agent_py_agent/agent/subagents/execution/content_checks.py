
from __future__ import annotations

"""Helpers for turning simple file-content commands into content_check tests."""

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar


@dataclass(frozen=True)
class CatContentCheckRequest:
    """Bundle inputs used to normalize one cat-style content assertion."""

    __test__: ClassVar[bool] = False

    item: dict[str, Any]
    workspace_root: Path
    artifact_expected_content: dict[Path, str]


def normalize_cat_content_check(request: CatContentCheckRequest) -> dict[str, Any]:
    """Return a normalized item when a cat command can be represented as content_check."""

    item = dict(request.item)
    method = str(item.get("validation_method") or "command").strip().lower() or "command"
    if method != "command":
        return item
    path = _cat_command_path(str(item.get("command") or ""), request.workspace_root)
    if path is None:
        return item
    expected = _expected_content_for_cat_item(item, path, request.artifact_expected_content)
    if not expected:
        return item
    item["validation_method"] = "content_check"
    item["file_path"] = _relative_or_absolute(path, request.workspace_root)
    item["content_equals"] = expected
    item["match_mode"] = "exact"
    return item


def _cat_command_path(command: str, workspace_root: Path) -> Path | None:
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if len(argv) != 2 or Path(argv[0]).name.lower() != "cat":
        return None
    return _workspace_path(argv[1], workspace_root)


def _workspace_path(value: object, workspace_root: Path) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (workspace_root / candidate).resolve()
    if not path.exists() and not candidate.is_absolute():
        path = _find_workspace_suffix(candidate, workspace_root) or path
    try:
        path.relative_to(workspace_root)
    except ValueError:
        return None
    return path


def _expected_content_for_cat_item(
    item: dict[str, Any],
    path: Path,
    artifact_expected_content: dict[Path, str],
) -> str:
    for key in ("content_equals", "expected_content", "content_pattern", "expected_stdout", "expected_output"):
        value = str(item.get(key) or "").strip()
        if _usable_expected_literal(value, path):
            return value
    fallback = artifact_expected_content.get(path, "")
    return fallback if _usable_expected_literal(fallback, path) else ""


def _usable_expected_literal(value: str, path: Path) -> bool:
    value = value.strip()
    if not value or len(value) > 200:
        return False
    if "/" in value or "\\" in value or value == path.name:
        return False
    lowered = value.lower()
    if lowered.endswith((".txt", ".py", ".md", ".json", ".html", ".css", ".js")):
        return False
    return True


def _find_workspace_suffix(relative_path: Path, workspace_root: Path) -> Path | None:
    parts = relative_path.parts
    if not parts:
        return None
    matches = [
        item.resolve()
        for item in workspace_root.rglob(parts[-1])
        if item.parts[-len(parts):] == parts
    ]
    return matches[0] if len(matches) == 1 else None


def _relative_or_absolute(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)) or "."
    except ValueError:
        return str(path)
