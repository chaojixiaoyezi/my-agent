
from __future__ import annotations

import re
from pathlib import Path

_URL_PATTERN = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s\"'<>]*")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def url_spans(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _URL_PATTERN.finditer(text)]


def overlaps_spans(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def suggest_workspace_typo_target(raw: str, workspace_roots: Path | list[Path]) -> str:
    if _WINDOWS_ABSOLUTE_RE.match(raw):
        return ""
    candidate = Path(raw.replace("\\", "/")).expanduser()
    if not candidate.is_absolute():
        return ""
    for root in _workspace_roots(workspace_roots):
        suggested = _suggest_workspace_root_tail(root, candidate.parts)
        if suggested:
            return suggested
    return ""


def _suggest_workspace_root_tail(root: Path, candidate_parts: tuple[str, ...]) -> str:
    tail = _tail_after_part(candidate_parts, root.name)
    if not tail:
        return ""
    suggestion = root.joinpath(*tail).resolve(strict=False)
    return str(suggestion) if _is_relative_to(suggestion, root) else ""


def _tail_after_part(parts: tuple[str, ...], marker: str) -> tuple[str, ...]:
    if not marker:
        return ()
    try:
        index = parts.index(marker)
    except ValueError:
        return ()
    return parts[index + 1 :]


def _workspace_roots(workspace_roots: Path | list[Path]) -> list[Path]:
    raw_roots = workspace_roots if isinstance(workspace_roots, list) else [workspace_roots]
    return [Path(root).resolve(strict=False) for root in raw_roots]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
