# LLM: Prepare runner-declared tests before bounded parent acceptance execution.
# 模块用途: 根据 output.json 里的 artifacts 给测试项补充安全工作目录，不执行命令、不读取 artifact 正文。

from __future__ import annotations

"""Prepare test execution items for parent acceptance."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar


# LLM: TestItemPreparationRequest keeps test normalization inputs bundled for future schema fields.
# 类用途: 保存测试项、runner 输出和 workspace 根目录；调用方用它生成可执行但仍受限的测试项。
@dataclass(frozen=True)
class TestItemPreparationRequest:
    """Bundle inputs used to prepare runner-declared tests."""

    __test__: ClassVar[bool] = False

    tests: list[dict[str, Any]]
    output: dict[str, object]
    workspace_root: str | Path


# LLM: prepare_test_items adds workspace-local working_dir hints without trusting arbitrary command text.
# 函数用途: 给缺少 working_dir 的命令测试补安全工作目录；目录来自同一 output.json 的 artifact 路径。
def prepare_test_items(request: TestItemPreparationRequest) -> list[dict[str, Any]]:
    """Return test items with inferred workspace-local working directories when safe."""

    workspace_root = Path(request.workspace_root).resolve()
    artifact_dirs = _artifact_dirs_by_name(request.output, workspace_root)
    fallback_dir = _single_artifact_dir(artifact_dirs)
    return [
        _prepared_test_item(test, artifact_dirs, fallback_dir, workspace_root)
        for test in request.tests
    ]


# LLM: _prepared_test_item keeps cwd inference flat so the public helper stays easy to audit.
# 函数用途: 复制单条测试项，并在安全时补 working_dir；不修改传入字典。
def _prepared_test_item(
    test: dict[str, Any],
    artifact_dirs: dict[str, Path],
    fallback_dir: Path | None,
    workspace_root: Path,
) -> dict[str, Any]:
    item = dict(test)
    if not _needs_working_dir(item):
        return item
    inferred = _named_artifact_dir(item, artifact_dirs) or fallback_dir
    if inferred is None:
        return item
    item["working_dir"] = _relative_or_absolute(inferred, workspace_root)
    return item


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


# LLM: _workspace_path resolves artifact paths as literals and rejects paths outside the configured workspace.
# 函数用途: 把 artifact path 解析成 workspace 内绝对路径；越界或空路径返回 None。
def _workspace_path(value: object, workspace_root: Path) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (workspace_root / candidate).resolve()
    try:
        path.relative_to(workspace_root)
    except ValueError:
        return None
    return path


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


# LLM: _relative_or_absolute keeps reports portable when the inferred directory sits below workspace_root.
# 函数用途: 优先返回 workspace 相对路径；极端情况下返回绝对路径给执行器再次做边界校验。
def _relative_or_absolute(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)) or "."
    except ValueError:
        return str(path)
