
from __future__ import annotations

from pathlib import Path
from typing import Any

from .workspace_roots import derived_workspace_roots_from_subagent_path

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
        _append_derived_workspace_roots(roots)
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


def _append_derived_workspace_roots(roots: list[Path]) -> None:
    for workspace in _derived_workspace_roots(roots):
        _append_existing_workspace_root(roots, workspace)


def _derived_workspace_roots(roots: list[Path]) -> list[Path]:
    derived: list[Path] = []
    for root in list(roots):
        derived.extend(derived_workspace_roots_from_subagent_path(root))
    return derived


def _append_existing_workspace_root(roots: list[Path], workspace: Path) -> None:
    if workspace.exists() and workspace.is_dir() and workspace not in roots:
        roots.append(workspace)
