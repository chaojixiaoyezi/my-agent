# LLM: search_text lives in its own module so file IO tools can grow without hitting code-size limits.
# 模块用途: 实现工作区文本搜索工具，支持分页、文件 glob 过滤和少量上下文展示。

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ._filesystem_helpers import (
    _MAX_SEARCH_LINE_CHARS,
    _read_text_safe,
)
from ._filesystem_read import (
    _COMMON_FILE_DISCOVERY_IGNORES,
    FileSystemAccessOptions,
    FileSystemTool,
)
from ._filesystem_search_models import (
    SearchHit,
    SearchMatch,
    SearchMatcher,
    SearchRequest,
    path_has_ignored_part,
    render_files_with_matches,
    render_match_counts,
    rg_args,
    rg_text,
    search_page_notice,
    search_request_from_params,
    slice_hits,
)
from ._filesystem_search_spec import build_search_text_spec
from .filesystem_path_recovery import MissingPathRequest, missing_path_result
from .models import ToolExecutionResult


# LLM: SearchTextTool 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: SearchTextTool 数据模型，集中保存 工具系统 的结构化状态。
class SearchTextTool(FileSystemTool):

    # LLM: SearchTextTool.__init__ 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 初始化 SearchTextTool 的依赖、配置和运行期字段。
    def __init__(
        self,
        workspace_root: Path,
        max_matches: int,
        workspace_roots: list[Path] | None = None,
        access_options: FileSystemAccessOptions | None = None,
    ):
        super().__init__(
            workspace_root,
            workspace_roots,
            access_options,
        )
        self.max_matches = max_matches
        self.spec = build_search_text_spec()

    # LLM: SearchTextTool.execute 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 执行 SearchTextTool 的主流程并返回 ToolExecutionResult。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            request = search_request_from_params(params, self.max_matches)
            target = self.resolve_path(request.raw_path)
        except ValueError as exc:
            return ToolExecutionResult("search_text", False, str(exc))
        if not target.exists():
            return missing_path_result(MissingPathRequest(
                tool_name="search_text",
                raw_path=request.raw_path,
                target=target,
                workspace_roots=self.workspace_roots,
                display_path=self.display_path(target),
                expected_kind="any",
                retry_tool="search_text",
            ))
        return self._search_target(target, request)

    # LLM: SearchTextTool._search_target keeps execute short and owns candidate traversal.
    # 函数用途: 遍历目标路径里的候选文件并汇总搜索结果。
    def _search_target(self, target: Path, request: SearchRequest) -> ToolExecutionResult:
        try:
            matcher = SearchMatcher.from_request(request)
        except ValueError as exc:
            return ToolExecutionResult("search_text", False, str(exc))
        hits = self._collect_hits_with_rg(target, request)
        if hits is None:
            hits = self._collect_hits(target, request, matcher)
        if request.output_mode == "files_with_matches":
            return ToolExecutionResult("search_text", True, render_files_with_matches(hits, request))
        if request.output_mode == "count":
            return ToolExecutionResult("search_text", True, render_match_counts(hits, request))
        return ToolExecutionResult("search_text", True, self._render_content_hits(hits, request))

    # LLM: SearchTextTool._collect_hits_with_rg uses ripgrep as a fast backend without exposing shell commands.
    # 函数用途: 优先用 rg 搜索；rg 不可用或执行异常时返回 None 交给 Python 兜底。
    def _collect_hits_with_rg(self, target: Path, request: SearchRequest) -> list[SearchHit] | None:
        rg_path = shutil.which("rg")
        if not rg_path:
            return None
        args = rg_args(rg_path, target, request)
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
    def _parse_rg_json_lines(self, stdout: str) -> list[SearchHit]:
        hits: list[SearchHit] = []
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
    def _search_hit_from_rg_event(self, event: dict[str, Any]) -> SearchHit | None:
        data = event.get("data")
        if not isinstance(data, dict):
            return None
        path_text = rg_text(data.get("path"))
        line_text = rg_text(data.get("lines")).rstrip("\n")
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
        return SearchHit(
            rel=_item_relative_path(self, raw_path, safe_item),
            line_number=line_number,
            line=line_text,
            lines=lines,
        )

    # LLM: SearchTextTool._iter_search_candidates keeps recursive search deterministic and skips noisy dirs.
    # 函数用途: 生成 search_text 的候选文件列表，默认跳过常见缓存和依赖目录。
    def _iter_search_candidates(self, target: Path, request: SearchRequest) -> list[Path]:
        if target.is_file():
            return [target]
        candidates: list[Path] = []
        for root, dirnames, filenames in os.walk(target):
            dirnames.sort()
            filenames.sort()
            self._filter_search_dirs(dirnames, request)
            items = [Path(root) / filename for filename in filenames]
            if not request.include_ignored:
                items = [item for item in items if not path_has_ignored_part(item, target)]
            candidates.extend(items)
        return candidates

    # LLM: SearchTextTool._filter_search_dirs keeps recursive traversal shallow.
    # 函数用途: 原地过滤常见忽略目录，include_ignored=true 时不动目录列表。
    def _filter_search_dirs(self, dirnames: list[str], request: SearchRequest) -> None:
        if not request.include_ignored:
            dirnames[:] = [name for name in dirnames if name not in _COMMON_FILE_DISCOVERY_IGNORES]

    # LLM: SearchTextTool._collect_hits normalizes literal and regex search into one renderable shape.
    # 函数用途: 扫描候选文件并返回结构化命中，避免输出模式之间重复搜索。
    def _collect_hits(self, target: Path, request: SearchRequest, matcher: SearchMatcher) -> list[SearchHit]:
        hits: list[SearchHit] = []
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
        request: SearchRequest,
        matcher: SearchMatcher,
    ) -> list[SearchHit]:
        try:
            safe_item = self.resolve_path(item)
        except ValueError:
            return []
        text = _read_text_safe(safe_item)
        if text is None:
            return []
        rel = _item_relative_path(self, item, safe_item)
        lines = text.splitlines()
        return [
            SearchHit(rel=rel, line_number=idx, line=line, lines=lines)
            for idx, line in enumerate(lines, start=1)
            if matcher.matches(line)
        ]

    # LLM: SearchTextTool._render_content_hits preserves grep-like line output and page notices.
    # 函数用途: 渲染 content 模式的 search_text 结果。
    def _render_content_hits(self, hits: list[SearchHit], request: SearchRequest) -> str:
        matches: list[str] = []
        for hit in slice_hits(hits, request):
            _append_search_match(
                SearchMatch(
                    rel=hit.rel,
                    line_number=hit.line_number,
                    line=hit.line,
                    lines=hit.lines,
                    context=request.context,
                ),
                matches,
            )
        if len(hits) > request.offset + request.limit:
            matches.append(search_page_notice(request.offset + request.limit, request.limit))
        return "\n".join(matches) or "没有找到匹配项"

    # LLM: _matches_file_glob keeps search_text file filtering literal and predictable.
    # 函数用途: 判断文件名或展示路径是否匹配用户传入的 glob。
    def _matches_file_glob(self, item: Path, file_glob: str) -> bool:
        display = self.display_path(item)
        return fnmatch.fnmatch(item.name, file_glob) or fnmatch.fnmatch(display, file_glob)


