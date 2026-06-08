
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from itertools import islice
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
    render_line_numbers,
    render_match_counts,
    rg_args,
    rg_text,
    search_page_notice,
    search_request_from_params,
    slice_hits,
)
from .filesystem_path_recovery import MissingPathRequest, missing_path_result
from .models import ToolExecutionResult, ToolSpec

_SEARCH_TEXT_USE_CASES = [
    "想找某个函数、类、配置项出现在哪些文件里",
    "先全局搜索，再决定读哪几个文件",
    "大文件或长记录要逐项汇总时，先定位章节/检查点/锚点，再用 read_file 读取命中附近源片段",
]
_SEARCH_TEXT_AVOID_WHEN = [
    "已经知道具体文件并且要看上下文时，直接 read_file 更合适",
    "需要证明完整覆盖 source 时，search_text 只能定位，不能替代 read_file/read_artifact 证据",
]
_SEARCH_TEXT_PARAMETERS = {
    "query": "要搜索的文本",
    "path": "从哪个目录开始搜，默认是工作区根目录",
    "limit": "本次最多返回多少条匹配，默认使用工具配置上限",
    "offset": "跳过前多少条匹配，用于分页，默认 0",
    "file_glob": "只搜索匹配 glob 的文件，例如 *.py",
    "context": "每条命中前后额外展示多少行上下文，默认 0",
    "literal": "是否按普通文本匹配，默认 true；false 时按正则匹配",
    "ignore_case": "是否忽略大小写，默认 false",
    "output_mode": "输出模式：content、files_with_matches、count 或 line_numbers，默认 content",
    "include_ignored": "是否搜索 .git/node_modules 等常见噪声目录，默认 false",
}
_SEARCH_TEXT_PARAMETER_DETAILS = {
    "query": "必填，默认按文本包含关系匹配；需要正则时传 literal=false。",
    "path": "可选，把搜索范围缩小到某个子目录时更高效。",
    "limit": "分页大小；命中很多时先看小批量，再用 next_offset 继续。",
    "offset": "上一页返回 next_offset 后，下一次传入这里继续看。",
    "file_glob": "按文件名或工作区相对路径过滤，例如 *.py、docs/*.md。",
    "context": "需要看命中附近内容时传 1 或 2；越大越占 prompt。",
    "literal": "默认 true，避免把用户普通文字误当正则；传 false 才启用正则。",
    "ignore_case": "大小写不确定时传 true。",
    "output_mode": "content 返回行内容；line_numbers 只返回文件:行号；files_with_matches 只返回文件；count 返回每个文件命中数。",
    "include_ignored": "默认跳过 .git、node_modules 和常见缓存目录；确实要查时传 true。",
}
_SEARCH_TEXT_EXAMPLES = [
    '{"tool": "search_text", "query": "PromptBuilder"}',
    '{"tool": "search_text", "query": "max_tool_rounds", "path": "agent_py_agent", "limit": 20, "offset": 0}',
    '{"tool": "search_text", "query": "class .*Tool", "literal": false, "file_glob": "*.py"}',
    '{"tool": "search_text", "query": "===== 章节", "path": "data/long_field_journal.txt", "limit": 100}',
    '{"tool": "search_text", "query": "===== 章节", "path": "data/long_field_journal.txt", "output_mode": "line_numbers", "limit": 100}',
    '{"tool": "search_text", "query": "TODO", "output_mode": "files_with_matches"}',
]


class MalformedRgOutput(Exception):
    pass


@dataclass(frozen=True)
class RgHitAppendRequest:
    hits: list[SearchHit]
    seen_files: set[str]
    hit: SearchHit
    output_mode: str


def build_search_text_spec() -> ToolSpec:
    return ToolSpec(
        name="search_text",
        category="filesystem",
        effect="read_only",
        description="在工作区里搜索纯文本，适合找函数名、配置项、章节标记、日志锚点和大文件里的候选范围。",
        use_cases=_SEARCH_TEXT_USE_CASES,
        avoid_when=_SEARCH_TEXT_AVOID_WHEN,
        keywords=["搜索", "查找", "关键字", "grep", "rg", "全文检索", "文本匹配"],
        parameters=_SEARCH_TEXT_PARAMETERS,
        parameter_details=_SEARCH_TEXT_PARAMETER_DETAILS,
        examples=_SEARCH_TEXT_EXAMPLES,
    )


