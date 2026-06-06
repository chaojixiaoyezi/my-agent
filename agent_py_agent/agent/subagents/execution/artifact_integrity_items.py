
from __future__ import annotations

from pathlib import Path
from typing import Any


def expand_artifact_integrity_items(
    tests: list[dict[str, Any]],
    *,
    artifact_paths: list[tuple[str, Path]],
    workspace_root: Path,
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for item in tests:
        expanded.extend(_expanded_item(item, artifact_paths=artifact_paths, workspace_root=workspace_root))
    return expanded


def _expanded_item(
    item: dict[str, Any],
    *,
    artifact_paths: list[tuple[str, Path]],
    workspace_root: Path,
) -> list[dict[str, Any]]:
    if not _needs_artifact_targets(item) or not artifact_paths:
        return [item]
    return [_item_for_target(item, path, artifact_paths=artifact_paths, workspace_root=workspace_root) for _raw, path in artifact_paths]


def _needs_artifact_targets(item: dict[str, Any]) -> bool:
    method = str(item.get("validation_method") or "command").strip().lower()
    if method != "artifact_integrity":
        return False
    return not str(item.get("file_path") or "").strip()


def _item_for_target(
    item: dict[str, Any],
    path: Path,
    *,
    artifact_paths: list[tuple[str, Path]],
    workspace_root: Path,
) -> dict[str, Any]:
    clone = dict(item)
    clone["file_path"] = _relative_or_absolute(path, workspace_root)
    if len(artifact_paths) > 1:
        clone["name"] = f"{str(item.get('name') or 'artifact integrity').strip()} {path.name}"
    return clone


def _relative_or_absolute(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)) or "."
    except ValueError:
        return str(path)
