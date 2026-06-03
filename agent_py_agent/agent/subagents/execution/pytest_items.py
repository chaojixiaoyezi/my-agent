
from __future__ import annotations

from pathlib import Path
from typing import Any


def artifact_pytest_items(
    artifact_paths: list[tuple[str, Path]],
    *,
    workspace_root: Path,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for _raw, path in artifact_paths:
        if path in seen or not _is_pytest_artifact(path):
            continue
        seen.add(path)
        items.append({
            "name": f"artifact pytest {path.name}",
            "validation_method": "command",
            "command": f"python3 -m pytest {path.name} -q",
            "working_dir": _relative_or_absolute(path.parent, workspace_root),
        })
    return items


def _is_pytest_artifact(path: Path) -> bool:
    return path.is_file() and path.suffix == ".py" and path.name.startswith("test_")


def _relative_or_absolute(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)) or "."
    except ValueError:
        return str(path)