class SearchTextTool(FileSystemTool):

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

    def _search_target(self, target: Path, request: SearchRequest) -> ToolExecutionResult:
        try:
            matcher = SearchMatcher.from_request(request)
        except ValueError as exc:
            return ToolExecutionResult("search_text", False, str(exc))
        if request.output_mode == "count":
            counts = _collect_counts_with_rg(self, target, request)
            if counts is None:
                counts = self._collect_counts(target, request, matcher)
            return ToolExecutionResult(
                "search_text",
                True,
                render_match_counts(counts, request),
                result_envelope={"page_window": self._page_window(target, request, total_items=len(counts))},
            )
        hits = _collect_hits_with_rg(self, target, request)
        if hits is None:
            hits = self._collect_hits(target, request, matcher)
        if request.output_mode == "files_with_matches":
            total_items = len(dict.fromkeys(hit.rel for hit in hits))
            return ToolExecutionResult(
                "search_text",
                True,
                render_files_with_matches(hits, request),
                result_envelope={"page_window": self._page_window(target, request, total_items=total_items)},
            )
        if request.output_mode == "line_numbers":
            return ToolExecutionResult(
                "search_text",
                True,
                render_line_numbers(hits, request),
                result_envelope={"page_window": self._page_window(target, request, total_items=len(hits))},
            )
        return ToolExecutionResult(
            "search_text",
            True,
            self._render_content_hits(hits, request),
            result_envelope={"page_window": self._page_window(target, request, total_items=len(hits))},
        )

    def _iter_search_candidates(self, target: Path, request: SearchRequest) -> list[Path]:
        if target.is_file():
            return [target]
        candidates: list[Path] = []
        for root, dirnames, filenames in os.walk(target):
            dirnames.sort()
            filenames.sort()
            self._filter_search_dirs(dirnames, request)
            items = [Path(root) / filename for filename in filenames]
            items = [item for item in items if not item.is_symlink()]
            if not request.include_ignored:
                items = [item for item in items if not path_has_ignored_part(item, target)]
            candidates.extend(items)
        return candidates

    def _filter_search_dirs(self, dirnames: list[str], request: SearchRequest) -> None:
        if not request.include_ignored:
            dirnames[:] = [name for name in dirnames if name not in _COMMON_FILE_DISCOVERY_IGNORES]

    def _collect_hits(self, target: Path, request: SearchRequest, matcher: SearchMatcher) -> list[SearchHit]:
        hits: list[SearchHit] = []
        seen_files: set[str] = set()
        hit_budget = request.offset + request.limit + 1
        for item in self._iter_search_candidates(target, request):
            if request.file_glob and not self._matches_file_glob(item, request.file_glob):
                continue
            for hit in self._iter_search_item_hits(item, request, matcher):
                if request.output_mode == "files_with_matches":
                    if hit.rel in seen_files:
                        continue
                    seen_files.add(hit.rel)
                hits.append(hit)
                if len(hits) >= hit_budget:
                    return hits
        return hits

    def _collect_counts(self, target: Path, request: SearchRequest, matcher: SearchMatcher) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self._iter_search_candidates(target, request):
            if request.file_glob and not self._matches_file_glob(item, request.file_glob):
                continue
            for hit in self._iter_search_item_hits(item, request, matcher, include_context=False):
                counts[hit.rel] = counts.get(hit.rel, 0) + 1
        return counts

    def _iter_search_item_hits(
        self,
        item: Path,
        request: SearchRequest,
        matcher: SearchMatcher,
        *,
        include_context: bool = True,
    ):
        try:
            safe_item = self.resolve_path(item)
        except ValueError:
            return
        rel = _item_relative_path(self, item, safe_item)
        try:
            with safe_item.open("r", encoding="utf-8") as handle:
                for idx, line in enumerate(handle, start=1):
                    line = line.rstrip("\n")
                    if matcher.matches(line):
                        yield SearchHit(
                            rel=rel,
                            line_number=idx,
                            line=line,
                            context_lines=_line_window(safe_item, idx, request.context) if include_context else (),
                        )
        except UnicodeDecodeError:
            return
        except OSError:
            return

    def _render_content_hits(self, hits: list[SearchHit], request: SearchRequest) -> str:
        matches: list[str] = []
        for hit in slice_hits(hits, request):
            _append_search_match(
                SearchMatch(
                    rel=hit.rel,
                    line_number=hit.line_number,
                    line=hit.line,
                    context_lines=hit.context_lines,
                    context=request.context,
                ),
                matches,
            )
        if len(hits) > request.offset + request.limit:
            matches.append(search_page_notice(request.offset + request.limit, request.limit))
        return "\n".join(matches) or "没有找到匹配项"

    def _matches_file_glob(self, item: Path, file_glob: str) -> bool:
        display = self.display_path(item)
        return fnmatch.fnmatch(item.name, file_glob) or fnmatch.fnmatch(display, file_glob)

    def _page_window(self, target: Path, request: SearchRequest, *, total_items: int) -> dict[str, int | bool | str]:
        returned = max(0, min(request.limit, total_items - request.offset))
        has_more = total_items > request.offset + request.limit
        return {
            "kind": "offset_page",
            "tool": "search_text",
            "source_path": self.display_path(target),
            "offset": request.offset,
            "limit": request.limit,
            "returned": returned,
            "next_offset": request.offset + returned if has_more else 0,
            "complete": not has_more,
            "output_mode": request.output_mode,
        }


