
from __future__ import annotations

"""Prepare test execution items for closeout."""

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .artifact_integrity_items import expand_artifact_integrity_items
from .content_checks import CatContentCheckRequest, normalize_cat_content_check
from .file_exists_items import expand_file_exists_items
from .inferred_content_items import (
    ContentCheckInferenceRequest,
    inferred_content_check_items,
)
from .pytest_items import artifact_pytest_items
from .static_site_items import StaticSiteTestItemsRequest, inferred_static_site_items
from .test_checklists import drop_non_executable_model_checklist_items


@dataclass(frozen=True)
class TestItemPreparationRequest:
    """Bundle inputs used to prepare runner-declared tests."""

    __test__: ClassVar[bool] = False

    tests: list[dict[str, Any]]
    output: dict[str, object]
    workspace_root: str | Path
    required_files: list[str] = field(default_factory=list)
    required_dom_ids: list[str] = field(default_factory=list)
    required_content_lines: list[str] = field(default_factory=list)
    required_content_files: dict[str, list[str]] = field(default_factory=dict)
    site_root_hints: list[object] = field(default_factory=list)


@dataclass(frozen=True)
class TestItemPreparationContext:
    """Derived metadata used while preparing one test item."""

    __test__: ClassVar[bool] = False

    artifact_dirs: dict[str, Path]
    artifact_paths: list[tuple[str, Path]]
    artifact_expected_content: dict[Path, str]
    default_dir: Path | None
    workspace_root: Path


def prepare_test_items(request: TestItemPreparationRequest) -> list[dict[str, Any]]:
    """Return test items with inferred workspace-local working directories when safe."""

    workspace_root = Path(request.workspace_root).resolve()
    artifact_dirs = _artifact_dirs_by_name(request.output, workspace_root)
    context = TestItemPreparationContext(
        artifact_dirs=artifact_dirs,
        artifact_paths=_artifact_paths(request.output, workspace_root),
        artifact_expected_content=_artifact_expected_content_by_path(request.output, workspace_root),
        default_dir=_single_artifact_dir(artifact_dirs),
        workspace_root=workspace_root,
    )
    prepared = _artifact_pytest_items(context) if not request.tests else [
        _prepared_test_item(test, context) for test in request.tests
    ]
    prepared = expand_artifact_integrity_items(
        prepared,
        artifact_paths=context.artifact_paths,
        workspace_root=workspace_root,
    )
    prepared = expand_file_exists_items(
        prepared,
        artifact_paths=context.artifact_paths,
        workspace_root=workspace_root,
    )
    inferred = inferred_static_site_items(
        StaticSiteTestItemsRequest(
            artifact_paths=context.artifact_paths,
            workspace_root=workspace_root,
            existing_tests=prepared,
            required_files=request.required_files,
            required_dom_ids=request.required_dom_ids,
            site_root_hints=request.site_root_hints,
        )
    )
    content_checks = inferred_content_check_items(
        ContentCheckInferenceRequest(
            artifact_paths=context.artifact_paths,
            workspace_root=workspace_root,
            existing_tests=prepared,
            required_lines=request.required_content_lines,
            required_files=request.required_content_files,
        )
    )
    if inferred or content_checks:
        prepared = drop_non_executable_model_checklist_items(prepared)
    return [*prepared, *inferred, *content_checks]


def _prepared_test_item(
    test: dict[str, Any],
    context: TestItemPreparationContext,
) -> dict[str, Any]:
    item = dict(test)
    _normalize_validation_method(item)
    _normalize_leading_cd_command(item, context.workspace_root)
    item = normalize_cat_content_check(
        CatContentCheckRequest(
            item=item,
            workspace_root=context.workspace_root,
            artifact_expected_content=context.artifact_expected_content,
        )
    )
    if not _needs_working_dir(item):
        return item
    command_dir = _command_artifact_working_dir(item, context.artifact_paths, context.workspace_root)
    if command_dir is not None:
        item["working_dir"] = _relative_or_absolute(command_dir, context.workspace_root)
        return item
    inferred = _named_artifact_dir(item, context.artifact_dirs) or context.default_dir
    if inferred is None:
        return item
    item["working_dir"] = _relative_or_absolute(inferred, context.workspace_root)
    return item


def _normalize_validation_method(item: dict[str, Any]) -> None:
    method = str(item.get("validation_method") or "command").strip().lower()
    if method == "command" and _static_site_command_alias(item.get("command")):
        item["validation_method"] = "static_site_check"
        item.pop("command", None)
        return
    if method in {"pytest", "unittest"} and str(item.get("command") or "").strip():
        item["validation_method"] = "command"


def _static_site_command_alias(command: object) -> bool:
    raw = str(command or "").strip()
    if not raw:
        return False
    try:
        parts = shlex.split(raw)
    except ValueError:
        return False
    if not parts:
        return False
    return parts[0].strip().lower().replace("-", "_") == "static_site_check"


