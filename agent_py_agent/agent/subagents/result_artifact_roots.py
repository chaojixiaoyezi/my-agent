
from __future__ import annotations

from pathlib import Path
from typing import Any

_ROOT_ATTRIBUTES = (
    "output_dir",
    "reports_dir",
    "agent_run_workspace_dir",
    "task_workspace_artifacts_dir",
    "agent_run_artifacts_dir",
    "task_workspace_shared_dir",
    "task_workspace_dir",
    "task_dir",
    "data_dir",
    "scratch_dir",
)


def artifact_candidate_roots(task: Any, *, include_workspace_roots: bool = True) -> list[Path]:
    roots: list[Path] = []
    for value in _root_values(task):
        _append_existing_root(roots, value)
    if include_workspace_roots:
        _append_declared_workspace_roots(roots, task)
    return roots


def declared_workspace_roots(task: Any) -> list[Path]:
    roots: list[Path] = []
    _append_declared_workspace_roots(roots, task)
    return roots


def artifact_suffix_roots(task: Any) -> list[Path]:
    return artifact_candidate_roots(task, include_workspace_roots=False)


def _root_values(task: Any) -> list[object]:
    values = [getattr(task, name, "") for name in _ROOT_ATTRIBUTES]
    values.extend(getattr(task, "allowed_write_roots", []) or [])
    return values


def _append_existing_root(roots: list[Path], value: object) -> None:
    text = str(value or "").strip()
    if not text:
        return
    try:
        path = Path(text).expanduser()
    except OSError:
        return
    root = path if path.is_dir() else path.parent
    if root.exists() and root.is_dir() and root not in roots:
        roots.append(root)


def _append_declared_workspace_roots(roots: list[Path], task: Any) -> None:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return
    for value in [attrs.get("workspace_root"), *list(attrs.get("workspace_roots") or [])]:
        _append_existing_workspace_root(roots, _path(value))


def _append_existing_workspace_root(roots: list[Path], workspace: Path | None) -> None:
    if workspace is not None and workspace.exists() and workspace.is_dir() and workspace not in roots:
        roots.append(workspace)


def _path(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None
