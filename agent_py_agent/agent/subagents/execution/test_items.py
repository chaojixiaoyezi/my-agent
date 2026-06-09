
from __future__ import annotations

"""Prepare test execution items for closeout."""

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .inferred_content_items import (
    ContentCheckInferenceRequest,
    inferred_content_check_items,
)
from .static_site_items import StaticSiteTestItemsRequest, inferred_static_site_items


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


@dataclass(frozen=True)
class CatContentCheckRequest:
    """Bundle inputs used to normalize one cat-style content assertion."""

    __test__: ClassVar[bool] = False

    item: dict[str, Any]
    workspace_root: Path
    artifact_expected_content: dict[Path, str]


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
    prepared = expand_file_check_items(
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
    if method in {"pytest", "unittest"} and str(item.get("command") or "").strip():
        item["validation_method"] = "command"


def _normalize_leading_cd_command(item: dict[str, Any], workspace_root: Path) -> None:
    parsed = _safe_leading_cd_command(str(item.get("command") or ""), workspace_root)
    if parsed is None:
        return
    working_dir, command = parsed
    item["command"] = command
    if not str(item.get("working_dir") or item.get("cwd") or "").strip():
        item["working_dir"] = _relative_or_absolute(working_dir, workspace_root)


def normalize_cat_content_check(request: CatContentCheckRequest) -> dict[str, Any]:
    """Return a normalized item when a cat command can be represented as content_check."""

    item = dict(request.item)
    method = str(item.get("validation_method") or "command").strip().lower() or "command"
    if method != "command":
        return item
    path = _cat_command_path(str(item.get("command") or ""), request.workspace_root)
    if path is None:
        return item
    expected = _expected_content_for_cat_item(item, path, request.artifact_expected_content)
    if not expected:
        return item
    item["validation_method"] = "content_check"
    item["file_path"] = _relative_or_absolute(path, request.workspace_root)
    item["content_equals"] = expected
    item["match_mode"] = "exact"
    return item


def _cat_command_path(command: str, workspace_root: Path) -> Path | None:
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if len(argv) != 2 or Path(argv[0]).name.lower() != "cat":
        return None
    return _workspace_path(argv[1], workspace_root)


def _expected_content_for_cat_item(
    item: dict[str, Any],
    path: Path,
    artifact_expected_content: dict[Path, str],
) -> str:
    for key in ("content_equals", "expected_content", "content_pattern", "expected_stdout", "expected_output"):
        value = str(item.get(key) or "").strip()
        if _usable_expected_literal(value, path):
            return value
    expected = artifact_expected_content.get(path, "")
    return expected if _usable_expected_literal(expected, path) else ""


def _usable_expected_literal(value: str, path: Path) -> bool:
    value = value.strip()
    if not value or len(value) > 200:
        return False
    if "/" in value or "\\" in value or value == path.name:
        return False
    lowered = value.lower()
    if lowered.endswith((".txt", ".py", ".md", ".json", ".html", ".css", ".js")):
        return False
    return True


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
    items: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for _raw, path in context.artifact_paths:
        if path in seen or not _is_pytest_artifact(path):
            continue
        seen.add(path)
        items.append({
            "name": f"artifact pytest {path.name}",
            "validation_method": "command",
            "command": f"python3 -m pytest {path.name} -q",
            "working_dir": _relative_or_absolute(path.parent, context.workspace_root),
        })
    return items


def _is_pytest_artifact(path: Path) -> bool:
    return path.is_file() and path.suffix == ".py" and path.name.startswith("test_")


def expand_artifact_integrity_items(
    tests: list[dict[str, Any]],
    *,
    artifact_paths: list[tuple[str, Path]],
    workspace_root: Path,
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for item in tests:
        expanded.extend(_expanded_artifact_integrity_item(item, artifact_paths=artifact_paths, workspace_root=workspace_root))
    return expanded


def _expanded_artifact_integrity_item(
    item: dict[str, Any],
    *,
    artifact_paths: list[tuple[str, Path]],
    workspace_root: Path,
) -> list[dict[str, Any]]:
    if not _needs_artifact_integrity_targets(item) or not artifact_paths:
        return [item]
    return [
        _artifact_integrity_item_for_target(item, path, artifact_paths=artifact_paths, workspace_root=workspace_root)
        for _raw, path in artifact_paths
    ]


def _needs_artifact_integrity_targets(item: dict[str, Any]) -> bool:
    method = str(item.get("validation_method") or "command").strip().lower()
    if method != "artifact_integrity":
        return False
    return not str(item.get("file_path") or "").strip()


def _artifact_integrity_item_for_target(
    item: dict[str, Any],
    path: Path,
    *,
    artifact_paths: list[tuple[str, Path]],
    workspace_root: Path,
) -> dict[str, Any]:
    clone = dict(item)
    clone["file_path"] = _relative_or_absolute(path, workspace_root)
    if len(artifact_paths) > 1:
        clone["name"] = f"{str(item.get('name') or 'artifact integrity').strip()} {path.name}"
    return clone


def expand_file_check_items(
    tests: list[dict[str, Any]],
    *,
    artifact_paths: list[tuple[str, Path]],
    workspace_root: Path,
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for item in tests:
        expanded.extend(_expanded_file_check_item(item, artifact_paths=artifact_paths, workspace_root=workspace_root))
    return expanded


def _expanded_file_check_item(
    item: dict[str, Any],
    *,
    artifact_paths: list[tuple[str, Path]],
    workspace_root: Path,
) -> list[dict[str, Any]]:
    if not _is_file_check_item(item):
        return [item]
    if str(item.get("file_path") or "").strip():
        return [_explicit_file_check_item(item)]
    if not artifact_paths:
        clone = dict(item)
        clone["validation_method"] = "file_check"
        return [clone]
    return [
        _file_check_item_for_target(item, path, artifact_paths=artifact_paths, workspace_root=workspace_root)
        for _raw, path in artifact_paths
    ]


def _explicit_file_check_item(item: dict[str, Any]) -> dict[str, Any]:
    clone = dict(item)
    clone["validation_method"] = "file_check"
    return clone


def _is_file_check_item(item: dict[str, Any]) -> bool:
    method = str(item.get("validation_method") or "command").strip().lower().replace("-", "_")
    return method == "file_check"


def _file_check_item_for_target(
    item: dict[str, Any],
    path: Path,
    *,
    artifact_paths: list[tuple[str, Path]],
    workspace_root: Path,
) -> dict[str, Any]:
    clone = dict(item)
    clone["validation_method"] = "file_check"
    clone["file_path"] = _relative_or_absolute(path, workspace_root)
    if len(artifact_paths) > 1:
        clone["name"] = f"{str(item.get('name') or 'file exists').strip()} {path.name}"
    return clone


def drop_non_executable_model_checklist_items(tests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in tests if not _non_executable_model_checklist_item(item)]


def _non_executable_model_checklist_item(item: dict[str, Any]) -> bool:
    method = str(item.get("validation_method") or "command").strip().lower() or "command"
    if method == "command":
        return not str(item.get("command") or "").strip()
    if method == "file_check":
        return not str(item.get("file_path") or "").strip()
    if method == "content_check":
        return not str(item.get("file_path") or "").strip() or not _content_pattern_value(item)
    if method == "static_site_check":
        return not str(item.get("site_root") or "").strip()
    return False


def _content_pattern_value(item: dict[str, Any]) -> str:
    for key in ("content_equals", "expected_content", "content_pattern"):
        value = str(item.get(key) or "")
        if value:
            return value
    return ""


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
