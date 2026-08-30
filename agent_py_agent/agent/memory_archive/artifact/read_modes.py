
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...settings.defaults import default_config_int


@dataclass(frozen=True)
class ArtifactContentReadRequest:
    content: str
    mode: str = "slice"
    offset: int = 0
    max_chars: int = -1
    query: str = ""


# LLM: This result separates source coverage from presentation truncation; preserve these fields
# together whenever adding a read mode or changing pagination semantics.
# 类用途: 描述一次 artifact 正文读取的内容窗口、总长度和前后剩余范围。
@dataclass(frozen=True)
class ArtifactContentReadResult:
    ok: bool
    mode: str
    content: str = ""
    offset: int = 0
    max_chars: int = -1
    truncated: bool = False
    total_chars: int = 0
    has_more_before: bool = False
    has_more_after: bool = False
    metadata: dict[str, Any] | None = None
    error_code: str = ""
    message: str = ""


# LLM: Every read mode returns an explicit window position. In particular tail omits a prefix but
# has no continuation after its returned content; callers must not infer direction from truncated.
# 函数用途: 按 slice/head/tail/search 读取 artifact 正文，并给出窗口前后是否还有内容。
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
            total_chars=len(request.content),
            error_code="invalid_read_mode",
            message="mode must be one of slice/head/tail/search",
        )
    offset = max(0, int(request.offset or 0))
    return _slice_result(request.content, mode=mode, offset=offset, max_chars=max_chars)


def _normalize_read_mode(mode: str) -> str:
    value = str(mode or "slice").strip().lower()
    return value or "slice"


def _read_chars_limit(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = -1
    if parsed >= 0:
        return parsed
    return default_config_int("memory_artifact_default_read_chars", minimum=0)


# LLM: Slice continuation advances only toward the end; omitted prefix and remaining suffix are
# separate facts so model-facing pagination never guesses the direction.
# 函数用途: 读取指定字符窗口，并明确窗口前后是否仍有正文。
def _slice_result(content: str, *, mode: str, offset: int, max_chars: int) -> ArtifactContentReadResult:
    content_slice, truncated = _content_slice(content, offset=offset, max_chars=max_chars)
    end = min(len(content), offset + len(content_slice))
    return ArtifactContentReadResult(
        ok=True,
        mode=mode,
        content=content_slice,
        offset=offset,
        max_chars=max_chars,
        truncated=truncated,
        total_chars=len(content),
        has_more_before=offset > 0,
        has_more_after=end < len(content),
    )


# LLM: Tail is an end-anchored window and ignores offset. Omitting the prefix is not a request for
# another forward page, so truncated remains false while has_more_before records the omission.
# 函数用途: 读取正文最后一段，明确已经到达结尾，避免模型把尾部窗口误判为还缺后续。
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
        truncated=False,
        total_chars=len(content),
        has_more_before=offset > 0,
        has_more_after=False,
    )


# LLM: Search truncation refers only to the bounded match list, not byte/character pagination.
# 函数用途: 在完整正文中查找匹配行，并区分“匹配展示截断”和“正文窗口未读完”。
def _search_result(content: str, *, query: str, max_chars: int) -> ArtifactContentReadResult:
    needle = query.strip()
    if not needle:
        return ArtifactContentReadResult(
            ok=False,
            mode="search",
            total_chars=len(content),
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
        total_chars=len(content),
        has_more_before=False,
        has_more_after=False,
        metadata={"search_query": needle, "match_count": len(matches), "search_truncated": truncated},
    )


def _matching_lines(content: str, query: str) -> list[str]:
    needle = query.casefold()
    return [
        f"{line_number}: {line}"
        for line_number, line in enumerate(content.splitlines(), start=1)
        if needle in line.casefold()
    ]


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


def _content_slice(content: str, *, offset: int, max_chars: int) -> tuple[str, bool]:
    if offset >= len(content):
        return "", False
    if max_chars == 0:
        return content[offset:], False
    end = min(len(content), offset + max_chars)
    return content[offset:end], end < len(content)
