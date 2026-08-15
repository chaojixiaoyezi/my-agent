
from __future__ import annotations

"""Prepare test execution items for closeout.

审计 R0:模型验收 command/cwd/working_dir 的 loader 路径已删除——不再从
command 字段合成 pytest 项、不再归一化 cd 前缀、不再推断 working_dir、
不再把 cat 命令转成 content_check。模型声明的 command 只保留为 inert
evidence,不会被加载为可执行项;可机验项只有 file_check / content_check /
static_site_check / artifact_integrity(进程内文件检查)。
"""

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

    artifact_paths: list[tuple[str, Path]]
    artifact_expected_content: dict[Path, str]
    workspace_root: Path


def prepare_test_items(request: TestItemPreparationRequest) -> list[dict[str, Any]]:
    """Return prepared test items: file/content/static-site/integrity checks only."""

    workspace_root = Path(request.workspace_root).resolve()
    context = TestItemPreparationContext(
        artifact_paths=_artifact_paths(request.output, workspace_root),
        artifact_expected_content=_artifact_expected_content_by_path(request.output, workspace_root),
        workspace_root=workspace_root,
    )
    prepared = [dict(test) for test in (request.tests or [])]
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
    method = str(item.get("validation_method") or "").strip().lower()
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
    method = str(item.get("validation_method") or "").strip().lower().replace("-", "_")
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
    method = str(item.get("validation_method") or "").strip().lower() or "command"
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


def _relative_or_absolute(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)) or "."
    except ValueError:
        return str(path)