def _normalize_leading_cd_command(item: dict[str, Any], workspace_root: Path) -> None:
    parsed = _safe_leading_cd_command(str(item.get("command") or ""), workspace_root)
    if parsed is None:
        return
    working_dir, command = parsed
    item["command"] = command
    if not str(item.get("working_dir") or item.get("cwd") or "").strip():
        item["working_dir"] = _relative_or_absolute(working_dir, workspace_root)


def _safe_leading_cd_command(command: str, workspace_root: Path) -> tuple[Path, str] | None:
    if command.count("&&") != 1:
        return None
    cd_part, actual = [part.strip() for part in command.split("&&", 1)]
    if not actual:
        return None
    try:
        cd_argv = shlex.split(cd_part)
    except ValueError:
        return None
    if len(cd_argv) != 2 or cd_argv[0] != "cd":
        return None
    path = _workspace_dir(cd_argv[1], workspace_root)
    if path is None:
        return None
    return path, actual


def _workspace_dir(value: object, workspace_root: Path) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (workspace_root / candidate).resolve()
    try:
        path.relative_to(workspace_root)
    except ValueError:
        return None
    return path if path.is_dir() else None


def _needs_working_dir(test: dict[str, Any]) -> bool:
    method = str(test.get("validation_method") or "command").strip() or "command"
    return method == "command" and not str(test.get("working_dir") or test.get("cwd") or "").strip()


def _artifact_dirs_by_name(output: dict[str, object], workspace_root: Path) -> dict[str, Path]:
    dirs: dict[str, Path] = {}
    for artifact in output.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        path = _workspace_path(artifact.get("path"), workspace_root)
        if path is None:
            continue
        dirs[path.name] = path.parent
    return dirs


def _artifact_paths(output: dict[str, object], workspace_root: Path) -> list[tuple[str, Path]]:
    values: list[tuple[str, Path]] = []
    for artifact in output.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        raw = str(artifact.get("path") or "").strip()
        path = _workspace_path(raw, workspace_root)
        if path is None:
            continue
        pair = (raw, path)
        if raw and pair not in values:
            values.append(pair)
    return values


def _artifact_expected_content_by_path(output: dict[str, object], workspace_root: Path) -> dict[Path, str]:
    values: dict[Path, str] = {}
    for artifact in output.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        path = _workspace_path(artifact.get("path"), workspace_root)
        if path is None:
            continue
        expected = _artifact_expected_content(artifact)
        if expected:
            values[path] = expected
    return values


def _artifact_expected_content(artifact: dict[str, object]) -> str:
    for key in ("content_equals", "expected_content", "content_pattern", "expected_stdout", "expected_output"):
        value = str(artifact.get(key) or "").strip()
        if value:
            return value
    return ""


def _workspace_path(value: object, workspace_root: Path) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (workspace_root / candidate).resolve()
    if not path.exists() and not candidate.is_absolute():
        path = _find_workspace_suffix(candidate, workspace_root) or path
    try:
        path.relative_to(workspace_root)
    except ValueError:
        return None
    return path


def _artifact_pytest_items(context: TestItemPreparationContext) -> list[dict[str, Any]]:
    return artifact_pytest_items(context.artifact_paths, workspace_root=context.workspace_root)


def _find_workspace_suffix(relative_path: Path, workspace_root: Path) -> Path | None:
    parts = relative_path.parts
    if not parts:
        return None
    matches = [
        item.resolve()
        for item in workspace_root.rglob(parts[-1])
        if item.parts[-len(parts):] == parts
    ]
    return matches[0] if len(matches) == 1 else None


def _single_artifact_dir(artifact_dirs: dict[str, Path]) -> Path | None:
    unique = set(artifact_dirs.values())
    if len(unique) != 1:
        return None
    return next(iter(unique))


def _named_artifact_dir(test: dict[str, Any], artifact_dirs: dict[str, Path]) -> Path | None:
    haystack = " ".join(str(test.get(key) or "") for key in ("name", "command", "file_path"))
    for name, path in artifact_dirs.items():
        if name and name in haystack:
            return path
    return None


def _command_artifact_working_dir(
    test: dict[str, Any],
    artifact_paths: list[tuple[str, Path]],
    workspace_root: Path,
) -> Path | None:
    command = str(test.get("command") or "")
    for raw, path in artifact_paths:
        if not (raw and raw in command and ("/" in raw or "\\" in raw)):
            continue
        if Path(raw).is_absolute():
            return workspace_root
        part_count = len(Path(raw).parts)
        if part_count <= 1:
            return path.parent
        return path.parents[part_count - 1]
    return None


def _relative_or_absolute(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)) or "."
    except ValueError:
        return str(path)
