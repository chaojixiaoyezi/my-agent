"""Path safety and match-to-read-target helpers for routed memory context."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .models import MemoryRouteMatch


@dataclass
class _ReadTarget:
    """Internal representation of one safe authority file read target."""
    route_id: str
    path: str
    absolute_path: Path
    reasons: list[str] = field(default_factory=list)


def _resolve_root(root: str | Path) -> tuple[Path | None, str]:
    """Resolve the configured project root before any index or rule reads."""
    root_path = Path(root)
    try:
        resolved = root_path.resolve()
    except OSError as exc:
        return None, f"root path cannot be resolved: {root_path} ({exc})"
    if not resolved.exists():
        return None, f"root path does not exist: {root_path}"
    if not resolved.is_dir():
        return None, f"root path is not a directory: {root_path}"
    return resolved, ""


def _resolve_relative_path(root: Path, raw_path: str, *, label: str) -> tuple[Path | None, str, str]:
    """Resolve a caller-provided relative path without escaping root."""
    cleaned = raw_path.strip()
    if not cleaned:
        return None, "", f"{label} is empty"
    candidate = Path(cleaned)
    if candidate.is_absolute():
        return None, "", f"{label} must be relative, got {cleaned}"
    try:
        resolved = (root / candidate).resolve()
    except OSError as exc:
        return None, "", f"{label} cannot be resolved: {cleaned} ({exc})"
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return None, "", f"{label} escapes root: {cleaned}"
    return resolved, relative.as_posix(), ""


def _read_targets_from_matches(
    matches: list[MemoryRouteMatch],
    root: Path,
    findings: list[str],
) -> list[_ReadTarget]:
    """Convert matched routes into unique safe read targets."""
    targets: list[_ReadTarget] = []
    seen_paths: set[str] = set()
    for match in matches:
        raw_path = match.route.authority_file()
        if not raw_path:
            continue
        absolute_path, normalized_path, error = _resolve_relative_path(root, raw_path, label="source_file")
        if error:
            _append_finding(findings, _authority_path_finding(match, error, raw_path))
            continue
        if absolute_path is None or normalized_path in seen_paths:
            continue
        seen_paths.add(normalized_path)
        targets.append(
            _ReadTarget(
                route_id=match.route.route_id,
                path=normalized_path,
                absolute_path=absolute_path,
                reasons=list(match.reasons),
            )
        )
    return targets


def _authority_path_finding(match: MemoryRouteMatch, error: str, raw_path: str) -> str:
    """Format authority path safety findings like the route validator."""
    label = match.route.route_id or "<empty route_id>"
    if "must be relative" in error:
        return f"route '{label}': authority_path must be relative, got {raw_path}"
    if "escapes root" in error:
        return f"route '{label}': authority_path escapes root: {raw_path}"
    if "cannot be resolved" in error:
        return f"route '{label}': authority_path cannot be resolved ({error})"
    return f"route '{label}': {error}"


def _append_finding(findings: list[str], finding: str) -> None:
    """Append a diagnostic once while preserving first-seen order."""
    if finding and finding not in findings:
        findings.append(finding)
