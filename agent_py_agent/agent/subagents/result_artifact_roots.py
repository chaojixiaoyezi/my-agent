
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..model_visible_refs import has_placeholder_path_segment

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
    values = [getattr(task, name, "") for name in _ROOT_ATTRIBUTES if _include_root_attribute(task, name)]
    values.extend(_filtered_allowed_write_roots(task))
    return values


def _include_root_attribute(task: Any, name: str) -> bool:
    if name != "task_dir":
        return True
    return not str(getattr(task, "agent_run_workspace_dir", "") or "").strip()


def _filtered_allowed_write_roots(task: Any) -> list[object]:
    roots: list[object] = []
    locator = _resolved_text(getattr(task, "task_dir", ""))
    canonical = _resolved_text(getattr(task, "agent_run_workspace_dir", ""))
    for value in getattr(task, "allowed_write_roots", []) or []:
        if has_placeholder_path_segment(value):
            continue
        resolved = _resolved_text(value)
        if canonical and locator and resolved and (resolved == locator or resolved.startswith(f"{locator}/")):
            continue
        roots.append(value)
    return roots


def _append_existing_root(roots: list[Path], value: object) -> None:
    text = str(value or "").strip()
    if not text or has_placeholder_path_segment(text):
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


def _resolved_text(value: object) -> str:
    text = str(value or "").strip()
    if not text or has_placeholder_path_segment(text):
        return ""
    try:
        return str(Path(text).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return ""