def _append_search_match(match: SearchMatch, matches: list[str]) -> None:
    snippet = _make_snippet(match.line)
    matches.append(f"{match.rel}:{match.line_number}: {snippet}")
    if not match.context_lines or match.context <= 0:
        return
    for idx, line in match.context_lines:
        if idx == match.line_number:
            continue
        matches.append(f"{match.rel}:{idx}: {_make_snippet(line)}")


def _make_snippet(line: str) -> str:
    snippet = line.strip()
    if len(snippet) > _MAX_SEARCH_LINE_CHARS:
        snippet = snippet[:_MAX_SEARCH_LINE_CHARS] + "... 已截断"
    return snippet


def _item_relative_path(tool: SearchTextTool, item: Path, safe_item: Path) -> str:
    return tool.display_path(safe_item if safe_item.is_absolute() else item)


def _collect_hits_with_rg(tool: SearchTextTool, target: Path, request: SearchRequest) -> list[SearchHit] | None:
    process = _start_rg_process(tool, target, request)
    if process is None:
        return None
    return _rg_hits_from_process(tool, process, request)


def _collect_counts_with_rg(tool: SearchTextTool, target: Path, request: SearchRequest) -> dict[str, int] | None:
    process = _start_rg_process(tool, target, request)
    if process is None:
        return None
    return _rg_counts_from_process(tool, process, request)


