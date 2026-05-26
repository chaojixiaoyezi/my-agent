# LLM: Prepare runner-declared tests before bounded parent acceptance execution.
# 模块用途: 根据 output.json 里的 artifacts 给测试项补充安全工作目录，不执行命令、不读取 artifact 正文。

from __future__ import annotations

"""Prepare test execution items for parent acceptance."""

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .execution_artifact_integrity_items import expand_artifact_integrity_items
from .execution_content_checks import CatContentCheckRequest, normalize_cat_content_check
from .execution_file_exists_items import expand_file_exists_items
from .execution_inferred_content_items import (
    ContentCheckInferenceRequest,
    inferred_content_check_items,
)
from .execution_pytest_items import artifact_pytest_items
from .execution_static_site_items import StaticSiteTestItemsRequest, inferred_static_site_items
from .execution_test_checklists import drop_non_executable_model_checklist_items


# LLM: TestItemPreparationRequest keeps test normalization inputs bundled for future schema fields.
# 类用途: 保存测试项、runner 输出和 workspace 根目录；调用方用它生成可执行但仍受限的测试项。
@dataclass(frozen=True)
class TestItemPreparationRequest:
    """Bundle inputs used to prepare runner-declared tests."""

    __test__: ClassVar[bool] = False

    tests: list[dict[str, Any]]
    output: dict[str, object]
    workspace_root: str | Path
    required_files: list[str] = field(default_factory=list)
    # LLM: required_dom_ids lets parent acceptance pass machine-readable business sections into static_site_check.
    required_dom_ids: list[str] = field(default_factory=list)
    # LLM: required_content_lines lets parent acceptance validate plain artifacts without trusting model self-reports.
    required_content_lines: list[str] = field(default_factory=list)
    # LLM: required_content_files maps exact file refs to required lines for multi-artifact outputs.
    required_content_files: dict[str, list[str]] = field(default_factory=dict)
    site_root_hints: list[object] = field(default_factory=list)


# LLM: TestItemPreparationContext bundles derived artifact indexes for one preparation pass.
# 类用途: 保存 artifact 索引和 workspace 根目录，避免内部 helper 继续扩散多参数接口。
@dataclass(frozen=True)
class TestItemPreparationContext:
    """Derived metadata used while preparing one test item."""

    __test__: ClassVar[bool] = False

    artifact_dirs: dict[str, Path]
    artifact_paths: list[tuple[str, Path]]
    artifact_expected_content: dict[Path, str]
    fallback_dir: Path | None
    workspace_root: Path


# LLM: prepare_test_items adds workspace-local working_dir hints without trusting arbitrary command text.
# 函数用途: 给缺少 working_dir 的命令测试补安全工作目录；目录来自同一 output.json 的 artifact 路径。
def prepare_test_items(request: TestItemPreparationRequest) -> list[dict[str, Any]]:
    """Return test items with inferred workspace-local working directories when safe."""

    workspace_root = Path(request.workspace_root).resolve()
    artifact_dirs = _artifact_dirs_by_name(request.output, workspace_root)
    context = TestItemPreparationContext(
        artifact_dirs=artifact_dirs,
        artifact_paths=_artifact_paths(request.output, workspace_root),
        artifact_expected_content=_artifact_expected_content_by_path(request.output, workspace_root),
        fallback_dir=_single_artifact_dir(artifact_dirs),
        workspace_root=workspace_root,
    )
    prepared = _artifact_pytest_items(context) if not request.tests else [
        _prepared_test_item(test, context) for test in request.tests
    ]
    # LLM: Artifact integrity checks are inferred from artifact refs, not runner prose.
    # 函数用途: 在父级验收前补齐通用产物完整性检查，避免空测试清单直接假绿。
    prepared = expand_artifact_integrity_items(
        prepared,
        artifact_paths=context.artifact_paths,
        workspace_root=workspace_root,
    )
    # LLM: file_exists is a generic existence alias; bind missing targets from machine artifact refs.
    # 函数用途: 把模型/系统常写的 file_exists 转成可执行 file_check，不从自然语言 summary 猜路径。
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
    # LLM: Static-site inference appends refs-only checks after runner tests are normalized.
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


# LLM: _prepared_test_item keeps cwd inference flat so the public helper stays easy to audit.
# 函数用途: 复制单条测试项，并在安全时补 working_dir；不修改传入字典。
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
    inferred = _named_artifact_dir(item, context.artifact_dirs) or context.fallback_dir
    if inferred is None:
        return item
    item["working_dir"] = _relative_or_absolute(inferred, context.workspace_root)
    return item


# LLM: _normalize_validation_method repairs common runner aliases before bounded execution.
# 函数用途: 把 pytest/unittest 这类模型常写的验证方式转成 command，交给 TestExecutor 的 allowlist 校验。
def _normalize_validation_method(item: dict[str, Any]) -> None:
    method = str(item.get("validation_method") or "command").strip().lower()
    if method == "command" and _static_site_command_alias(item.get("command")):
        item["validation_method"] = "static_site_check"
        item.pop("command", None)
        return
    if method in {"pytest", "unittest"} and str(item.get("command") or "").strip():
        item["validation_method"] = "command"


