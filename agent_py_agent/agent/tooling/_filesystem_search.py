# LLM: search_text lives in its own module so file IO tools can grow without hitting code-size limits.
# 模块用途: 实现工作区文本搜索工具，支持分页、文件 glob 过滤和少量上下文展示。

from __future__ import annotations

import fnmatch
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
    _text_param,
)
from ._filesystem_read import FileSystemTool
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
                "path": "从哪个目录开始搜，默认是工作区根目录",
                "limit": "本次最多返回多少条匹配，默认使用工具配置上限",
                "offset": "跳过前多少条匹配，用于分页，默认 0",
                "file_glob": "只搜索匹配 glob 的文件，例如 *.py",
                "context": "每条命中前后额外展示多少行上下文，默认 0",
                "case_sensitive": "是否区分大小写，默认 false",
            },
            parameter_details={
                "query": "必填，直接按文本包含关系匹配，不做正则解析；默认大小写不敏感。",
                "path": "可选，把搜索范围缩小到某个子目录时更高效。",
                "limit": "分页大小；命中很多时先看小批量，再用 next_offset 继续。",
                "offset": "上一页返回 next_offset 后，下一次传入这里继续看。",
                "file_glob": "按文件名或工作区相对路径过滤，例如 *.py、docs/*.md。",
                "context": "需要看命中附近内容时传 1 或 2；越大越占 prompt。",
                "case_sensitive": "需要精确大小写匹配时传 true；日志和自然语言检索通常保持默认 false。",
            },
            examples=[
                '{"tool": "search_text", "query": "PromptBuilder"}',
                '{"tool": "search_text", "query": "max_tool_rounds", "path": "agent_py_agent", "limit": 20, "offset": 0}',
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
        search_root = target if target.is_dir() else target.parent
        candidates = [target] if target.is_file() else list(search_root.rglob("*"))
        matches: list[str] = []
        state = {"seen": 0, "limit": request.limit, "offset": request.offset, "context": request.context}
        for item in candidates:
            if not item.is_file():
                continue
            if request.file_glob and not self._matches_file_glob(item, request.file_glob):
                continue
            found = self._search_item_for_query(item, request, matches, state)
            if found == "full":
                return ToolExecutionResult("search_text", True, "\n".join(matches))
        return ToolExecutionResult("search_text", True, "\n".join(matches) or "没有找到匹配项")

    # LLM: _matches_file_glob keeps search_text file filtering literal and predictable.
    # 函数用途: 判断文件名或展示路径是否匹配用户传入的 glob。
    def _matches_file_glob(self, item: Path, file_glob: str) -> bool:
        display = self.display_path(item)
        return fnmatch.fnmatch(item.name, file_glob) or fnmatch.fnmatch(display, file_glob)

    # LLM: SearchTextTool._search_item_for_query 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 工具系统 中的 search_item_for_query 步骤，并保持调用方依赖的数据形状。
    def _search_item_for_query(
        self,
        item: Path,
        request: _SearchRequest,
        matches: list[str],
        state: dict[str, int],
    ) -> str:
        try:
            safe_item = self.resolve_path(item)
        except ValueError:
            return ""
        try:
            return self._search_lines(
                _SearchLineRequest(item=item, safe_item=safe_item, request=request, state=state),
                matches,
            )
        except UnicodeDecodeError:
            return ""

    # LLM: SearchTextTool._search_lines 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 工具系统 中的 search_lines 步骤，并保持调用方依赖的数据形状。
    def _search_lines(
        self,
        request: _SearchLineRequest,
        matches: list[str],
    ) -> str:
        lines = request.safe_item.read_text(encoding="utf-8").splitlines()
        query = request.request.query if request.request.case_sensitive else request.request.query.lower()
        for idx, line in enumerate(lines, start=1):
            haystack = line if request.request.case_sensitive else line.lower()
            if query not in haystack:
                continue
            if _skip_seen_match(request.state):
                continue
            if len(matches) >= request.state["limit"]:
                matches.append(_search_page_notice(request.state))
                return "full"
            self._append_search_match(
                _SearchMatch(
                    rel=self._item_relative_path(request.item, request.safe_item),
                    line_number=idx,
                    line=line,
                    lines=lines,
                    context=request.state["context"],
                ),
                matches,
            )
        return ""

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
    case_sensitive: bool


# LLM: _SearchLineRequest bundles one file scan so line parsing does not grow a long signature.
# 类用途: 保存 search_text 扫描单个文件所需的路径、查询词和分页状态。
@dataclass(frozen=True)
class _SearchLineRequest:
    item: Path
    safe_item: Path
    request: _SearchRequest
    state: dict[str, int]


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
    query = _text_param(
        _bundled_filesystem_param(params, "query"),
        name="query",
        max_chars=_MAX_SEARCH_QUERY_CHARS,
        strip=True,
    )
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
        case_sensitive=_bool_param(_bundled_filesystem_param(params, "case_sensitive", False), default=False),
    )


# LLM: _skip_seen_match owns offset accounting so paging behavior stays consistent.
# 函数用途: 更新已见命中计数，并判断当前命中是否应被 offset 跳过。
def _skip_seen_match(state: dict[str, int]) -> bool:
    seen = state["seen"]
    state["seen"] = seen + 1
    return seen < state["offset"]


# LLM: _search_page_notice gives models a copyable offset for the next search_text page.
# 函数用途: 生成 search_text 分页继续查询提示。
def _search_page_notice(state: dict[str, int]) -> str:
    seen = state["seen"] - 1
    return f"... 已截断，next_offset={seen} limit={state['limit']}；继续搜索请再次调用 search_text 并传入 offset={seen}"
