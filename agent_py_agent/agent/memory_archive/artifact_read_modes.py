# LLM: Artifact body mode helpers keep indexed artifact lookup separate from content shaping.
# 模块用途: 为 read_artifact 提供 slice/head/tail/search 四种正文窄读模式。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# LLM: ArtifactContentReadRequest bundles mode-specific read fields without importing artifact_reader.
# 类用途: 保存 artifact 正文窄读所需的 mode、offset、max_chars 和 query。
@dataclass(frozen=True)
class ArtifactContentReadRequest:
    content: str
    mode: str = "slice"
    offset: int = 0
    max_chars: int = -1
    query: str = ""


# LLM: ArtifactContentReadResult keeps mode-specific artifact body output out of the public reader shape.
# 类用途: 保存一次 artifact 正文读取的模式、偏移、内容、截断状态和附加元数据。
@dataclass(frozen=True)
class ArtifactContentReadResult:
    ok: bool
    mode: str
    content: str = ""
    offset: int = 0
    max_chars: int = -1
    truncated: bool = False
    metadata: dict[str, Any] | None = None
    error_code: str = ""
    message: str = ""


# LLM: read_artifact_content_by_mode turns read_artifact modes into bounded body strings.
# 函数用途: 根据 slice/head/tail/search 模式返回可控正文片段，避免模型为了找线索反复读完整 artifact。
def read_artifact_content_by_mode(request: ArtifactContentReadRequest) -> ArtifactContentReadResult:
    mode = _normalize_read_mode(request.mode)
    max_chars = _read_chars_limit(request.max_chars)
    if mode == "head":
        return _slice_result(request.content, mode=mode, offset=0, max_chars=max_chars)
    if mode == "tail":
        return _tail_result(request.content, max_chars=max_chars)
    if mode == "search":
        return _search_result(request.content, query=str(request.query or ""), max_chars=max_chars)
    if mode != "slice":
        return ArtifactContentReadResult(
            ok=False,
            mode=mode,
            error_code="invalid_read_mode",
            message="mode must be one of slice/head/tail/search",
        )
    offset = max(0, int(request.offset or 0))
    return _slice_result(request.content, mode=mode, offset=offset, max_chars=max_chars)


# LLM: _normalize_read_mode preserves old callers by treating blank mode as slice.
# 函数用途: 归一化 read_artifact mode 字段；未知值保留给上层返回 invalid_read_mode。
def _normalize_read_mode(mode: str) -> str:
    value = str(mode or "slice").strip().lower()
    return value or "slice"


# LLM: _read_chars_limit resolves artifact body length from AgentConfig when callers omit a value.
# 函数用途: 统一 read_artifact 默认读取长度，避免 artifact 读取链路散落第二份 4000 默认值。
def _read_chars_limit(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = -1
    if parsed >= 0:
        return parsed
    from ..settings.config import AgentConfig

    return max(0, int(AgentConfig().memory_artifact_default_read_chars))


# LLM: _slice_result is the common implementation for slice and head modes.
# 函数用途: 返回 offset/max_chars 控制下的正文片段，并标记是否还有后续内容。
def _slice_result(content: str, *, mode: str, offset: int, max_chars: int) -> ArtifactContentReadResult:
    content_slice, truncated = _content_slice(content, offset=offset, max_chars=max_chars)
    return ArtifactContentReadResult(
        ok=True,
        mode=mode,
        content=content_slice,
        offset=offset,
        max_chars=max_chars,
        truncated=truncated,
    )


# LLM: _tail_result gives agents a bounded way to inspect the latest lines of long outputs.
# 函数用途: 返回 artifact 正文尾部片段；max_chars=0 时兼容读取全部。
def _tail_result(content: str, *, max_chars: int) -> ArtifactContentReadResult:
    if max_chars == 0:
        return _slice_result(content, mode="tail", offset=0, max_chars=0)
    offset = max(0, len(content) - max_chars)
    return ArtifactContentReadResult(
        ok=True,
        mode="tail",
        content=content[offset:],
        offset=offset,
        max_chars=max_chars,
        truncated=offset > 0,
    )


# LLM: _search_result returns matching lines only, not the whole artifact body.
# 函数用途: 在 artifact 正文中按关键词查找，输出带行号的有限匹配片段和匹配计数。
def _search_result(content: str, *, query: str, max_chars: int) -> ArtifactContentReadResult:
    needle = query.strip()
    if not needle:
        return ArtifactContentReadResult(
            ok=False,
            mode="search",
            error_code="missing_search_query",
            message="query is required when mode=search",
        )
    matches = _matching_lines(content, needle)
    bounded, truncated = _bounded_join(matches, max_chars=max_chars)
    return ArtifactContentReadResult(
        ok=True,
        mode="search",
        content=bounded,
        offset=0,
        max_chars=max_chars,
        truncated=truncated,
        metadata={"search_query": needle, "match_count": len(matches), "search_truncated": truncated},
    )


# LLM: _matching_lines keeps search output line-oriented so agents can request a later slice if needed.
# 函数用途: 返回包含 query 的正文行，格式为 `line_number: line_text`。
def _matching_lines(content: str, query: str) -> list[str]:
    needle = query.casefold()
    return [
        f"{line_number}: {line}"
        for line_number, line in enumerate(content.splitlines(), start=1)
        if needle in line.casefold()
    ]


# LLM: _bounded_join caps search output while keeping whole match rows where possible.
# 函数用途: 把匹配行拼成不超过 max_chars 的字符串；max_chars=0 表示不截断。
def _bounded_join(items: list[str], *, max_chars: int) -> tuple[str, bool]:
    if max_chars == 0:
        return "\n".join(items), False
    lines: list[str] = []
    size = 0
    for item in items:
        next_size = size + len(item) + (1 if lines else 0)
        if next_size > max_chars:
            return "\n".join(lines), True
        lines.append(item)
        size = next_size
    return "\n".join(lines), False


# LLM: _content_slice applies explicit slicing so artifact reads can avoid re-flooding prompts.
# 函数用途: max_chars=0 读取全部，否则返回 offset 后最多 max_chars 个字符，并标记是否截断。
def _content_slice(content: str, *, offset: int, max_chars: int) -> tuple[str, bool]:
    if offset >= len(content):
        return "", False
    if max_chars == 0:
        return content[offset:], False
    end = min(len(content), offset + max_chars)
    return content[offset:end], end < len(content)
