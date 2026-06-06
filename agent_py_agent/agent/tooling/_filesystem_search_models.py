
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._filesystem_helpers import (
    _MAX_SEARCH_QUERY_CHARS,
    _bool_param,
    _int_param,
    _optional_path,
    _text_param,
)
from ._filesystem_read import _COMMON_FILE_DISCOVERY_IGNORES


@dataclass(frozen=True)
class SearchRequest:
    query: str
    raw_path: str
    limit: int
    offset: int
    context: int
    file_glob: str
    literal: bool
    ignore_case: bool
    output_mode: str
    include_ignored: bool


@dataclass(frozen=True)
class SearchHit:
    rel: str
    line_number: int
    line: str
    context_lines: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class SearchMatcher:
    query: str
    literal: bool
    ignore_case: bool
    regex: re.Pattern[str] | None = None

    @classmethod
    def from_request(cls, request: SearchRequest) -> SearchMatcher:
        if request.literal:
            query = request.query.lower() if request.ignore_case else request.query
            return cls(query=query, literal=True, ignore_case=request.ignore_case)
        flags = re.IGNORECASE if request.ignore_case else 0
        try:
            return cls(
                query=request.query,
                literal=False,
                ignore_case=request.ignore_case,
                regex=re.compile(request.query, flags),
            )
        except re.error as exc:
            raise ValueError(f"正则表达式无效: {exc}") from exc

    def matches(self, line: str) -> bool:
        if self.literal:
            target = line.lower() if self.ignore_case else line
            return self.query in target
        return bool(self.regex and self.regex.search(line))


@dataclass(frozen=True)
class SearchMatch:
    rel: str
    line_number: int
    line: str
    context_lines: tuple[tuple[int, str], ...]
    context: int


def search_request_from_params(params: dict[str, Any], max_matches: int) -> SearchRequest:
    if isinstance(params.get("filesystem"), dict):
        raise ValueError("filesystem bundle is not current search_text syntax; use top-level path instead.")
    query = _text_param(
        params.get("query"),
        name="query",
        max_chars=_MAX_SEARCH_QUERY_CHARS,
        strip=True,
    )
    output_mode = _search_output_mode(params)
    return SearchRequest(
        query=query,
        raw_path=_optional_path(params.get("path", "."), default="."),
        limit=min(
            _int_param(params.get("limit"), name="limit", default=max_matches, min_value=1),
            max_matches,
        ),
        offset=_int_param(params.get("offset"), name="offset", default=0, min_value=0),
        context=_int_param(params.get("context"), name="context", default=0, min_value=0),
        file_glob=_text_param(
            params.get("file_glob", ""),
            name="file_glob",
            max_chars=200,
            allow_empty=True,
            strip=True,
        ),
        literal=_bool_param(params.get("literal"), default=True),
        ignore_case=_bool_param(params.get("ignore_case"), default=False),
        output_mode=output_mode,
        include_ignored=_bool_param(params.get("include_ignored"), default=False),
    )


def path_has_ignored_part(item: Path, root: Path) -> bool:
    try:
        rel = item.relative_to(root)
    except ValueError:
        return False
    return any(part in _COMMON_FILE_DISCOVERY_IGNORES for part in rel.parts[:-1])


def rg_args(rg_path: str, target: Path, request: SearchRequest) -> list[str]:
    args = [
        rg_path,
        "--json",
        "--line-number",
        "--color=never",
        "--hidden",
        "--no-config",
        "--no-messages",
        "--no-follow",
    ]
    if request.literal:
        args.append("--fixed-strings")
    if request.ignore_case:
        args.append("--ignore-case")
    if request.file_glob:
        args.extend(["--glob", request.file_glob])
    if not request.include_ignored:
        for ignored in sorted(_COMMON_FILE_DISCOVERY_IGNORES):
            args.extend(["--glob", f"!{ignored}/**", "--glob", f"!**/{ignored}/**"])
    args.extend(["--", request.query, str(target)])
    return args


def rg_text(value: Any) -> str:
    if isinstance(value, dict):
        text = value.get("text")
        return text if isinstance(text, str) else ""
    return ""


def slice_hits(hits: list[SearchHit], request: SearchRequest) -> list[SearchHit]:
    return hits[request.offset : request.offset + request.limit]


def render_files_with_matches(hits: list[SearchHit], request: SearchRequest) -> str:
    paths = list(dict.fromkeys(hit.rel for hit in hits))
    page = paths[request.offset : request.offset + request.limit]
    if len(paths) > request.offset + request.limit:
        page.append(search_page_notice(request.offset + request.limit, request.limit))
    return "\n".join(page) or "没有找到匹配项"


def render_match_counts(counts: dict[str, int], request: SearchRequest) -> str:
    rows = [f"{path}: {count}" for path, count in counts.items()]
    page = rows[request.offset : request.offset + request.limit]
    if len(rows) > request.offset + request.limit:
        page.append(search_page_notice(request.offset + request.limit, request.limit))
    return "\n".join(page) or "没有找到匹配项"


def render_line_numbers(hits: list[SearchHit], request: SearchRequest) -> str:
    rows = [f"{hit.rel}:{hit.line_number}" for hit in hits]
    page = rows[request.offset : request.offset + request.limit]
    if len(rows) > request.offset + request.limit:
        page.append(search_page_notice(request.offset + request.limit, request.limit))
    return "\n".join(page) or "没有找到匹配项"


def search_page_notice(seen: int, limit: int) -> str:
    return f"... 已截断，next_offset={seen} limit={limit}；继续搜索请再次调用 search_text 并传入 offset={seen}"


def _search_output_mode(params: dict[str, Any]) -> str:
    output_mode = _text_param(
        params.get("output_mode", "content"),
        name="output_mode",
        max_chars=40,
        strip=True,
    ).lower()
    if output_mode not in {"content", "files_with_matches", "count", "line_numbers"}:
        raise ValueError("output_mode 只能是 content、files_with_matches、count 或 line_numbers")
    return output_mode
