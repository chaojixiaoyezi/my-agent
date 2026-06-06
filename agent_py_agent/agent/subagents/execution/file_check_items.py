
from __future__ import annotations

from pathlib import Path
from typing import Any


def expand_file_check_items(
    tests: list[dict[str, Any]],
    *,
    artifact_paths: list[tuple[str, Path]],
    workspace_root: Path,
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for item in tests:
        expanded.extend(_expanded_file_check_item(item, artifact_paths=artifact_paths, workspace_root=workspace_root))
    return expanded


def _expanded_file_check_item(
    item: dict[str, Any],
    *,
    artifact_paths: list[tuple[str, Path]],
    workspace_root: Path,
) -> list[dict[str, Any]]:
    if not _is_file_check_item(item):
        return [item]
    if str(item.get("file_path") or item.get("path") or "").strip():
        return [_explicit_file_check_item(item)]
    if not artifact_paths:
        clone = dict(item)
        clone["validation_method"] = "file_check"
        return [clone]
    return [
        _file_check_item_for_target(item, path, artifact_paths=artifact_paths, workspace_root=workspace_root)
        for _raw, path in artifact_paths
    ]


def _explicit_file_check_item(item: dict[str, Any]) -> dict[str, Any]:
    clone = dict(item)
    clone["validation_method"] = "file_check"
    if not str(clone.get("file_path") or "").strip() and str(clone.get("path") or "").strip():
        clone["file_path"] = clone["path"]
    return clone


def _is_file_check_item(item: dict[str, Any]) -> bool:
    method = str(item.get("validation_method") or "command").strip().lower().replace("-", "_")
    return method == "file_check"


def _file_check_item_for_target(
    item: dict[str, Any],
    path: Path,
    *,
    artifact_paths: list[tuple[str, Path]],
    workspace_root: Path,
) -> dict[str, Any]:
    clone = dict(item)
    clone["validation_method"] = "file_check"
    clone["file_path"] = _relative_or_absolute(path, workspace_root)
    if len(artifact_paths) > 1:
        clone["name"] = f"{str(item.get('name') or 'file exists').strip()} {path.name}"
    return clone


def _relative_or_absolute(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)) or "."
    except ValueError:
        return str(path)
