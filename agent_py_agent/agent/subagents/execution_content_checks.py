# LLM: Normalize model-written content assertions into bounded parent-acceptance content checks.
# 模块用途: 处理 runner 常写的 `cat file` 验收形式，把它改成安全的 content_check，不放开 shell 工具。

from __future__ import annotations

"""Helpers for turning simple file-content commands into content_check tests."""

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar


# LLM: CatContentCheckRequest keeps cat normalization inputs explicit and future-extensible.
# 类用途: 保存单条测试、workspace 根目录和 artifact 摘要，供归一化函数判断能否安全转换。
@dataclass(frozen=True)
class CatContentCheckRequest:
    """Bundle inputs used to normalize one cat-style content assertion."""

    __test__: ClassVar[bool] = False

    item: dict[str, Any]
    workspace_root: Path
    artifact_summaries: dict[Path, str]


# LLM: normalize_cat_content_check never executes cat; it rewrites narrow assertions into content_check.
# 函数用途: 当模型测试是 `cat <workspace文件>` 且期望内容明确时，返回 content_check 测试项副本。
def normalize_cat_content_check(request: CatContentCheckRequest) -> dict[str, Any]:
    """Return a normalized item when a cat command can be represented as content_check."""

    item = dict(request.item)
    method = str(item.get("validation_method") or "command").strip().lower() or "command"
    if method != "command":
        return item
    path = _cat_command_path(str(item.get("command") or ""), request.workspace_root)
    if path is None:
        return item
    expected = _expected_content_for_cat_item(item, path, request.artifact_summaries)
    if not expected:
        return item
    item["validation_method"] = "content_check"
    item["file_path"] = _relative_or_absolute(path, request.workspace_root)
    item["content_equals"] = expected
    item["match_mode"] = "exact"
    return item


# LLM: _cat_command_path accepts only a literal two-argument cat against a workspace-local file.
# 函数用途: 解析 `cat path` 命令；路径为空、越界或命令复杂时返回 None，让安全执行器继续拦截。
def _cat_command_path(command: str, workspace_root: Path) -> Path | None:
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if len(argv) != 2 or Path(argv[0]).name.lower() != "cat":
        return None
    return _workspace_path(argv[1], workspace_root)


# LLM: _workspace_path resolves content-check targets as literals and rejects workspace escapes.
# 函数用途: 把文件路径解析到 workspace 内；相对路径缺前缀时只接受唯一后缀匹配。
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


# LLM: _expected_content_for_cat_item prefers schema fields before conservative prose extraction.
# 函数用途: 为 cat->content_check 提取明确期望值；没有清晰字面值时不猜，继续走命令安全闸门。
def _expected_content_for_cat_item(
    item: dict[str, Any],
    path: Path,
    artifact_summaries: dict[Path, str],
) -> str:
    for key in ("content_equals", "expected_content", "content_pattern", "expected_stdout", "expected_output"):
        value = str(item.get(key) or "").strip()
        if _usable_expected_literal(value, path):
            return value
    texts = [
        str(item.get("summary") or ""),
        str(item.get("name") or ""),
        artifact_summaries.get(path, ""),
    ]
    for text in texts:
        value = _expected_literal_from_text(text, path)
        if value:
            return value
    return ""


# LLM: _expected_literal_from_text is intentionally narrow so prose cannot become fake exact content.
# 函数用途: 从“内容应为 X / must be X / `X`”这类短提示中提取字面期望值。
def _expected_literal_from_text(text: str, path: Path) -> str:
    for quoted in re.findall(r"`([^`\n]{1,200})`", text):
        value = quoted.strip()
        if _usable_expected_literal(value, path):
            return value
    patterns = [
        r"(?:内容|content)[^。\n;；]{0,60}?(?:是否为|应为|必须为|必须是|为|是|equals?|must be|should be)\s*[`\"']?([^`\"'，。；;\s]+)",
        r"(?:expected(?: content)?|must be|should be|equals?)\s*[:：]?\s*[`\"']?([^`\"'，。；;\s]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match and _usable_expected_literal(match.group(1), path):
            return match.group(1).strip()
    return ""


# LLM: _usable_expected_literal filters paths and filenames out of exact content guesses.
# 函数用途: 判断提取出的短文本是否像文件内容，而不是路径、文件名或整段说明。
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


# LLM: _find_workspace_suffix recovers model-reported relative paths without broad glob imports.
# 函数用途: 当相对路径少了前缀时，在 workspace 内按路径后缀找唯一真实文件。
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


# LLM: _relative_or_absolute keeps validation payloads portable for workspace-local paths.
# 函数用途: 优先返回 workspace 相对路径；不在 workspace 内时返回绝对路径给执行器再次校验。
def _relative_or_absolute(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)) or "."
    except ValueError:
        return str(path)