# LLM: _static_site_command_alias treats common model pseudo-commands as native checks.
# 函数用途: 模型把内置 static_site_check 写进 command 字段时，转成 validation_method，避免被 shell allowlist 误拦。
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


# LLM: _normalize_leading_cd_command removes a safe shell cwd wrapper without allowing shell execution.
# 函数用途: 把 `cd <workspace内目录> && python...` 转成 working_dir 加纯命令，避免为了常见模型输出放开 shell。
def _normalize_leading_cd_command(item: dict[str, Any], workspace_root: Path) -> None:
    parsed = _safe_leading_cd_command(str(item.get("command") or ""), workspace_root)
    if parsed is None:
        return
    working_dir, command = parsed
    item["command"] = command
    if not str(item.get("working_dir") or item.get("cwd") or "").strip():
        item["working_dir"] = _relative_or_absolute(working_dir, workspace_root)


# LLM: _safe_leading_cd_command accepts only one leading cd chain and keeps the actual command for executor validation.
# 函数用途: 解析常见 `cd 目录 && 命令` 模式；目录必须在 workspace 内，其余命令仍由执行器安全校验。
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


# LLM: _workspace_dir resolves a cwd literal and rejects missing or out-of-workspace directories.
# 函数用途: 校验 cd 目标目录是否真实存在且位于 workspace 内；失败时让执行器继续按原命令阻断。
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


# LLM: _needs_working_dir limits inference to command tests that have not already chosen a cwd.
# 函数用途: 判断是否需要自动补目录；显式 cwd/working_dir 永远优先。
def _needs_working_dir(test: dict[str, Any]) -> bool:
    method = str(test.get("validation_method") or "command").strip() or "command"
    return method == "command" and not str(test.get("working_dir") or test.get("cwd") or "").strip()


# LLM: _artifact_dirs_by_name indexes artifact file parents by basename and ignores paths outside workspace.
# 函数用途: 从 output.json artifacts 提取安全目录；只使用路径元数据，不打开 artifact 文件。
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


# LLM: _artifact_paths keeps workspace-safe artifact path pairs for command/cwd inference.
# 函数用途: 收集 output.json 中安全 artifact 的原始路径和解析路径，供命令匹配但不读取文件正文。
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


# LLM: _artifact_expected_content_by_path indexes exact artifact assertions only.
# 函数用途: 从 output.artifacts 的结构化期望字段读取 cat->content_check 的内容，不解析 summary 文案。
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


# LLM: _artifact_expected_content reads closed schema keys, not human summaries.
# 函数用途: 兼容 artifact 级 content_equals/expected_content 等字段作为机器验收合同。
def _artifact_expected_content(artifact: dict[str, object]) -> str:
    for key in ("content_equals", "expected_content", "content_pattern", "expected_stdout", "expected_output"):
        value = str(artifact.get(key) or "").strip()
        if value:
            return value
    return ""


# LLM: _workspace_path resolves artifact paths as literals and rejects paths outside the configured workspace.
# 函数用途: 把 artifact path 解析成 workspace 内绝对路径；越界或空路径返回 None。
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


# LLM: _artifact_pytest_items gives parent acceptance a bounded fallback when runners omit tests.
# 函数用途: 从 workspace 内 test_*.py artifact 生成 pytest 命令；只用路径元数据，不执行或读取文件正文。
def _artifact_pytest_items(context: TestItemPreparationContext) -> list[dict[str, Any]]:
    return artifact_pytest_items(context.artifact_paths, workspace_root=context.workspace_root)


# LLM: _find_workspace_suffix recovers model-reported relative artifact paths from nested run directories.
# 函数用途: 当 artifact 相对路径少了任务目录前缀时，在 workspace 内按路径后缀找唯一真实文件。
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


# LLM: _single_artifact_dir is a conservative fallback for single-directory outputs.
# 函数用途: 只有所有 artifact 都在同一个目录时才返回兜底目录，避免多目录任务误跑。
def _single_artifact_dir(artifact_dirs: dict[str, Path]) -> Path | None:
    unique = set(artifact_dirs.values())
    if len(unique) != 1:
        return None
    return next(iter(unique))


# LLM: _named_artifact_dir lets test names like test_string_tools.py choose their own artifact directory.
# 函数用途: 测试 name 或 command 中出现 artifact 文件名时，返回对应 artifact 所在目录。
def _named_artifact_dir(test: dict[str, Any], artifact_dirs: dict[str, Path]) -> Path | None:
    haystack = " ".join(str(test.get(key) or "") for key in ("name", "command", "file_path"))
    for name, path in artifact_dirs.items():
        if name and name in haystack:
            return path
    return None


# LLM: _command_artifact_working_dir chooses a cwd that makes runner-declared artifact paths valid.
# 函数用途: 如果命令已经写了 artifact 路径，按真实文件后缀推断执行目录，避免路径重复或少前缀。
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


# LLM: _relative_or_absolute keeps reports portable when the inferred directory sits below workspace_root.
# 函数用途: 优先返回 workspace 相对路径；极端情况下返回绝对路径给执行器再次做边界校验。
def _relative_or_absolute(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)) or "."
    except ValueError:
        return str(path)
