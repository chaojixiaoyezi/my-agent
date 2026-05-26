# LLM: search_text lives in its own module so file IO tools can grow without hitting code-size limits.
# 模块用途: 实现工作区文本搜索工具，支持分页、文件 glob 过滤和少量上下文展示。

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._filesystem_helpers import (
    _MAX_SEARCH_LINE_CHARS,
    _MAX_SEARCH_QUERY_CHARS,
    _bool_param,
    _bundled_filesystem_param,
    _int_param,
    _optional_path,
    _read_text_safe,
    _text_param,
)
from ._filesystem_read import _COMMON_FILE_DISCOVERY_IGNORES, FileSystemTool
from .models import ToolExecutionResult, ToolSpec


# LLM: SearchTextTool 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: SearchTextTool 数据模型，集中保存 工具系统 的结构化状态。
class SearchTextTool(FileSystemTool):

    # LLM: SearchTextTool.__init__ 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 初始化 SearchTextTool 的依赖、配置和运行期字段。
    def __init__(self, workspace_root: Path, max_matches: int, workspace_roots: list[Path] | None = None):
        super().__init__(workspace_root, workspace_roots)
        self.max_matches = max_matches
        self.spec = ToolSpec(
            name="search_text",
            category="filesystem",
            effect="read_only",
            description="在工作区里搜索纯文本，适合找函数名、配置项和关键字。",
            use_cases=[
                "想找某个函数、类、配置项出现在哪些文件里",
                "先全局搜索，再决定读哪几个文件",
            ],
            avoid_when=[
                "已经知道具体文件并且要看上下文时，直接 read_file 更合适",
            ],
            keywords=["搜索", "查找", "关键字", "grep", "rg", "全文检索", "文本匹配"],
            parameters={
                "query": "要搜索的文本",
                "pattern": "query 的别名，便于兼容 grep 风格调用",
                "path": "从哪个目录开始搜，默认是工作区根目录",
                "limit": "本次最多返回多少条匹配，默认使用工具配置上限",
                "offset": "跳过前多少条匹配，用于分页，默认 0",
                "file_glob": "只搜索匹配 glob 的文件，例如 *.py",
                "context": "每条命中前后额外展示多少行上下文，默认 0",
                "literal": "是否按普通文本匹配，默认 true；false 时按正则匹配",
                "ignore_case": "是否忽略大小写，默认 false",
                "output_mode": "输出模式：content、files_with_matches 或 count，默认 content",
                "include_ignored": "是否搜索 .git/node_modules 等常见噪声目录，默认 false",
            },
            parameter_details={
                "query": "必填，默认按文本包含关系匹配；需要正则时传 literal=false。",
                "pattern": "可选，和 query 等价；同时传时 query 优先。",
                "path": "可选，把搜索范围缩小到某个子目录时更高效。",
                "limit": "分页大小；命中很多时先看小批量，再用 next_offset 继续。",
                "offset": "上一页返回 next_offset 后，下一次传入这里继续看。",
                "file_glob": "按文件名或工作区相对路径过滤，例如 *.py、docs/*.md。",
                "context": "需要看命中附近内容时传 1 或 2；越大越占 prompt。",
                "literal": "默认 true，避免把用户普通文字误当正则；传 false 才启用正则。",
                "ignore_case": "大小写不确定时传 true。",
                "output_mode": "content 返回行内容；files_with_matches 只返回文件；count 返回每个文件命中数。",
                "include_ignored": "默认跳过 .git、node_modules 和常见缓存目录；确实要查时传 true。",
            },
            examples=[
                '{"tool": "search_text", "query": "PromptBuilder"}',
                '{"tool": "search_text", "query": "max_tool_rounds", "path": "agent_py_agent", "limit": 20, "offset": 0}',
                '{"tool": "search_text", "query": "class .*Tool", "literal": false, "file_glob": "*.py"}',
                '{"tool": "search_text", "query": "TODO", "output_mode": "files_with_matches"}',
            ],
        )

    # LLM: SearchTextTool.execute 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 执行 SearchTextTool 的主流程并返回 ToolExecutionResult。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            request = _search_request_from_params(params, self.max_matches)
            target = self.resolve_path(request.raw_path)
        except ValueError as exc:
            return ToolExecutionResult("search_text", False, str(exc))
        if not target.exists():
            return ToolExecutionResult("search_text", False, f"路径不存在: {self.display_path(target)}")
        return self._search_target(target, request)

    # LLM: SearchTextTool._search_target keeps execute short and owns candidate traversal.
    # 函数用途: 遍历目标路径里的候选文件并汇总搜索结果。
    def _search_target(self, target: Path, request: _SearchRequest) -> ToolExecutionResult:
        try:
            matcher = _SearchMatcher.from_request(request)
        except ValueError as exc:
            return ToolExecutionResult("search_text", False, str(exc))
        hits = self._collect_hits_with_rg(target, request)
        if hits is None:
            hits = self._collect_hits(target, request, matcher)
        if request.output_mode == "files_with_matches":
            return ToolExecutionResult("search_text", True, _render_files_with_matches(hits, request))
        if request.output_mode == "count":
            return ToolExecutionResult("search_text", True, _render_match_counts(hits, request))
        return ToolExecutionResult("search_text", True, self._render_content_hits(hits, request))

    # LLM: SearchTextTool._collect_hits_with_rg uses ripgrep as a fast backend without exposing shell commands.
    # 函数用途: 优先用 rg 搜索；rg 不可用或执行异常时返回 None 交给 Python 兜底。
    def _collect_hits_with_rg(self, target: Path, request: _SearchRequest) -> list[_SearchHit] | None:
        rg_path = shutil.which("rg")
        if not rg_path:
            return None
        args = _rg_args(rg_path, target, request)
        try:
            result = subprocess.run(
                args,
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            return None
        if result.returncode == 1:
            return []
        if result.returncode != 0:
            return None
        return self._parse_rg_json_lines(result.stdout)

    # LLM: SearchTextTool._parse_rg_json_lines maps ripgrep JSON events into stable tool output rows.
    # 函数用途: 解析 rg --json 的 match 事件，忽略 summary/context 等非命中事件。
    def _parse_rg_json_lines(self, stdout: str) -> list[_SearchHit]:
        hits: list[_SearchHit] = []
        for raw_line in stdout.splitlines():
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                return []
            if event.get("type") != "match":
                continue
            hit = self._search_hit_from_rg_event(event)
            if hit is not None:
                hits.append(hit)
        return hits

    # LLM: SearchTextTool._search_hit_from_rg_event re-checks workspace boundaries for rg output paths.
    # 函数用途: 把单个 rg match 事件转成 SearchHit，并拒绝任何越界路径。
    def _search_hit_from_rg_event(self, event: dict[str, Any]) -> _SearchHit | None:
        data = event.get("data")
        if not isinstance(data, dict):
            return None
        path_text = _rg_text(data.get("path"))
        line_text = _rg_text(data.get("lines")).rstrip("\n")
        line_number = data.get("line_number")
        if not path_text or not isinstance(line_number, int):
            return None
        raw_path = Path(path_text)
        if not raw_path.is_absolute():
            raw_path = self.workspace_root / raw_path
        try:
            safe_item = self.resolve_path(raw_path)
        except ValueError:
            return None
        text = _read_text_safe(safe_item)
        lines = text.splitlines() if text is not None else [line_text]
        return _SearchHit(
            rel=self._item_relative_path(raw_path, safe_item),
            line_number=line_number,
            line=line_text,
            lines=lines,
        )

    # LLM: SearchTextTool._iter_search_candidates keeps recursive search deterministic and skips noisy dirs.
    # 函数用途: 生成 search_text 的候选文件列表，默认跳过常见缓存和依赖目录。
    def _iter_search_candidates(self, target: Path, request: _SearchRequest) -> list[Path]:
        if target.is_file():
            return [target]
        candidates: list[Path] = []
        for root, dirnames, filenames in os.walk(target):
            dirnames.sort()
            filenames.sort()
            if not request.include_ignored:
                dirnames[:] = [name for name in dirnames if name not in _COMMON_FILE_DISCOVERY_IGNORES]
            for filename in filenames:
                item = Path(root) / filename
                if not request.include_ignored and _path_has_ignored_part(item, target):
                    continue
                candidates.append(item)
        return candidates

    # LLM: SearchTextTool._collect_hits normalizes literal and regex search into one renderable shape.
    # 函数用途: 扫描候选文件并返回结构化命中，避免输出模式之间重复搜索。
    def _collect_hits(self, target: Path, request: _SearchRequest, matcher: _SearchMatcher) -> list[_SearchHit]:
        hits: list[_SearchHit] = []
        for item in self._iter_search_candidates(target, request):
            if request.file_glob and not self._matches_file_glob(item, request.file_glob):
                continue
            hits.extend(self._search_item_hits(item, request, matcher))
        return hits

    # LLM: SearchTextTool._search_item_hits reads one UTF-8 text file and never leaks paths outside workspace.
    # 函数用途: 搜索单个文件的所有命中并保留上下文渲染所需原始行。
    def _search_item_hits(
        self,
        item: Path,
        request: _SearchRequest,
        matcher: _SearchMatcher,
    ) -> list[_SearchHit]:
        try:
            safe_item = self.resolve_path(item)
        except ValueError:
            return []
        text = _read_text_safe(safe_item)
        if text is None:
            return []
        rel = self._item_relative_path(item, safe_item)
        lines = text.splitlines()
        return [
            _SearchHit(rel=rel, line_number=idx, line=line, lines=lines)
            for idx, line in enumerate(lines, start=1)
            if matcher.matches(line)
        ]

    # LLM: SearchTextTool._render_content_hits preserves grep-like line output and page notices.
    # 函数用途: 渲染 content 模式的 search_text 结果。
    def _render_content_hits(self, hits: list[_SearchHit], request: _SearchRequest) -> str:
        matches: list[str] = []
        for hit in _slice_hits(hits, request):
            self._append_search_match(
                _SearchMatch(
                    rel=hit.rel,
                    line_number=hit.line_number,
                    line=hit.line,
                    lines=hit.lines,
                    context=request.context,
                ),
                matches,
            )
        if len(hits) > request.offset + request.limit:
            matches.append(_search_page_notice({"seen": request.offset + request.limit + 1, "limit": request.limit}))
        return "\n".join(matches) or "没有找到匹配项"

    # LLM: _matches_file_glob keeps search_text file filtering literal and predictable.
    # 函数用途: 判断文件名或展示路径是否匹配用户传入的 glob。
    def _matches_file_glob(self, item: Path, file_glob: str) -> bool:
        display = self.display_path(item)
        return fnmatch.fnmatch(item.name, file_glob) or fnmatch.fnmatch(display, file_glob)

    # LLM: SearchTextTool._append_search_match 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 向结果或告警集合加入 append_search_match，同时保留调用方依赖的顺序。
    def _append_search_match(
        self,
        match: _SearchMatch,
        matches: list[str],
    ) -> None:
        snippet = self._make_snippet(match.line)
        matches.append(f"{match.rel}:{match.line_number}: {snippet}")
        if not match.lines or match.context <= 0:
            return
        start = max(1, match.line_number - match.context)
        end = min(len(match.lines), match.line_number + match.context)
        for idx in range(start, end + 1):
            if idx == match.line_number:
                continue
            matches.append(f"{match.rel}:{idx}: {self._make_snippet(match.lines[idx - 1])}")

    # LLM: SearchTextTool._make_snippet 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 工具系统 中的 make_snippet 步骤，并保持调用方依赖的数据形状。
    def _make_snippet(self, line: str) -> str:
        snippet = line.strip()
        if len(snippet) > _MAX_SEARCH_LINE_CHARS:
            snippet = snippet[:_MAX_SEARCH_LINE_CHARS] + "... 已截断"
        return snippet

    # LLM: SearchTextTool._item_relative_path 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 工具系统 中的 item_relative_path 步骤，并保持调用方依赖的数据形状。
    def _item_relative_path(self, item: Path, safe_item: Path) -> str:
        return self.display_path(safe_item if safe_item.is_absolute() else item)


# LLM: _SearchRequest bundles search_text business parameters to keep methods small and expandable.
# 类用途: 保存 search_text 的查询、分页和过滤参数，避免散落多个函数参数。
@dataclass(frozen=True)
class _SearchRequest:
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


# LLM: _SearchHit stores one normalized search hit before rendering to a selected output mode.
# 类用途: 保存 search_text 的单条命中，供 content/files/count 三种输出模式复用。
@dataclass(frozen=True)
class _SearchHit:
    rel: str
    line_number: int
    line: str
    lines: list[str]


# LLM: _SearchMatcher hides literal/regex differences so file traversal stays simple.
# 类用途: 保存 search_text 的匹配器，支持普通文本、正则和大小写开关。
@dataclass(frozen=True)
class _SearchMatcher:
    query: str
    literal: bool
    ignore_case: bool
    regex: re.Pattern[str] | None = None

    @classmethod
    def from_request(cls, request: _SearchRequest) -> _SearchMatcher:
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


# LLM: _SearchMatch bundles render data for a single search hit and optional context lines.
# 类用途: 保存 search_text 单条命中及其上下文渲染参数。
@dataclass(frozen=True)
class _SearchMatch:
    rel: str
    line_number: int
    line: str
    lines: list[str]
    context: int


# LLM: _search_request_from_params validates the JSON bundle from the model before filesystem work starts.
# 函数用途: 从工具参数中解析 search_text 的业务参数包。
def _search_request_from_params(params: dict[str, Any], max_matches: int) -> _SearchRequest:
    query_value = _bundled_filesystem_param(
        params,
        "query",
        _bundled_filesystem_param(params, "pattern"),
    )
    query = _text_param(
        query_value,
        name="query",
        max_chars=_MAX_SEARCH_QUERY_CHARS,
        strip=True,
    )
    output_mode = _text_param(
        _bundled_filesystem_param(params, "output_mode", "content"),
        name="output_mode",
        max_chars=40,
        strip=True,
    ).lower()
    if output_mode not in {"content", "files_with_matches", "count"}:
        raise ValueError("output_mode 只能是 content、files_with_matches 或 count")
    return _SearchRequest(
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


# LLM: _path_has_ignored_part prevents recursive search from wandering into dependency/cache dirs.
# 函数用途: 判断候选路径是否位于默认忽略目录之内。
def _path_has_ignored_part(item: Path, root: Path) -> bool:
    try:
        rel = item.relative_to(root)
    except ValueError:
        return False
    return any(part in _COMMON_FILE_DISCOVERY_IGNORES for part in rel.parts[:-1])


# LLM: _rg_args builds a subprocess argv list, never a shell string.
# 函数用途: 根据 search_text 参数生成受控 rg 命令参数。
def _rg_args(rg_path: str, target: Path, request: _SearchRequest) -> list[str]:
    args = [
        rg_path,
        "--json",
        "--line-number",
        "--color=never",
        "--hidden",
        "--no-config",
        "--no-messages",
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


# LLM: _rg_text extracts ripgrep JSON text fields without assuming byte payload support.
# 函数用途: 从 rg 的 {"text": "..."} 字段取字符串，坏结构返回空串。
def _rg_text(value: Any) -> str:
    if isinstance(value, dict):
        text = value.get("text")
        return text if isinstance(text, str) else ""
    return ""


# LLM: _slice_hits applies offset/limit after all hits are normalized.
# 函数用途: 对 search_text 命中做分页切片。
def _slice_hits(hits: list[_SearchHit], request: _SearchRequest) -> list[_SearchHit]:
    return hits[request.offset : request.offset + request.limit]


# LLM: _render_files_with_matches mirrors grep -l style output.
# 函数用途: 渲染只包含命中文件路径的 search_text 输出。
def _render_files_with_matches(hits: list[_SearchHit], request: _SearchRequest) -> str:
    paths = list(dict.fromkeys(hit.rel for hit in hits))
    page = paths[request.offset : request.offset + request.limit]
    if len(paths) > request.offset + request.limit:
        page.append(_search_page_notice({"seen": request.offset + request.limit + 1, "limit": request.limit}))
    return "\n".join(page) or "没有找到匹配项"


# LLM: _render_match_counts mirrors grep -c style output.
# 函数用途: 渲染每个文件的命中次数。
def _render_match_counts(hits: list[_SearchHit], request: _SearchRequest) -> str:
    counts: dict[str, int] = {}
    for hit in hits:
        counts[hit.rel] = counts.get(hit.rel, 0) + 1
    rows = [f"{path}: {count}" for path, count in counts.items()]
    page = rows[request.offset : request.offset + request.limit]
    if len(rows) > request.offset + request.limit:
        page.append(_search_page_notice({"seen": request.offset + request.limit + 1, "limit": request.limit}))
    return "\n".join(page) or "没有找到匹配项"


# LLM: _search_page_notice gives models a copyable offset for the next search_text page.
# 函数用途: 生成 search_text 分页继续查询提示。
def _search_page_notice(state: dict[str, int]) -> str:
    seen = state["seen"] - 1
    return f"... 已截断，next_offset={seen} limit={state['limit']}；继续搜索请再次调用 search_text 并传入 offset={seen}"
