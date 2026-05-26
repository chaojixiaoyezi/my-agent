# LLM: search_text parameter and render helpers are shared by rg and Python backends.
# 模块用途: 保存 search_text 请求、命中、匹配器和分页渲染的小型工具函数。

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._filesystem_helpers import (
    _MAX_SEARCH_QUERY_CHARS,
    _bool_param,
    _bundled_filesystem_param,
    _int_param,
    _optional_path,
    _text_param,
)
from ._filesystem_read import _COMMON_FILE_DISCOVERY_IGNORES


# LLM: SearchRequest bundles search_text business parameters to keep methods small.
# 类用途: 保存 search_text 的查询、分页和过滤参数，避免散落多个函数参数。
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


# LLM: SearchHit stores one normalized search hit before output rendering.
# 类用途: 保存 search_text 的单条命中，供 content/files/count 三种输出模式复用。
@dataclass(frozen=True)
class SearchHit:
    rel: str
    line_number: int
    line: str
    lines: list[str]


# LLM: SearchMatcher hides literal/regex differences so traversal stays simple.
# 类用途: 保存 search_text 的匹配器，支持普通文本、正则和大小写开关。
@dataclass(frozen=True)
class SearchMatcher:
    query: str
    literal: bool
    ignore_case: bool
    regex: re.Pattern[str] | None = None

    # LLM: SearchMatcher.from_request builds the correct matcher for one request.
    # 函数用途: 根据 literal/ignore_case 编译普通文本或正则匹配器。
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

    # LLM: matches applies the chosen literal or regex matching policy.
    # 函数用途: 判断一行文本是否命中当前 search_text 查询。
    def matches(self, line: str) -> bool:
        if self.literal:
            target = line.lower() if self.ignore_case else line
            return self.query in target
        return bool(self.regex and self.regex.search(line))


# LLM: SearchMatch bundles render data for one hit and optional context lines.
# 类用途: 保存 search_text 单条命中及其上下文渲染参数。
@dataclass(frozen=True)
class SearchMatch:
    rel: str
    line_number: int
    line: str
    lines: list[str]
    context: int


# LLM: search_request_from_params validates JSON before filesystem work starts.
# 函数用途: 从工具参数中解析 search_text 的业务参数包。
def search_request_from_params(params: dict[str, Any], max_matches: int) -> SearchRequest:
    query = _text_param(
        _bundled_filesystem_param(params, "query", _bundled_filesystem_param(params, "pattern")),
        name="query",
        max_chars=_MAX_SEARCH_QUERY_CHARS,
        strip=True,
    )
    output_mode = _search_output_mode(params)
    return SearchRequest(
        query=query,
        raw_path=_optional_path(_bundled_filesystem_param(params, "path", "."), default="."),
        limit=min(
            _int_param(_bundled_filesystem_param(params, "limit"), name="limit", default=max_matches, min_value=1),
            max_matches,
        ),
        offset=_int_param(_bundled_filesystem_param(params, "offset"), name="offset", default=0, min_value=0),
        context=_int_param(_bundled_filesystem_param(params, "context"), name="context", default=0, min_value=0),
        file_glob=_text_param(
            _bundled_filesystem_param(params, "file_glob", ""),
            name="file_glob",
            max_chars=200,
            allow_empty=True,
            strip=True,
        ),
        literal=_bool_param(_bundled_filesystem_param(params, "literal"), default=True),
        ignore_case=_bool_param(_bundled_filesystem_param(params, "ignore_case"), default=False),
        output_mode=output_mode,
        include_ignored=_bool_param(_bundled_filesystem_param(params, "include_ignored"), default=False),
    )


# LLM: path_has_ignored_part prevents recursive search from entering noisy dirs.
# 函数用途: 判断候选路径是否位于默认忽略目录之内。
def path_has_ignored_part(item: Path, root: Path) -> bool:
    try:
        rel = item.relative_to(root)
    except ValueError:
        return False
    return any(part in _COMMON_FILE_DISCOVERY_IGNORES for part in rel.parts[:-1])


# LLM: rg_args builds a subprocess argv list, never a shell string.
# 函数用途: 根据 search_text 参数生成受控 rg 命令参数。
def rg_args(rg_path: str, target: Path, request: SearchRequest) -> list[str]:
    args = [rg_path, "--json", "--line-number", "--color=never", "--hidden", "--no-config", "--no-messages"]
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


# LLM: rg_text extracts ripgrep JSON text fields.
# 函数用途: 从 rg 的 {"text": "..."} 字段取字符串，坏结构返回空串。
def rg_text(value: Any) -> str:
    if isinstance(value, dict):
        text = value.get("text")
        return text if isinstance(text, str) else ""
    return ""


# LLM: slice_hits applies offset/limit after all hits are normalized.
# 函数用途: 对 search_text 命中做分页切片。
def slice_hits(hits: list[SearchHit], request: SearchRequest) -> list[SearchHit]:
    return hits[request.offset : request.offset + request.limit]


# LLM: render_files_with_matches mirrors grep -l style output.
# 函数用途: 渲染只包含命中文件路径的 search_text 输出。
def render_files_with_matches(hits: list[SearchHit], request: SearchRequest) -> str:
    paths = list(dict.fromkeys(hit.rel for hit in hits))
    page = paths[request.offset : request.offset + request.limit]
    if len(paths) > request.offset + request.limit:
        page.append(search_page_notice(request.offset + request.limit, request.limit))
    return "\n".join(page) or "没有找到匹配项"


# LLM: render_match_counts mirrors grep -c style output.
# 函数用途: 渲染每个文件的命中次数。
def render_match_counts(hits: list[SearchHit], request: SearchRequest) -> str:
    counts: dict[str, int] = {}
    for hit in hits:
        counts[hit.rel] = counts.get(hit.rel, 0) + 1
    rows = [f"{path}: {count}" for path, count in counts.items()]
    page = rows[request.offset : request.offset + request.limit]
    if len(rows) > request.offset + request.limit:
        page.append(search_page_notice(request.offset + request.limit, request.limit))
    return "\n".join(page) or "没有找到匹配项"


# LLM: search_page_notice gives models a copyable offset for the next page.
# 函数用途: 生成 search_text 分页继续查询提示。
def search_page_notice(seen: int, limit: int) -> str:
    return f"... 已截断，next_offset={seen} limit={limit}；继续搜索请再次调用 search_text 并传入 offset={seen}"


# LLM: _search_output_mode parses and validates the output mode string.
# 函数用途: 解析 content/files_with_matches/count 输出模式。
def _search_output_mode(params: dict[str, Any]) -> str:
    output_mode = _text_param(
        _bundled_filesystem_param(params, "output_mode", "content"),
        name="output_mode",
        max_chars=40,
        strip=True,
    ).lower()
    if output_mode not in {"content", "files_with_matches", "count"}:
        raise ValueError("output_mode 只能是 content、files_with_matches 或 count")
    return output_mode
