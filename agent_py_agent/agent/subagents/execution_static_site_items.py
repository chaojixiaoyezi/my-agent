# LLM: Static web artifact inference adds deterministic parent checks when runners omit them.
# 模块用途: 根据 output.json artifacts 自动生成 static_site_check 验收项，不读取 HTML 正文。

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar


# LLM: StaticSiteTestItemsRequest bundles artifact refs and existing tests for one inference pass.
# 类用途: 保存静态站点测试推断所需的上下文，避免 public helper 后续扩散参数。
@dataclass(frozen=True)
class StaticSiteTestItemsRequest:
    """Bundle inputs for static-site test inference."""

    __test__: ClassVar[bool] = False

    artifact_paths: list[tuple[str, Path]]
    workspace_root: Path
    existing_tests: list[dict[str, Any]]
    required_files: list[str] = field(default_factory=list)


# LLM: inferred_static_site_items creates one refs-only static_site_check for HTML outputs.
# 函数用途: 当 artifact 显示有静态 Web 产物且尚无同类测试时，自动补机器验收项；单页也检查本地资源。
def inferred_static_site_items(request: StaticSiteTestItemsRequest) -> list[dict[str, Any]]:
    if _has_static_site_check(request.existing_tests):
        return []
    html_paths = _html_artifact_paths(request.artifact_paths)
    if not html_paths:
        return []
    site_root = _common_parent(html_paths)
    required_files = _merged_required_files(
        _required_files(html_paths, site_root),
        _normalized_required_files(request.required_files),
    )
    return [{
        "name": "inferred static site check",
        "validation_method": "static_site_check",
        "site_root": _relative_or_absolute(site_root, request.workspace_root),
        "required_files": required_files,
    }]


# LLM: _has_static_site_check keeps runner-declared checks from being duplicated.
# 函数用途: 如果模型已经显式提供 static_site_check，自动推断不再追加第二条。
def _has_static_site_check(tests: list[dict[str, Any]]) -> bool:
    return any(str(item.get("validation_method") or "").strip().lower() == "static_site_check" for item in tests)


# LLM: _html_artifact_paths selects concrete HTML artifact refs without touching file contents.
# 函数用途: 从 artifact path 索引里提取 html 文件路径；路径是否存在由执行器最终判断。
def _html_artifact_paths(artifact_paths: list[tuple[str, Path]]) -> list[Path]:
    seen: set[Path] = set()
    paths: list[Path] = []
    for _raw, path in artifact_paths:
        if path in seen or path.suffix.lower() not in {".html", ".htm"}:
            continue
        seen.add(path)
        paths.append(path)
    return paths


# LLM: _common_parent finds the smallest shared static-site root for generated pages.
# 函数用途: 计算多页面产物的共同目录，作为 static_site_check 的 site_root。
def _common_parent(paths: list[Path]) -> Path:
    parents = [path.parent for path in paths]
    common = Path(*Path(*parents[0].parts).parts)
    for parent in parents[1:]:
        common = _shared_prefix(common, parent)
    return common


# LLM: _shared_prefix keeps common-parent calculation independent from filesystem existence.
# 函数用途: 对两个路径按字面 parts 计算公共前缀，支持尚未存在的目标文件。
def _shared_prefix(left: Path, right: Path) -> Path:
    parts = []
    for left_part, right_part in zip(left.parts, right.parts):
        if left_part != right_part:
            break
        parts.append(left_part)
    return Path(*parts) if parts else Path(".")


# LLM: _required_files renders required HTML files relative to the inferred site root.
# 函数用途: 为 static_site_check 生成必需文件列表，保证缺页面会失败。
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


# LLM: _normalized_required_files accepts task-level static deliverable expectations without reading files.
# 函数用途: 规整从任务目标/验收条件抽取的必需文件名；保留相对路径，拒绝绝对路径和父目录跳转。
def _normalized_required_files(values: list[str]) -> list[str]:
    files: list[str] = []
    for value in values:
        raw = str(value or "").strip().replace("\\", "/").lstrip("./")
        if not raw or raw.startswith("/") or ".." in Path(raw).parts:
            continue
        if raw not in files:
            files.append(raw)
    return files


# LLM: _merged_required_files keeps artifact-observed pages plus task-declared top-level files in one check.
# 函数用途: 合并自动发现和任务要求的必需文件，顺序稳定且去重。
def _merged_required_files(observed: list[str], declared: list[str]) -> list[str]:
    files: list[str] = []
    for value in [*observed, *declared]:
        if value and value not in files:
            files.append(value)
    return sorted(files)


# LLM: _relative_or_absolute keeps generated test specs portable under workspace_root.
# 函数用途: 优先返回 workspace 相对 site_root；越界时保留绝对路径交给执行器阻断。
def _relative_or_absolute(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)) or "."
    except ValueError:
        return str(path)
