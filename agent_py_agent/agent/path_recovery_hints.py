# LLM: shared path recovery helpers keep runner/tool guidance consistent across orchestration and filesystem tools.
# 模块用途: 提供 URL 范围识别和工作区路径拼写修复提示，帮助模型从路径错误中恢复而不是申请多余权限。

from __future__ import annotations

import re
from pathlib import Path

_URL_PATTERN = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s\"'<>]*")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")


# LLM: url_spans marks network references before path regexes scan the same text.
# 函数用途: 返回文本里 URL 的字符范围，后续路径提取可以跳过这些范围。
def url_spans(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _URL_PATTERN.finditer(text)]


# LLM: overlaps_spans is a tiny predicate used by path scanners to ignore URL internals.
# 函数用途: 判断候选字符区间是否与已知 URL 范围重叠。
def overlaps_spans(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


# LLM: suggest_workspace_typo_target repairs only suffix-matching paths under known workspace roots.
# 函数用途: 根据工作区目录名后的相同尾部，推导用户名前缀拼错时应重试的目标路径。
def suggest_workspace_typo_target(raw: str, workspace_roots: Path | list[Path]) -> str:
    if _WINDOWS_ABSOLUTE_RE.match(raw):
        return ""
    candidate = Path(raw.replace("\\", "/")).expanduser()
    if not candidate.is_absolute():
        return ""
    for root in _workspace_roots(workspace_roots):
        suggested = _suggest_workspace_root_tail(root, candidate.parts)
        if suggested:
            return suggested
    return ""


# LLM: _suggest_workspace_root_tail keeps typo repair readable and avoids nested path heuristics.
# 函数用途: 从候选路径中找到工作区目录名后的尾部，并拼回真实工作区根。
def _suggest_workspace_root_tail(root: Path, candidate_parts: tuple[str, ...]) -> str:
    tail = _tail_after_part(candidate_parts, root.name)
    if not tail:
        return ""
    suggestion = root.joinpath(*tail).resolve(strict=False)
    return str(suggestion) if _is_relative_to(suggestion, root) else ""


# LLM: _tail_after_part extracts a suffix after a known workspace directory marker.
# 函数用途: 找到路径中指定目录名后面的相对尾部；找不到或没有尾部时返回空元组。
def _tail_after_part(parts: tuple[str, ...], marker: str) -> tuple[str, ...]:
    if not marker:
        return ()
    try:
        index = parts.index(marker)
    except ValueError:
        return ()
    return parts[index + 1 :]


# LLM: _workspace_roots normalizes one or many roots without requiring callers to pre-resolve.
# 函数用途: 将工作区根统一解析成绝对 Path 列表，供路径包含判断复用。
def _workspace_roots(workspace_roots: Path | list[Path]) -> list[Path]:
    raw_roots = workspace_roots if isinstance(workspace_roots, list) else [workspace_roots]
    return [Path(root).resolve(strict=False) for root in raw_roots]


# LLM: _is_relative_to supports Python versions and call sites that need explicit relative checks.
# 函数用途: 判断 path 是否在 root 之下；不抛异常，方便安全边界判断。
def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