# LLM: _append_search_match renders one content-mode hit plus optional context lines.
# 函数用途: 向结果集合加入 search_text 命中行，保持 grep-like 输出形状。
def _append_search_match(match: SearchMatch, matches: list[str]) -> None:
    snippet = _make_snippet(match.line)
    matches.append(f"{match.rel}:{match.line_number}: {snippet}")
    if not match.lines or match.context <= 0:
        return
    start = max(1, match.line_number - match.context)
    end = min(len(match.lines), match.line_number + match.context)
    for idx in range(start, end + 1):
        if idx == match.line_number:
            continue
        matches.append(f"{match.rel}:{idx}: {_make_snippet(match.lines[idx - 1])}")


# LLM: _make_snippet clips one matching line without changing search semantics.
# 函数用途: 给 search_text 命中行生成短预览。
def _make_snippet(line: str) -> str:
    snippet = line.strip()
    if len(snippet) > _MAX_SEARCH_LINE_CHARS:
        snippet = snippet[:_MAX_SEARCH_LINE_CHARS] + "... 已截断"
    return snippet


# LLM: _item_relative_path keeps output paths display-only after workspace safety checks.
# 函数用途: 生成 search_text 的相对展示路径，不参与权限判断。
def _item_relative_path(tool: SearchTextTool, item: Path, safe_item: Path) -> str:
    return tool.display_path(safe_item if safe_item.is_absolute() else item)
