# LLM: Inferred content checks are split from test-item orchestration to keep acceptance prep thin.
# 模块用途: 根据普通文件 artifact 和 required_content_lines 生成父级 content_check 测试项。

from __future__ import annotations

"""Infer bounded content_check tests for plain file artifacts."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar


# LLM: ContentCheckInferenceRequest bundles artifact refs and explicit required lines for one pass.
# 类用途: 保存生成普通文件 content_check 所需的上下文；调用方不用散传路径、测试和内容列表。
@dataclass(frozen=True)
class ContentCheckInferenceRequest:
    """Inputs for inferring content_check items."""

    __test__: ClassVar[bool] = False

    artifact_paths: list[tuple[str, Path]]
    workspace_root: Path
    existing_tests: list[dict[str, Any]]
    required_lines: list[str] = field(default_factory=list)
    required_files: dict[str, list[str]] = field(default_factory=dict)


# LLM: inferred_content_check_items converts explicit content-line contracts into file-bound checks.
# 函数用途: 当任务明确给出 required_content_lines 且只有一个普通文件产物时，生成可执行 content_check。
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


# LLM: _mapped_content_check_items honors explicit file-to-lines contracts for multi-file outputs.
# 函数用途: 多个 artifact 时按文件名或相对路径匹配目标文件，生成对应 content_check；不按顺序猜。
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


# LLM: _artifact_path_for_key resolves a file-scoped content contract without scanning arbitrary files.
# 函数用途: 用 artifact refs 精确匹配相对路径或唯一 basename；有歧义时返回 None。
def _artifact_path_for_key(file_key: str, artifact_paths: list[tuple[str, Path]], workspace_root: Path) -> Path | None:
    key = _normalized_key(file_key)
    matches = [(_artifact_match_kind(key, raw, path, workspace_root), path) for raw, path in artifact_paths]
    exact = _paths_for_match(matches, "exact")
    basename = _paths_for_match(matches, "basename")
    return _single_path(exact) or (_single_path(basename) if not exact else None)


# LLM: _paths_for_match filters precomputed match tuples without nesting path-resolution logic.
# 函数用途: 从 artifact 匹配结果里提取某类命中的路径，保持主解析函数扁平。
def _paths_for_match(matches: list[tuple[str, Path]], kind: str) -> list[Path]:
    return [path for match_kind, path in matches if match_kind == kind]


# LLM: _single_path returns a path only when matching was unambiguous.
# 函数用途: 唯一命中才返回路径；多命中或无命中都交给上层跳过，避免猜错文件。
def _single_path(paths: list[Path]) -> Path | None:
    return paths[0] if len(paths) == 1 else None


# LLM: _artifact_match_kind keeps path-key matching shallow and explicit for code-size guards.
# 函数用途: 判断内容合同的文件 key 是精确命中 artifact ref，还是只命中了唯一文件名。
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


# LLM: _has_content_check avoids duplicating runner-declared concrete content validation.
# 函数用途: 如果模型已经给出可执行 content_check，父级不再生成重复的逐行检查。
def _has_content_check(tests: list[dict[str, Any]]) -> bool:
    return any(
        str(item.get("validation_method") or "").strip().lower() == "content_check"
        and str(item.get("file_path") or "").strip()
        and _content_pattern_value(item)
        for item in tests
    )


# LLM: _content_pattern_value mirrors executor pattern aliases without importing executor internals.
# 函数用途: 判断 content_check 是否提供了可执行的字面匹配内容，支持旧字段和 v2 字段。
def _content_pattern_value(item: dict[str, Any]) -> str:
    for key in ("content_equals", "expected_content", "content_pattern"):
        value = str(item.get(key) or "")
        if value:
            return value
    return ""


# LLM: _single_content_artifact_path is intentionally conservative for non-web files.
# 函数用途: 只在恰好一个普通文件 artifact 时自动绑定 required_content_lines，多个文件时不猜目标。
def _single_content_artifact_path(artifact_paths: list[tuple[str, Path]]) -> Path | None:
    candidates: list[Path] = []
    for _raw, path in artifact_paths:
        if not _plain_file_artifact(path):
            continue
        if path not in candidates:
            candidates.append(path)
    return candidates[0] if len(candidates) == 1 else None


# LLM: _plain_file_artifact keeps content checks away from pytest files, directories and static HTML pages.
# 函数用途: 判断 artifact 是否适合逐行文本 content_check；HTML 和 test_*.py 走各自专门验收。
def _plain_file_artifact(path: Path) -> bool:
    if path.name.startswith("test_") and path.suffix == ".py":
        return False
    if path.exists() and path.is_dir():
        return False
    return path.suffix.lower() not in {".html", ".htm"}


# LLM: _normalized_key compares artifact refs as portable relative path strings.
# 函数用途: 规范用户写的 file key、artifact raw path 和 workspace 相对路径，供精确匹配。
def _normalized_key(value: object) -> str:
    return str(value or "").strip().strip("'\"").replace("\\", "/").lstrip("./")


# LLM: _relative_or_absolute keeps reports portable when the inferred file sits below workspace_root.
# 函数用途: 优先返回 workspace 相对路径；极端情况下返回绝对路径给执行器再次做边界校验。
def _relative_or_absolute(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)) or "."
    except ValueError:
        return str(path)


# LLM: _dedupe_texts keeps generated content checks stable and compact.
# 函数用途: 去重 required_content_lines；空字符串不会生成无意义 content_check。
def _dedupe_texts(values: list[str]) -> list[str]:
    items: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in items:
            items.append(text)
    return items
