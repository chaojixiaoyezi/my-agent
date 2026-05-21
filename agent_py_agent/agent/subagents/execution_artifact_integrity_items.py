# LLM: Artifact integrity test-item expansion stays separate from general test preparation.
# 模块用途: 给 artifact_integrity 验收项补结构化 file_path，不读取产物正文、不依赖自然语言说明。

from __future__ import annotations

from pathlib import Path
from typing import Any


# LLM: expand_artifact_integrity_items clones runner-declared checks over machine artifact refs.
# 函数用途: 当 runner 只声明 artifact_integrity 但没给路径时，按 output.artifacts 生成可执行验收项。
def expand_artifact_integrity_items(
    tests: list[dict[str, Any]],
    *,
    artifact_paths: list[tuple[str, Path]],
    workspace_root: Path,
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for item in tests:
        expanded.extend(_expanded_item(item, artifact_paths=artifact_paths, workspace_root=workspace_root))
    return expanded


# LLM: _expanded_item keeps the public loop shallow for code-size and review.
# 函数用途: 处理单条测试项；只有缺少目标路径的 artifact_integrity 才会按 artifacts 展开。
def _expanded_item(
    item: dict[str, Any],
    *,
    artifact_paths: list[tuple[str, Path]],
    workspace_root: Path,
) -> list[dict[str, Any]]:
    if not _needs_artifact_targets(item) or not artifact_paths:
        return [item]
    return [_item_for_target(item, path, artifact_paths=artifact_paths, workspace_root=workspace_root) for _raw, path in artifact_paths]


# LLM: _needs_artifact_targets uses only the closed validation_method field and path fields.
# 函数用途: 判断 artifact_integrity 是否缺 file_path/path；不读取 name/summary 这类人类文本。
def _needs_artifact_targets(item: dict[str, Any]) -> bool:
    method = str(item.get("validation_method") or "command").strip().lower()
    if method != "artifact_integrity":
        return False
    return not str(item.get("file_path") or item.get("path") or "").strip()


# LLM: _item_for_target binds a single artifact path while preserving the runner's other fields.
# 函数用途: 为某个产物生成验收项，多产物时只追加文件名辅助人读。
def _item_for_target(
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


# LLM: _relative_or_absolute keeps generated test refs portable across workspaces.
# 函数用途: 优先返回 workspace 相对路径；越界时保留绝对路径给执行器边界再校验。
def _relative_or_absolute(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)) or "."
    except ValueError:
        return str(path)
