
from __future__ import annotations

from pathlib import Path

_SUBAGENT_WORKSPACE_MARKERS = (
    ("data", "subagents"),
    (".my_agent", "subagents"),
    (".my-agent", "subagents"),
)


def derived_workspace_roots_from_subagent_path(path: Path) -> list[Path]:
    parts = Path(path).parts
    roots: list[Path] = []
    for index in _subagent_workspace_marker_indexes(parts):
        candidate = Path(*parts[:index])
        if _usable_workspace_root(candidate, path, roots):
            roots.append(candidate)
    return roots


def _subagent_workspace_marker_indexes(parts: tuple[str, ...]) -> list[int]:
    return [
        index
        for index in range(1, len(parts) - 2)
        if any(_matches_marker(parts, index, marker) for marker in _SUBAGENT_WORKSPACE_MARKERS)
    ]


def _matches_marker(parts: tuple[str, ...], index: int, marker: tuple[str, ...]) -> bool:
    return tuple(parts[index:index + len(marker)]) == marker


def _usable_workspace_root(candidate: Path, original: Path, roots: list[Path]) -> bool:
    if candidate == candidate.parent:
        return False
    return str(candidate) not in {"", "."} and candidate != original and candidate not in roots
