
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar


@dataclass(frozen=True)
class StaticSiteTestItemsRequest:
    """Bundle inputs for static-site test inference."""

    __test__: ClassVar[bool] = False

    artifact_paths: list[tuple[str, Path]]
    workspace_root: Path
    existing_tests: list[dict[str, Any]]
    required_files: list[str] = field(default_factory=list)
    required_dom_ids: list[str] = field(default_factory=list)
    site_root_hints: list[object] = field(default_factory=list)


def inferred_static_site_items(request: StaticSiteTestItemsRequest) -> list[dict[str, Any]]:
    if _has_static_site_check(request.existing_tests):
        return []
    html_paths = _html_artifact_paths(request.artifact_paths)
    declared_files = _normalized_required_files(request.required_files)
    if html_paths:
        site_root = _common_parent(html_paths)
        observed_required = _required_files(html_paths, site_root)
        required_files = (
            _merged_required_files(observed_required, declared_files)
            if len(html_paths) > 1
            else observed_required
        )
    else:
        if not declared_files:
            return []
        site_root = _required_site_root(
            declared_files,
            request.workspace_root,
            site_root_hints=request.site_root_hints,
        )
        required_files = declared_files
    item = {
        "name": "inferred static site check",
        "validation_method": "static_site_check",
        "site_root": _relative_or_absolute(site_root, request.workspace_root),
        "required_files": required_files,
        "require_complete_html": True,
    }
    if request.required_dom_ids:
        item["required_dom_ids"] = _normalized_required_dom_ids(request.required_dom_ids)
    if len(html_paths) == 1:
        item["html_files"] = required_files
    return [item]


def _has_static_site_check(tests: list[dict[str, Any]]) -> bool:
    return any(
        str(item.get("validation_method") or "").strip().lower() == "static_site_check"
        and str(item.get("site_root") or "").strip()
        for item in tests
    )


def _html_artifact_paths(artifact_paths: list[tuple[str, Path]]) -> list[Path]:
    seen: set[Path] = set()
    paths: list[Path] = []
    for _raw, path in artifact_paths:
        if path in seen or path.suffix.lower() not in {".html", ".htm"}:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _common_parent(paths: list[Path]) -> Path:
    parents = [path.parent for path in paths]
    common = Path(*Path(*parents[0].parts).parts)
    for parent in parents[1:]:
        common = _shared_prefix(common, parent)
    return common


def _shared_prefix(left: Path, right: Path) -> Path:
    parts = []
    for left_part, right_part in zip(left.parts, right.parts):
        if left_part != right_part:
            break
        parts.append(left_part)
    return Path(*parts) if parts else Path(".")


def _required_files(paths: list[Path], site_root: Path) -> list[str]:
    files: list[str] = []
    for path in sorted(paths, key=lambda item: str(item)):
        try:
            value = str(path.relative_to(site_root))
        except ValueError:
            value = path.name
        if value not in files:
            files.append(value)
    return files


def _normalized_required_files(values: list[str]) -> list[str]:
    files: list[str] = []
    for value in values:
        raw = str(value or "").strip().replace("\\", "/").lstrip("./")
        if not raw or raw.startswith("/") or ".." in Path(raw).parts:
            continue
        if raw not in files:
            files.append(raw)
    return files


def _normalized_required_dom_ids(values: list[str]) -> list[str]:
    ids: list[str] = []
    for value in values:
        raw = str(value or "").strip()
        if not raw or len(raw) > 80:
            continue
        if not all(ch.isalnum() or ch in {"-", "_", ":"} for ch in raw):
            continue
        if raw not in ids:
            ids.append(raw)
    return ids[:50]


def _merged_required_files(observed: list[str], declared: list[str]) -> list[str]:
    files: list[str] = []
    for value in [*observed, *declared]:
        if value and value not in files:
            files.append(value)
    return sorted(files)


def _required_site_root(
    required_files: list[str],
    workspace_root: Path,
    *,
    site_root_hints: list[object] | None = None,
) -> Path:
    root = Path(workspace_root).resolve()
    candidates = [
        *_site_root_hint_candidates(site_root_hints or [], root),
        root / "artifacts",
        root / "deliverables",
        root / "outputs",
        root,
    ]
    for candidate in candidates:
        if candidate.is_dir() and _candidate_contains_required_file(candidate, required_files):
            return candidate
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return root


def _site_root_hint_candidates(values: list[object], workspace_root: Path) -> list[Path]:
    candidates: list[Path] = []
    for value in values:
        path = _workspace_candidate_root(value, workspace_root)
        if path is None or path in candidates:
            continue
        candidates.append(path)
    return candidates


def _workspace_candidate_root(value: object, workspace_root: Path) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    path = candidate if candidate.is_absolute() else workspace_root / candidate
    if path.suffix.lower() in {".html", ".htm", ".css", ".js"}:
        path = path.parent
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(workspace_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


def _candidate_contains_required_file(candidate: Path, required_files: list[str]) -> bool:
    return any((candidate / value).exists() for value in required_files)


def _relative_or_absolute(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)) or "."
    except ValueError:
        return str(path)
