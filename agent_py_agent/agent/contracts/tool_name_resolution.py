# LLM: Tool name resolution repairs only exact structural drift and suggests fuzzy fixes.
# 模块用途: 参考 通道运行时/长期助手/终端交互，把工具名归一和未知工具建议做成纯结构化函数。

from __future__ import annotations

from collections.abc import Iterable
from difflib import SequenceMatcher

_SUGGESTION_THRESHOLD = 0.74


# LLM: resolve_dispatch_tool_name only returns names safe to execute.
# 函数用途: 对 exact/case/namespace/suffix 这类确定性漂移做归一，模糊匹配不执行。
def resolve_dispatch_tool_name(raw_name: object, available_tools: Iterable[str] | None) -> str | None:
    """Return an executable registered tool name only when the match is deterministic."""
    raw = _text(raw_name)
    available = _available_names(available_tools)
    if not raw or not available:
        return None
    for candidate in _structured_candidates(raw):
        if candidate in available:
            return candidate
        folded = _case_insensitive_match(candidate, available)
        if folded:
            return folded
    return None


# LLM: suggested_tool_name is diagnostic-only for unknown tool calls.
# 函数用途: 给未知工具 finding 附近似建议，调用方不得把建议直接当执行工具名。
def suggested_tool_name(raw_name: object, available_tools: Iterable[str] | None) -> str:
    """Return a single close suggestion for diagnostics; callers must not execute it automatically."""
    raw = _normalized_key(_text(raw_name))
    available = _available_names(available_tools)
    if not raw or not available:
        return ""
    scored = sorted(
        (
            (SequenceMatcher(None, raw, _normalized_key(name)).ratio(), name)
            for name in available
        ),
        reverse=True,
    )
    if not scored or scored[0][0] < _SUGGESTION_THRESHOLD:
        return ""
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return ""
    return scored[0][1]


# LLM: _structured_candidates enumerates deterministic tool-name rewrites.
# 函数用途: 生成原名、命名空间后缀和 _tool/.tool 去尾候选。
def _structured_candidates(raw: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    # LLM: add records each deterministic candidate once while preserving order.
    # 函数用途: 去重后追加候选工具名，避免同一候选重复打分或执行。
    def add(value: str) -> None:
        item = value.strip()
        if item and item not in seen:
            seen.add(item)
            candidates.append(item)

    add(raw)
    normalized = raw.replace("/", ".").replace("::", ".")
    add(normalized)
    suffixless = _strip_tool_suffix(normalized)
    add(suffixless)
    segments = [segment for segment in normalized.split(".") if segment]
    for index in range(1, len(segments)):
        suffix = ".".join(segments[index:])
        add(suffix)
        add(_strip_tool_suffix(suffix))
    return candidates


# LLM: _available_names normalizes the registered tool-name set.
# 函数用途: 清理空值并返回可用于 exact/case 匹配的工具名集合。
def _available_names(values: Iterable[str] | None) -> set[str]:
    return {str(item).strip() for item in values or [] if str(item).strip()}


# LLM: _case_insensitive_match is deterministic only when exactly one tool matches.
# 函数用途: 支持 Echo -> echo 这类大小写漂移，多个候选时拒绝。
def _case_insensitive_match(value: str, available: set[str]) -> str:
    matches = [name for name in available if name.lower() == value.lower()]
    return matches[0] if len(matches) == 1 else ""


# LLM: _strip_tool_suffix handles common wrapper suffix drift.
# 函数用途: 去掉 _tool/.tool 后缀，和 通道运行时 的结构化归一思路保持一致。
def _strip_tool_suffix(value: str) -> str:
    lower = value.lower()
    if lower.endswith("_tool") and len(value) > 5:
        return value[:-5]
    if lower.endswith(".tool") and len(value) > 5:
        return value[:-5]
    return value


# LLM: _normalized_key prepares strings for diagnostic fuzzy scoring only.
# 函数用途: 把分隔符和大小写规整成比较 key，不参与自动执行。
def _normalized_key(value: str) -> str:
    return _strip_tool_suffix(value).lower().replace("-", "_").replace(".", "_").replace("/", "_")


# LLM: _text converts scalar tool names into safe strings.
# 函数用途: 统一处理 None/空值，避免 resolver 抛异常。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["resolve_dispatch_tool_name", "suggested_tool_name"]