def _start_rg_process(
    tool: SearchTextTool,
    target: Path,
    request: SearchRequest,
) -> subprocess.Popen[str] | None:
    rg_path = shutil.which("rg")
    if not rg_path:
        return None
    try:
        return subprocess.Popen(
            rg_args(rg_path, target, request),
            cwd=str(tool.workspace_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        return None


def _rg_hits_from_process(
    tool: SearchTextTool,
    process: subprocess.Popen[str],
    request: SearchRequest,
) -> list[SearchHit] | None:
    try:
        hits, stopped_early = _read_rg_hits(tool, process, request)
        return_code = process.wait(timeout=2)
    except Exception:
        _stop_process(process)
        return None
    if stopped_early or return_code == 1:
        return hits
    return hits if return_code == 0 else None


def _read_rg_hits(
    tool: SearchTextTool,
    process: subprocess.Popen[str],
    request: SearchRequest,
) -> tuple[list[SearchHit], bool]:
    hits: list[SearchHit] = []
    seen_files: set[str] = set()
    stdout = process.stdout
    if stdout is None:
        raise RuntimeError("rg stdout missing")
    for raw_line in stdout:
        try:
            hit = _hit_from_rg_line(tool, raw_line, request)
        except MalformedRgOutput:
            _stop_process(process)
            return [], True
        if hit is None:
            continue
        if not _append_rg_hit(RgHitAppendRequest(
            hits=hits,
            seen_files=seen_files,
            hit=hit,
            output_mode=request.output_mode,
        )):
            continue
        if len(hits) >= request.offset + request.limit + 1:
            _stop_process(process)
            return hits, True
    return hits, False


def _append_rg_hit(request: RgHitAppendRequest) -> bool:
    if request.output_mode != "files_with_matches":
        request.hits.append(request.hit)
        return True
    if request.hit.rel in request.seen_files:
        return False
    request.seen_files.add(request.hit.rel)
    request.hits.append(request.hit)
    return True


def _rg_counts_from_process(
    tool: SearchTextTool,
    process: subprocess.Popen[str],
    request: SearchRequest,
) -> dict[str, int] | None:
    try:
        counts = _read_rg_counts(tool, process, request)
        return_code = process.wait(timeout=2)
    except Exception:
        _stop_process(process)
        return None
    if return_code == 1:
        return counts
    return counts if return_code == 0 else None


def _read_rg_counts(
    tool: SearchTextTool,
    process: subprocess.Popen[str],
    request: SearchRequest,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    stdout = process.stdout
    if stdout is None:
        raise RuntimeError("rg stdout missing")
    for raw_line in stdout:
        try:
            hit = _hit_from_rg_line(tool, raw_line, request, include_context=False)
        except MalformedRgOutput:
            _stop_process(process)
            return {}
        if hit is not None:
            counts[hit.rel] = counts.get(hit.rel, 0) + 1
    return counts


def _hit_from_rg_line(
    tool: SearchTextTool,
    raw_line: str,
    request: SearchRequest,
    *,
    include_context: bool = True,
) -> SearchHit | None:
    if not raw_line.strip():
        return None
    try:
        event = json.loads(raw_line)
    except json.JSONDecodeError:
        raise MalformedRgOutput from None
    if event.get("type") != "match":
        return None
    return _search_hit_from_rg_event(tool, event, request, include_context=include_context)


def _search_hit_from_rg_event(
    tool: SearchTextTool,
    event: dict[str, Any],
    request: SearchRequest,
    *,
    include_context: bool = True,
) -> SearchHit | None:
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    path_text = rg_text(data.get("path"))
    line_number = data.get("line_number")
    if not path_text or not isinstance(line_number, int):
        return None
    raw_path = _rg_event_path(tool, path_text)
    if raw_path.is_symlink():
        return None
    try:
        safe_item = tool.resolve_path(raw_path)
    except ValueError:
        return None
    return SearchHit(
        rel=_item_relative_path(tool, raw_path, safe_item),
        line_number=line_number,
        line=rg_text(data.get("lines")).rstrip("\n"),
        context_lines=_line_window(safe_item, line_number, request.context) if include_context else (),
    )


def _rg_event_path(tool: SearchTextTool, path_text: str) -> Path:
    raw_path = Path(path_text)
    return raw_path if raw_path.is_absolute() else tool.workspace_root / raw_path


def _line_window(path: Path, line_number: int, context: int) -> tuple[tuple[int, str], ...]:
    if context <= 0:
        return ()
    start = max(1, line_number - context)
    end = line_number + context
    try:
        return _line_window_rows(path, start, end)
    except (OSError, UnicodeDecodeError):
        return ()


def _line_window_rows(path: Path, start: int, end: int) -> tuple[tuple[int, str], ...]:
    with path.open("r", encoding="utf-8") as handle:
        return tuple(
            (idx, line.rstrip("\n"))
            for idx, line in enumerate(islice(handle, start - 1, end), start=start)
        )


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if hasattr(signal, "SIGKILL"):
            process.kill()
    except OSError:
        return
