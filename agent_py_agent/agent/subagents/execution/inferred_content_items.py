
from __future__ import annotations

"""Infer bounded content_check tests for plain file artifacts."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar


@dataclass(frozen=True)
class ContentCheckInferenceRequest:
    """Inputs for inferring content_check items."""

    __test__: ClassVar[bool] = False

    artifact_paths: list[tuple[str, Path]]
    workspace_root: Path
    existing_tests: list[dict[str, Any]]
    required_lines: list[str] = field(default_factory=list)
    required_files: dict[str, list[str]] = field(default_factory=dict)


def inferred_content_check_items(request: ContentCheckInferenceRequest) -> list[dict[str, Any]]:
    """Return inferred content_check tests for one clear plain-file artifact."""

    if _has_content_check(request.existing_tests):
        return []
    mapped = _mapped_content_check_items(request)
    if mapped:
        return mapped
    if not request.required_lines:
        return []
    target = _single_content_artifact_path(request.artifact_paths)
    if target is None:
        return []
    file_path = _relative_or_absolute(target, request.workspace_root)
    return [
        {
            "name": f"inferred content check {index}",
            "validation_method": "content_check",
            "file_path": file_path,
            "content_pattern": line,
        }
        for index, line in enumerate(_dedupe_texts(request.required_lines), start=1)
    ]


def _mapped_content_check_items(request: ContentCheckInferenceRequest) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for file_key, lines in request.required_files.items():
        target = _artifact_path_for_key(file_key, request.artifact_paths, request.workspace_root)
        if target is None:
            continue
        file_path = _relative_or_absolute(target, request.workspace_root)
        label = Path(file_key).name or "artifact"
        items.extend(
            {
                "name": f"inferred content check {label} {index}",
                "validation_method": "content_check",
                "file_path": file_path,
                "content_pattern": line,
            }
            for index, line in enumerate(_dedupe_texts(lines), start=1)
        )
    return items


def _artifact_path_for_key(file_key: str, artifact_paths: list[tuple[str, Path]], workspace_root: Path) -> Path | None:
    key = _normalized_key(file_key)
    matches = [(_artifact_match_kind(key, raw, path, workspace_root), path) for raw, path in artifact_paths]
    exact = _paths_for_match(matches, "exact")
    basename = _paths_for_match(matches, "basename")
    return _single_path(exact) or (_single_path(basename) if not exact else None)


def _paths_for_match(matches: list[tuple[str, Path]], kind: str) -> list[Path]:
    return [path for match_kind, path in matches if match_kind == kind]


def _single_path(paths: list[Path]) -> Path | None:
    return paths[0] if len(paths) == 1 else None


def _artifact_match_kind(key: str, raw: str, path: Path, workspace_root: Path) -> str:
    if not _plain_file_artifact(path):
        return ""
    raw_key = _normalized_key(raw)
    rel_key = _normalized_key(_relative_or_absolute(path, workspace_root))
    if key in {raw_key, rel_key}:
        return "exact"
    if key and Path(key).name == path.name:
        return "basename"
    return ""


def _has_content_check(tests: list[dict[str, Any]]) -> bool:
    return any(
        str(item.get("validation_method") or "").strip().lower() == "content_check"
        and str(item.get("file_path") or "").strip()
        and _content_pattern_value(item)
        for item in tests
    )


def _content_pattern_value(item: dict[str, Any]) -> str:
    for key in ("content_equals", "expected_content", "content_pattern"):
        value = str(item.get(key) or "")
        if value:
            return value
    return ""


def _single_content_artifact_path(artifact_paths: list[tuple[str, Path]]) -> Path | None:
    candidates: list[Path] = []
    for _raw, path in artifact_paths:
        if not _plain_file_artifact(path):
            continue
        if path not in candidates:
            candidates.append(path)
    return candidates[0] if len(candidates) == 1 else None


def _plain_file_artifact(path: Path) -> bool:
    if path.name.startswith("test_") and path.suffix == ".py":
        return False
    if path.exists() and path.is_dir():
        return False
    return path.suffix.lower() not in {".html", ".htm"}


def _normalized_key(value: object) -> str:
    return str(value or "").strip().strip("'\"").replace("\\", "/").lstrip("./")


def _relative_or_absolute(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)) or "."
    except ValueError:
        return str(path)


def _dedupe_texts(values: list[str]) -> list[str]:
    items: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in items:
            items.append(text)
    return items
