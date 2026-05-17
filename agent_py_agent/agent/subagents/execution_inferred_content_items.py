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


# LLM: inferred_content_check_items converts explicit content-line contracts into file-bound checks.
# 函数用途: 当任务明确给出 required_content_lines 且只有一个普通文件产物时，生成可执行 content_check。
def inferred_content_check_items(request: ContentCheckInferenceRequest) -> list[dict[str, Any]]:
    """Return inferred content_check tests for one clear plain-file artifact."""

    if not request.required_lines or _has_content_check(request.existing_tests):
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
        if path.name.startswith("test_") and path.suffix == ".py":
            continue
        if path.exists() and path.is_dir():
            continue
        if path.suffix.lower() in {".html", ".htm"}:
            continue
        if path not in candidates:
            candidates.append(path)
    return candidates[0] if len(candidates) == 1 else None


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
