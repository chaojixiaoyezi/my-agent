
# LLM: 两后端只收命中；正文当前页按文件一次补上下文，预算和源变化必须进入查询覆盖证据。
#   rg回收有界且kill后必须wait；stdout只由读线程finally关闭，取消回调不得跨线程关闭迭代流。
# 模块用途: 提供工作区内文本搜索，统一参数说明、ripgrep 快速路径和 Python 后备扫描。
from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..common.cancellation import ToolCancelled, raise_if_cancelled, register_cancellation_callback
from ._filesystem_helpers import (
    _FALLBACK_SCAN_MAX_FILES,
    _FALLBACK_SCAN_MAX_SECONDS,
    _MAX_SEARCH_LINE_CHARS,
    _discovery_file_batch,
    _discovery_identity,
    _discovery_matches_glob,
    _discovery_nofollow_stat,
    _iter_discovery_files,
    _safe_discovery_path,
    path_resolution_error_outcome,
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
    render_files_with_matches,
    render_line_numbers,
    render_match_counts,
    rg_args,
    rg_text,
    search_page_notice,
    search_request_from_params,
    slice_hits,
)
from .filesystem_artifact_guard import (
    is_tool_output_artifact_path,
    mark_tool_output_artifact_result,
)
from .filesystem_path_recovery import MissingPathRequest, missing_path_result
from .models import (
    ConcurrencyPolicy,
    EffectResolverPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

_SEARCH_TEXT_USE_CASES = [
    "想在已经存在的本地工作区里找某个函数、类、配置项出现在哪些文件里",
    "先按精确标识符或正则搜索本地代码，再决定读哪几个文件",
    "大文件或长记录要逐项汇总时，先定位章节/检查点/锚点，再用 read_file 读取命中附近源片段",
]
_SEARCH_TEXT_AVOID_WHEN = [
    "已经知道具体文件并且要看上下文时，直接 read_file 更合适",
    "需要证明完整覆盖 source 时，search_text 只能定位，不能替代 read_file/read_artifact 证据",
    "需要查互联网、GitHub 上尚未下载的仓库或语义答案时，应使用 web_search/联网工具；本工具只搜本地文件",
    "query 是一句自然语言问题而不是文件中连续出现的原文时，先改成标识符、短字面片段或显式正则",
]
_SEARCH_TEXT_PARAMETERS = {
    "query": "本地文件中要搜索的连续文本或正则",
    "path": "从哪个目录开始搜，默认是工作区根目录",
    "limit": "本次最多返回多少条匹配，默认使用工具配置上限",
    "offset": "跳过前多少条匹配，用于分页，默认 0",
    "file_glob": "只搜索匹配 glob 的文件，例如 *.py",
    "context": "每条命中前后额外展示多少行上下文，默认 0",
    "literal": "是否按普通文本匹配，默认 false；默认使用 ripgrep 正则语义",
    "ignore_case": "是否忽略大小写，默认 false",
    "output_mode": "输出模式：content、files_with_matches、count 或 line_numbers，默认 content",
    "include_ignored": "是否搜索 .git/node_modules 等常见噪声目录，默认 false",
}
_SEARCH_TEXT_PARAMETER_DETAILS = {
    "query": "必填；默认使用 ripgrep 正则语义，不做分词、语义检索或联网搜索；要把整段 query 当普通文字时传 literal=true。",
    "path": "可选，只能是当前工作区内已经存在的文件或目录；源码尚未下载时先获取正确 checkout。",
    "limit": "分页大小；命中很多时先看小批量，再用 next_offset 继续。",
    "offset": "上一页返回 next_offset 后，下一次传入这里继续看。",
    "file_glob": "按文件名或工作区相对路径过滤，例如 *.py、docs/*.md。",
    "context": "需要看命中附近内容时传 1 或 2；越大越占 prompt。",
    "literal": "默认 false，使用 ripgrep 正则语义；搜索包含正则符号的普通原文时传 true。",
    "ignore_case": "大小写不确定时传 true。",
    "output_mode": "content 返回行内容；line_numbers 只返回文件:行号；files_with_matches 只返回文件；count 返回每个文件命中数。",
    "include_ignored": "默认跳过 .git、node_modules 和常见缓存目录；确实要查时传 true。",
}
_SEARCH_TEXT_EXAMPLES = [
    '{"tool": "search_text", "query": "PromptBuilder"}',
    '{"tool": "search_text", "query": "LEVELS|level1|level2", "file_glob": "*.js"}',
    '{"tool": "search_text", "query": "max_tool_rounds", "path": "agent_py_agent", "limit": 20, "offset": 0}',
    '{"tool": "search_text", "query": "class .*Tool", "literal": false, "file_glob": "*.py"}',
    '{"tool": "search_text", "query": "===== 章节", "path": "data/long_field_journal.txt", "limit": 100}',
    '{"tool": "search_text", "query": "===== 章节", "path": "data/long_field_journal.txt", "output_mode": "line_numbers", "limit": 100}',
    '{"tool": "search_text", "query": "TODO", "output_mode": "files_with_matches"}',
]


_LOGGER = logging.getLogger(__name__)

class MalformedRgOutput(Exception):
    pass


# LLM: 逐调用状态通过ContextVar隔离；预算和源变化观察不产生路径权限。
# 类用途: 记录没有 rg 时本轮已扫描多少文件，以及是否触发安全上限。
@dataclass
class SearchScanState:
    started_at: float
    candidates: int = 0
    limited: bool = False
    limit_reason: str = ""
    backend: str = "rg"
    read_bytes: int = 0
    rg_stdout_bytes: int = 0
    retained_bytes: int = 0
    stale: bool = False
    issues: list[str] = field(default_factory=list)
    sources: dict[Path, tuple[int, ...]] = field(default_factory=dict)
    tool: Any = None

    # LLM: 原因列表有帽；后续命中不能抹去已观察到的失败或变化。
    # 函数用途: 记录本轮读取预算或来源不完整事实。
    def mark(self, reason: str, *, stale: bool = False) -> None:
        self.limited = True
        self.limit_reason = self.limit_reason or reason
        self.stale = self.stale or stale
        if reason not in self.issues and len(self.issues) < 32:
            self.issues.append(reason)


_SEARCH_READ_MAX_BYTES = 16 * 1024 * 1024
_SEARCH_RETAIN_MAX_BYTES = 4 * 1024 * 1024
_SEARCH_LINE_MAX_BYTES = 256 * 1024
_SEARCH_RG_MAX_BYTES = 32 * 1024 * 1024
_SEARCH_RG_RECORD_MAX_BYTES = 2 * 1024 * 1024
_SEARCH_STATE: ContextVar[SearchScanState | None] = ContextVar('filesystem_search_state', default=None)


# LLM: 本异常仅停止不能安全继续的文件，不捕获或替换取消异常。
# 类用途: 将读取中止传递回结构化覆盖状态。
class _SearchReadAbort(OSError):
    pass


# LLM: 只计真正保留的命中、来源指纹和上下文，不能先堆积后截断。
# 函数用途: 为搜索结果分配有界内存保留预算。
def _retain_search_bytes(size: int) -> bool:
    state = _SEARCH_STATE.get()
    if state is None:
        return True
    if state.retained_bytes + size > _SEARCH_RETAIN_MAX_BYTES:
        state.mark('retained_byte_limit')
        return False
    state.retained_bytes += size
    return True


# LLM: 读前读后都复用原路径策略；指纹相等只称best_effort，绝非原子文件版本。
# 函数用途: 登记或复核已授权来源的身份和变化。
def _observe_search_source(tool, path: Path) -> tuple[int, ...]:
    state = _SEARCH_STATE.get()
    try:
        safe = _safe_discovery_path(tool, path, root=path.parent,
                                    include_ignored=True, ignored_names=())
        if safe is None:
            raise _SearchReadAbort('source_access_changed')
        identity = _discovery_identity(_discovery_nofollow_stat(safe))
    except (OSError, ValueError):
        if state is not None:
            state.mark('source_unavailable', stale=path in state.sources)
        raise _SearchReadAbort('source_unavailable') from None
    if state is not None:
        previous = state.sources.get(safe)
        if previous is not None and previous != identity:
            state.mark('source_changed', stale=True)
            raise _SearchReadAbort('source_changed')
        if previous is None:
            if len(state.sources) >= _FALLBACK_SCAN_MAX_FILES:
                state.mark('source_file_limit')
                raise _SearchReadAbort('source_file_limit')
            if not _retain_search_bytes(len(str(safe).encode('utf-8', 'surrogatepass')) + 256):
                raise _SearchReadAbort('retained_byte_limit')
            state.sources[safe] = identity
    return identity


# LLM: 读取前比较真实FD与路径身份，返回前复核；这些检查不宣称消除所有文件系统竞态。
# 函数用途: 在单次打开内维护源观察边界。
@contextlib.contextmanager
def _checked_search_file(tool, path: Path):
    state = _SEARCH_STATE.get()
    expected = _observe_search_source(tool, path)
    with path.open('rb', buffering=0) as handle:
        if _discovery_identity(os.fstat(handle.fileno())) != expected:
            if state is not None:
                state.mark('source_replaced_before_read', stale=True)
            raise _SearchReadAbort('source_replaced_before_read')
        _observe_search_source(tool, path)
        try:
            yield handle
        finally:
            try:
                if _discovery_identity(os.fstat(handle.fileno())) != expected:
                    if state is not None:
                        state.mark('source_changed_during_read', stale=True)
                _observe_search_source(tool, path)
            except (OSError, ValueError):
                if state is not None:
                    state.mark('source_changed_after_read', stale=True)


# LLM: 只复核本轮实际观察过的有帽来源，不把未观察路径称为已验证。
# 函数用途: 在最终信封生成前发现源删除、替换或权限变化。
def _validate_search_sources(tool) -> None:
    state = _SEARCH_STATE.get()
    if state is not None:
        for path in tuple(state.sources):
            raise_if_cancelled()
            try:
                _observe_search_source(tool, path)
            except _SearchReadAbort:
                continue


@dataclass(frozen=True)
class RgHitAppendRequest:
    hits: list[SearchHit]
    seen_files: set[str]
    hit: SearchHit
    output_mode: str


@dataclass(frozen=True)
class MatchingLineRequest:
    path: Path
    rel: str
    search: SearchRequest
    matcher: SearchMatcher
    include_context: bool


@dataclass(frozen=True)
class AppendItemHitsRequest:
    item: Path
    search: SearchRequest
    matcher: SearchMatcher
    hits: list[SearchHit]
    seen_files: set[str]
    hit_budget: int


# LLM: This model contract must keep local lexical search distinct from internet/semantic research;
# tool routing may use hints, but runtime matching stays controlled only by typed parameters.
# 函数用途: 声明本地文本搜索的精确参数、适用范围和空结果修复提示，避免模型把它当搜索引擎。
def build_search_text_model_spec() -> ToolModelSpec:
    properties: dict[str, dict[str, Any]] = {
        name: {"type": "string", "description": _SEARCH_TEXT_PARAMETER_DETAILS[name]}
        for name in _SEARCH_TEXT_PARAMETERS
    }
    properties.update(
        {
            "limit": {"type": "integer", "minimum": 1, "description": _SEARCH_TEXT_PARAMETER_DETAILS["limit"]},
            "offset": {"type": "integer", "minimum": 0, "description": _SEARCH_TEXT_PARAMETER_DETAILS["offset"]},
            "context": {"type": "integer", "minimum": 0, "description": _SEARCH_TEXT_PARAMETER_DETAILS["context"]},
            "literal": {
                "type": "boolean",
                "default": False,
                "description": _SEARCH_TEXT_PARAMETER_DETAILS["literal"],
            },
            "ignore_case": {"type": "boolean", "description": _SEARCH_TEXT_PARAMETER_DETAILS["ignore_case"]},
            "include_ignored": {"type": "boolean", "description": _SEARCH_TEXT_PARAMETER_DETAILS["include_ignored"]},
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count", "line_numbers"],
                "description": _SEARCH_TEXT_PARAMETER_DETAILS["output_mode"],
            },
        }
    )
    return ToolModelSpec(
        name="search_text",
        description=(
            "只在当前本地工作区文件中做字面或正则匹配，适合找函数名、配置项、章节标记和日志锚点；"
            "不联网、不分词、不做语义搜索，默认使用 ripgrep 正则语义；精确原文请传 literal=true。"
        ),
        input_schema={
            "type": "object",
            "properties": properties,
            "required": ["query"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="filesystem",
            use_cases=tuple(_SEARCH_TEXT_USE_CASES),
            avoid_when=tuple(_SEARCH_TEXT_AVOID_WHEN),
            keywords=("本地搜索", "代码搜索", "文件搜索", "关键字", "grep", "rg", "文本匹配"),
            examples=tuple(_SEARCH_TEXT_EXAMPLES),
        ),
    )


# LLM: 工具实例不保存逐调用观察；先收命中和哨兵，再只补正文当前页的上下文。
# 类用途: 提供有界本地搜索及真实覆盖披露。
class SearchTextTool(FileSystemTool):

    model_spec = build_search_text_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(parameter_names=("path",)),
        output_policy=OutputPolicy(redaction="source_code"),
        promotes_task=True,
    )

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

    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        try:
            request = search_request_from_params(params, self.max_matches)
            target = self.resolve_path(request.raw_path)
        except ValueError as exc:
            # 参数/路径解析失败→TOOL_INVALID_ARGUMENTS(改参可修)；路径策略拒绝→原权限码+authorization。
            # not-found 走下面 missing_path_result(PATH_NOT_FOUND)。
            return path_resolution_error_outcome("search_text", exc)
        if not target.exists():
            return missing_path_result(MissingPathRequest(
                tool_name="search_text",
                raw_path=request.raw_path,
                target=target,
                workspace_roots=self.workspace_roots,
                display_path=self.display_path(target),
                expected_kind="any",
                retry_tool="search_text",
            decide_access=self.check_path_access,
            ))
        return self._search_target(target, request)

    # LLM: ContextVar在finally恢复，不能让并行调用共享预算或来源状态。
    # 函数用途: 为一次搜索绑定独立执行状态。
    def _search_target(self, target: Path, request: SearchRequest) -> ToolHandlerOutcome:
        token = _SEARCH_STATE.set(SearchScanState(started_at=time.monotonic(), tool=self))
        try:
            return self._search_target_run(target, request)
        finally:
            _SEARCH_STATE.reset(token)

    # LLM: 两后端收集不读context；正文只切页一次，补全失败保留原有哨兵事实。
    # 函数用途: 执行搜索、当前页补全和结果投影。
    def _search_target_run(self, target: Path, request: SearchRequest) -> ToolHandlerOutcome:
        try:
            matcher = SearchMatcher.from_request(request)
        except ValueError as exc:
            # 无效正则/匹配参数→TOOL_INVALID_ARGUMENTS(改 pattern 可修)；漏码兜底 UNKNOWN_ERROR
            # (retryable=False)会让模型放弃整个搜索而非修正 query/pattern。
            return ToolHandlerOutcome("search_text", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")
        if request.output_mode == "count":
            scan_state = _SEARCH_STATE.get()
            counts = _collect_counts_with_rg(self, target, request)
            if counts is None:
                scan_state.backend = 'python'
                scan_state.sources.clear()
                scan_state.retained_bytes = 0
                counts = self._collect_counts(target, request, matcher, scan_state)
            _validate_search_sources(self)
            result_envelope = _search_result_envelope(
                self,
                target,
                request,
                total_items=len(counts),
                scan_state=scan_state,
            )
            result = ToolHandlerOutcome(
                "search_text",
                True,
                _with_search_result_guidance(
                    render_match_counts(counts, request),
                    result_envelope,
                ),
                result_envelope=result_envelope,
            )
            return _search_result_with_source_projection(result, target, counts)
        scan_state = _SEARCH_STATE.get()
        hits = _collect_hits_with_rg(self, target, request)
        if hits is None:
            scan_state.backend = 'python'
            scan_state.sources.clear()
            scan_state.retained_bytes = 0
            hits = self._collect_hits(target, request, matcher, scan_state)
        _validate_search_sources(self)
        content_page = (_hydrate_search_page(self, slice_hits(hits, request), request)
                        if request.output_mode == 'content' else [])
        if request.output_mode == "files_with_matches":
            total_items = len(dict.fromkeys(hit.rel for hit in hits))
            result_envelope = _search_result_envelope(
                self,
                target,
                request,
                total_items=total_items,
                scan_state=scan_state,
            )
            result = ToolHandlerOutcome(
                "search_text",
                True,
                _with_search_result_guidance(
                    render_files_with_matches(hits, request),
                    result_envelope,
                ),
                result_envelope=result_envelope,
            )
            return _search_result_with_source_projection(
                result,
                target,
                (hit.rel for hit in hits),
            )
        if request.output_mode == "line_numbers":
            result_envelope = _search_result_envelope(
                self,
                target,
                request,
                total_items=len(hits),
                scan_state=scan_state,
            )
            result = ToolHandlerOutcome(
                "search_text",
                True,
                _with_search_result_guidance(
                    render_line_numbers(hits, request),
                    result_envelope,
                ),
                result_envelope=result_envelope,
            )
            return _search_result_with_source_projection(
                result,
                target,
                (hit.rel for hit in hits),
            )
        result_envelope = _search_result_envelope(
            self,
            target,
            request,
            total_items=len(hits),
            scan_state=scan_state,
        )
        result = ToolHandlerOutcome(
            "search_text",
            True,
            _with_search_result_guidance(
                self._render_content_hits(content_page, request,
                    has_more=len(hits) > request.offset + request.limit),
                result_envelope,
            ),
            result_envelope=result_envelope,
        )
        return _search_result_with_source_projection(
            result,
            target,
            (hit.rel for hit in hits),
        )

    # LLM: 目录下降和候选交出都经过共享权限及忽略策略，取消异常直接传播。
    # 函数用途: 逐个产出可搜索文件，并在后备扫描达到存活上限时停止。
    def _iter_search_candidates(
        self,
        target: Path,
        request: SearchRequest,
        scan_state: SearchScanState,
    ) -> Iterable[Path]:
        yield from _iter_discovery_files(self, target, include_ignored=request.include_ignored,
            ignored_names=_COMMON_FILE_DISCOVERY_IGNORES,
            scan_state=scan_state, admit=_admit_fallback_candidate)

    # LLM: Hit collection consumes only enough streamed candidates for one
    # requested page plus the has-more sentinel.
    # 函数用途: 从 Python 后备遍历中收集当前分页需要的文本命中。
    def _collect_hits(
        self,
        target: Path,
        request: SearchRequest,
        matcher: SearchMatcher,
        scan_state: SearchScanState,
    ) -> list[SearchHit]:
        hits: list[SearchHit] = []
        seen_files: set[str] = set()
        hit_budget = request.offset + request.limit + 1
        for item in self._iter_search_candidates(target, request, scan_state):
            raise_if_cancelled()
            if not self._should_search_item(item, request):
                continue
            if self._append_item_hits(AppendItemHitsRequest(
                item=item,
                search=request,
                matcher=matcher,
                hits=hits,
                seen_files=seen_files,
                hit_budget=hit_budget,
            )):
                return hits
        return hits

    # LLM: 提前收够哨兵时也关闭生成器，立即执行FD收口与末次来源检查。
    # 函数用途: 收集不含上下文的单文件命中。
    def _append_item_hits(self, request: AppendItemHitsRequest) -> bool:
        stream = self._iter_search_item_hits(request.item, request.search, request.matcher,
                                            include_context=False)
        with contextlib.closing(stream):
            for hit in stream:
                accepted = _append_search_hit(RgHitAppendRequest(request.hits,
                    request.seen_files, hit, request.search.output_mode))
                state = _SEARCH_STATE.get()
                if state is not None and state.limited:
                    return True
                if accepted and len(request.hits) >= request.hit_budget:
                    return True
        state = _SEARCH_STATE.get()
        return bool(state and state.limited)

    # LLM: 计数不保留正文或上下文，字节预算和遍历上限同样作用于该模式。
    # 函数用途: 统计各文件命中数，同时遵守后备扫描的文件数和时间上限。
    def _collect_counts(
        self,
        target: Path,
        request: SearchRequest,
        matcher: SearchMatcher,
        scan_state: SearchScanState,
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self._iter_search_candidates(target, request, scan_state):
            raise_if_cancelled()
            if not self._should_search_item(item, request):
                continue
            for hit in self._iter_search_item_hits(item, request, matcher, include_context=False):
                if hit.rel not in counts and not _retain_search_bytes(len(hit.rel.encode('utf-8')) + 64):
                    break
                counts[hit.rel] = counts.get(hit.rel, 0) + 1
            if scan_state.limited:
                break
        return counts

    def _should_search_item(self, item: Path, request: SearchRequest) -> bool:
        return not request.file_glob or self._matches_file_glob(item, request.file_glob)

    # LLM: 坏编码和读取失败进入覆盖状态，不能被跳过后返回complete；取消不在此捕获。
    # 函数用途: 从单个安全来源流式生成匹配行。
    def _iter_search_item_hits(
        self,
        item: Path,
        request: SearchRequest,
        matcher: SearchMatcher,
        *,
        include_context: bool = False,
    ):
        try:
            safe_item = self.resolve_path(item)
        except ValueError:
            return
        rel = _item_relative_path(self, item, safe_item)
        try:
            yield from _iter_matching_lines(MatchingLineRequest(
                path=safe_item,
                rel=rel,
                search=request,
                matcher=matcher,
                include_context=include_context,
            ))
        except UnicodeDecodeError:
            state = _SEARCH_STATE.get()
            if state is not None:
                state.mark('source_decode_error')
            return
        except OSError:
            state = _SEARCH_STATE.get()
            if state is not None:
                state.mark('source_read_error')
            return

    # LLM: 输入已经是当前页，不得再次offset切片或重新读取来源。
    # 函数用途: 以原逐命中格式渲染当前页及分页提示。
    def _render_content_hits(self, page: list[SearchHit], request: SearchRequest,
                             *, has_more: bool = False) -> str:
        matches: list[str] = []
        for hit in page:
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
        if has_more:
            matches.append(search_page_notice(request.offset + request.limit, request.limit))
        return "\n".join(matches) or "没有找到匹配项"

    # LLM: Python与原生候选批次共享glob语义，不继承平台默认的大小写折叠。
    # 函数用途: 复用发现策略筛选搜索文件。
    def _matches_file_glob(self, item: Path, file_glob: str) -> bool:
        return _discovery_matches_glob(self.display_path(item), item.name, file_glob)

    # LLM: 扫描完整性与已观察到的后续页分开，context失败不能抹去has_more。
    # 函数用途: 生成搜索分页事实，并在后备扫描提前停止时附上结构化原因。
    def _page_window(
        self,
        target: Path,
        request: SearchRequest,
        *,
        total_items: int,
        scan_state: SearchScanState | None = None,
    ) -> dict[str, int | bool | str]:
        returned = max(0, min(request.limit, total_items - request.offset))
        has_more = total_items > request.offset + request.limit
        scan_limited = bool(scan_state and scan_state.limited)
        payload: dict[str, int | bool | str] = {
            "kind": "offset_page",
            "tool": "search_text",
            "source_path": self.display_path(target),
            "offset": request.offset,
            "limit": request.limit,
            "returned": returned,
            "next_offset": request.offset + returned if has_more else 0,
            "complete": not has_more and not scan_limited,
            "output_mode": request.output_mode,
        }
        if scan_limited and scan_state is not None:
            payload.update(
                {
                    "scan_limited": True,
                    "scanned_files": scan_state.candidates,
                    "scan_limit_reason": scan_state.limit_reason,
                }
            )
        return payload


# LLM: 两后端的分页、预算和源变化事实统一进入查询证据；坏rg协议仍回原Python实查。
# No-match and incomplete-search decisions come only from typed execution facts. The
# envelope does not infer repository absence or auto-route based on natural-language queries.
# 函数用途: 统一记录本地搜索范围、匹配方式、后端和覆盖状态，让模型知道空结果究竟代表什么。
def _search_result_envelope(
    tool: SearchTextTool,
    target: Path,
    request: SearchRequest,
    *,
    total_items: int,
    scan_state: SearchScanState | None,
) -> dict[str, object]:
    page_window = tool._page_window(
        target,
        request,
        total_items=total_items,
        scan_state=scan_state,
    )
    scan_limited = bool(scan_state and scan_state.limited)
    if scan_limited:
        status = "incomplete"
        hint_code = "LOCAL_SCAN_INCOMPLETE"
        suggested_actions = ["narrow_path", "set_file_glob", "install_rg"]
    elif total_items > 0:
        status = "matches"
        hint_code = ""
        suggested_actions = []
    else:
        status = "no_matches"
        hint_code = "LOCAL_LITERAL_NO_MATCH" if request.literal else "LOCAL_REGEX_NO_MATCH"
        suggested_actions = ["verify_local_path", "shorten_query"]
        if request.literal:
            suggested_actions.append("use_explicit_regex")
        suggested_actions.append("use_web_search_for_internet_scope")
    from .query_evidence import QueryObservation, query_evidence
    source_stale = bool(scan_state and scan_state.stale)
    observation_error = ('FILE_SOURCE_STALE' if source_stale
                         else 'LOCAL_SCAN_INCOMPLETE' if scan_limited else '')
    observation = QueryObservation(
        source="local_workspace", conditions={"query": request.query, "path": tool.display_path(target),
            "file_glob": request.file_glob, "literal": request.literal, "ignore_case": request.ignore_case,
            "include_ignored": request.include_ignored, "offset": request.offset, "limit": request.limit,
            "context": request.context, "output_mode": request.output_mode, "access_filter": "current_tool_path_policy"},
        returned_count=int(page_window["returned"]), observed_count=total_items,
        coverage="incomplete" if scan_limited else "page_window" if not page_window["complete"] else "complete",
        parser_version="local_text_search.v1", total_count=total_items if page_window["complete"] else None,
        error_code=observation_error,
        count_unit="matching_files" if request.output_mode in {"count", "files_with_matches"} else "matching_lines",
    )
    envelope = {
        "query_evidence": query_evidence(observation),
        "page_window": page_window,
        "search_result": {
            "schema": "local_text_search.v1",
            "scope": "local_workspace",
            "source_path": tool.display_path(target),
            "match_mode": "literal" if request.literal else "regex",
            "ignore_case": request.ignore_case,
            "backend": scan_state.backend if scan_state is not None else "rg",
            "status": status,
            "hint_code": hint_code,
            "returned": page_window["returned"],
            "scan_complete": bool(page_window["complete"]),
            "scanned_files": scan_state.candidates if scan_state is not None else 0,
            "scanned_files_known": bool(scan_state and scan_state.backend == 'python'),
            "suggested_actions": suggested_actions,
        },
    }
    if scan_limited and scan_state is not None:
        envelope['search_result'].update({
            'consistency': 'stale' if source_stale else 'best_effort',
            'partial': scan_limited, 'issues': list(scan_state.issues),
            'source_error_code': observation_error, 'read_bytes': scan_state.read_bytes,
            'read_byte_limit': _SEARCH_READ_MAX_BYTES,
            'retained_bytes': scan_state.retained_bytes,
            'retained_byte_limit': _SEARCH_RETAIN_MAX_BYTES,
            'rg_stdout_bytes': scan_state.rg_stdout_bytes,
            'rg_source_read_bytes_known': False,
        })
    return envelope


# LLM: The fallback limit is an objective liveness fence, not a no-match claim;
# callers must expose an incomplete result and ask the model to narrow scope.
# 函数用途: 在 Python 后备搜索扫描文件过多或耗时过久前停止继续遍历。
def _admit_fallback_candidate(state: SearchScanState) -> bool:
    if state.candidates >= _FALLBACK_SCAN_MAX_FILES:
        state.limited = True
        state.limit_reason = "file_limit"
        return False
    if time.monotonic() - state.started_at >= _FALLBACK_SCAN_MAX_SECONDS:
        state.limited = True
        state.limit_reason = "time_limit"
        return False
    state.candidates += 1
    return True


# LLM: Model-visible repair guidance is a display of the typed search_result envelope. It may
# distinguish local no-match from incomplete coverage, but it must not infer source absence or
# silently turn a lexical query into semantic/internet search.
# 函数用途: 空结果时说明实际搜了哪里、怎么匹配和下一步怎么改；有命中时保持原输出简洁。
def _with_search_result_guidance(
    text: str,
    result_envelope: dict[str, object],
) -> str:
    facts = result_envelope.get("search_result")
    facts = facts if isinstance(facts, dict) else {}
    status = str(facts.get("status") or "")
    if status == "matches":
        return text
    lines = [
        text,
        "",
        "[local-search-result]",
        "- query_evidence: " + json.dumps(result_envelope.get("query_evidence", {}), ensure_ascii=False, sort_keys=True),
        f"- status: {status or 'unknown'}",
        f"- hint_code: {facts.get('hint_code', '')}",
        f"- searched_path: {facts.get('source_path', '')}",
        f"- match_mode: {facts.get('match_mode', '')}",
        f"- backend: {facts.get('backend', '')}",
        f"- scan_complete: {str(bool(facts.get('scan_complete', False))).lower()}",
        "- scope: 仅限本地工作区文件；不联网，也不按语义回答问题",
    ]
    if status == "incomplete":
        page_window = result_envelope.get("page_window")
        page_window = page_window if isinstance(page_window, dict) else {}
        lines.extend(
            [
                f"- scanned_files: {facts.get('scanned_files', 0)}",
                f"- scan_limit_reason: {page_window.get('scan_limit_reason', '')}",
                "- next: 搜索未完整覆盖；请缩小 path/file_glob 或安装 rg 后重试，不能据此认定本地完全无匹配",
            ]
        )
    else:
        lines.append(
            "- next: 先确认本地 checkout/path，再用更短的精确标识符或显式正则重试；联网范围请使用 web_search"
        )
    return "\n".join(lines)


def _search_result_with_source_projection(
    result: ToolHandlerOutcome,
    target: Path,
    matched_paths: Iterable[str],
) -> ToolHandlerOutcome:
    if is_tool_output_artifact_path(target) or any(
        is_tool_output_artifact_path(Path(path))
        for path in matched_paths
    ):
        return mark_tool_output_artifact_result(result)
    return result


def _append_search_match(match: SearchMatch, matches: list[str]) -> None:
    snippet = _make_snippet(match.line)
    matches.append(f"{match.rel}:{match.line_number}: {snippet}")
    if not match.context_lines or match.context <= 0:
        return
    for idx, line in match.context_lines:
        if idx == match.line_number:
            continue
        matches.append(f"{match.rel}:{idx}: {_make_snippet(line)}")


# LLM: 非正文模式不保留行正文；offset之前和哨兵均不包含context。
# 函数用途: 统一有帽命中保留及按文件去重。
def _append_search_hit(request: RgHitAppendRequest) -> bool:
    if request.output_mode == 'files_with_matches' and request.hit.rel in request.seen_files:
        return False
    hit = request.hit
    if request.output_mode != 'content':
        hit = replace(hit, line='', context_lines=())
    if not _retain_search_bytes(len(hit.rel.encode('utf-8')) + len(hit.line.encode('utf-8')) + 128):
        return False
    if request.output_mode == 'files_with_matches':
        request.seen_files.add(hit.rel)
    request.hits.append(hit)
    return True


# LLM: 收集阶段只有一次FD扫描；context在当前正文页确定后统一补全。
# 函数用途: 用有界真实读取器生成匹配行。
def _iter_matching_lines(request: MatchingLineRequest):
    state = _SEARCH_STATE.get()
    if state is None or state.tool is None:
        raise _SearchReadAbort('search_execution_state_missing')
    with _checked_search_file(state.tool, request.path) as handle:
        for idx, line in _bounded_source_lines(handle):
            hit = _matching_line_hit(request, idx, line)
            if hit is not None:
                yield hit


def _non_null_hits(hits):
    for hit in hits:
        raise_if_cancelled()
        if hit is not None:
            yield hit


# LLM: 收集阶段只构造匹配，不为跳过行、哨兵或非正文模式打开上下文窗口。
# 函数用途: 将匹配行转换为无上下文命中。
def _matching_line_hit(request: MatchingLineRequest, idx: int, raw_line: str) -> SearchHit | None:
    line = raw_line.rstrip("\n")
    if not request.matcher.matches(line):
        return None
    return SearchHit(
        rel=request.rel,
        line_number=idx,
        line=line,
        context_lines=(),
    )


def _make_snippet(line: str) -> str:
    snippet = line.strip()
    if len(snippet) > _MAX_SEARCH_LINE_CHARS:
        snippet = snippet[:_MAX_SEARCH_LINE_CHARS] + "... 已截断"
    return snippet


def _item_relative_path(tool: SearchTextTool, item: Path, safe_item: Path) -> str:
    return tool.display_path(safe_item if safe_item.is_absolute() else item)


# LLM: rg lifetime is registered on the shared cancellation token so /stop can
# terminate a blocked stdout reader instead of waiting for process completion.
# 函数用途: 使用 rg 流式收集命中，并在当前工具调用取消时结束子进程。
def _collect_hits_with_rg(tool: SearchTextTool, target: Path, request: SearchRequest) -> list[SearchHit] | None:
    process = _start_rg_process(tool, target, request)
    if process is None:
        return None
    with register_cancellation_callback(lambda: _stop_process(process)):
        return _rg_hits_from_process(tool, process, request)


# LLM: Count-mode rg uses the same process cancellation boundary as content mode.
# 函数用途: 使用可取消的 rg 子进程统计各文件命中数。
def _collect_counts_with_rg(tool: SearchTextTool, target: Path, request: SearchRequest) -> dict[str, int] | None:
    process = _start_rg_process(tool, target, request)
    if process is None:
        return None
    with register_cancellation_callback(lambda: _stop_process(process)):
        return _rg_counts_from_process(tool, process, request)


# LLM: rg协议按原始字节有界读取；源读取量没有可核算接口，不把stdout量当来源量。
# 函数用途: 启动本次可取消的rg进程。
def _start_rg_process(
    tool: SearchTextTool,
    target: Path,
    request: SearchRequest,
) -> subprocess.Popen[str] | None:
    rg_path = shutil.which("rg")
    if not rg_path:
        return None
    state = _SEARCH_STATE.get()
    candidates = _discovery_file_batch(tool, target, include_ignored=request.include_ignored,
        ignored_names=_COMMON_FILE_DISCOVERY_IGNORES, scan_state=state,
        admit=_admit_fallback_candidate, file_glob=request.file_glob)
    if not candidates:
        return None
    try:
        return subprocess.Popen(
            rg_args(rg_path, candidates, request),
            cwd=str(tool.workspace_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=False,
        )
    except Exception:
        _LOGGER.debug("rg spawn failed; falling back to python search", exc_info=True)
        return None


# LLM: 取消原样抛出，普通读失败走原Python后备；所有退出均在读线程finally回收进程并关闭stdout。
# 函数用途: 收集rg命中并保证流和子进程收口，取消回调不碰读线程持有的stdout。
def _rg_hits_from_process(
    tool: SearchTextTool,
    process: subprocess.Popen[str],
    request: SearchRequest,
) -> list[SearchHit] | None:
    try:
        hits, stopped_early = _read_rg_hits(tool, process, request)
        return_code = process.wait(timeout=2)
    except ToolCancelled:
        raise
    except Exception:
        _LOGGER.debug("rg hits read failed; falling back to python search", exc_info=True)
        return None
    finally:
        try:
            _stop_process(process)
        finally:
            if process.stdout is not None:
                with contextlib.suppress(OSError):
                    process.stdout.close()
    state = _SEARCH_STATE.get()
    if stopped_early or return_code == 1 or (state is not None and state.limited):
        return hits
    return hits if return_code == 0 else None


# LLM: 坏 JSON 必须失败回原 Python 实查；不能把未解析 stdout 的零条当有效 no_matches。
# 函数用途: 流式读取已声明 rg JSON 协议并保留分页上界。
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
    for raw_line in _bounded_rg_records(stdout):
        raise_if_cancelled()
        try:
            hit = _hit_from_rg_line(tool, raw_line, request)
        except MalformedRgOutput:
            _stop_process(process)
            raise
        if hit is None:
            state = _SEARCH_STATE.get()
            if state is not None and state.limited:
                _stop_process(process)
                return hits, True
            continue
        if not _append_search_hit(RgHitAppendRequest(
            hits=hits,
            seen_files=seen_files,
            hit=hit,
            output_mode=request.output_mode,
        )):
            state = _SEARCH_STATE.get()
            if state is not None and state.limited:
                _stop_process(process)
                return hits, True
            continue
        if len(hits) >= request.offset + request.limit + 1:
            _stop_process(process)
            return hits, True
    state = _SEARCH_STATE.get()
    if state is not None and state.limited:
        _stop_process(process)
        return hits, True
    return hits, False


# LLM: 取消原样抛出，普通计数失败走原后备；读线程负责finally清理stdout并有界回收进程。
# 函数用途: 读取rg计数并释放对应进程和管道，不让取消线程关闭正在读取的流。
def _rg_counts_from_process(
    tool: SearchTextTool,
    process: subprocess.Popen[str],
    request: SearchRequest,
) -> dict[str, int] | None:
    try:
        counts = _read_rg_counts(tool, process, request)
        return_code = process.wait(timeout=2)
    except ToolCancelled:
        raise
    except Exception:
        _LOGGER.debug("rg counts read failed; falling back to python search", exc_info=True)
        return None
    finally:
        try:
            _stop_process(process)
        finally:
            if process.stdout is not None:
                with contextlib.suppress(OSError):
                    process.stdout.close()
    state = _SEARCH_STATE.get()
    if return_code == 1 or (state is not None and state.limited):
        return counts
    return counts if return_code == 0 else None


# LLM: 数量只由有效 rg match 事件累加；解析损坏抛错给原后备执行路径。
# 函数用途: 从声明 JSON 协议统计匹配文件行数，避免误报正常零结果。
def _read_rg_counts(
    tool: SearchTextTool,
    process: subprocess.Popen[str],
    request: SearchRequest,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    stdout = process.stdout
    if stdout is None:
        raise RuntimeError("rg stdout missing")
    for raw_line in _bounded_rg_records(stdout):
        raise_if_cancelled()
        try:
            hit = _hit_from_rg_line(tool, raw_line, request, include_context=False)
        except MalformedRgOutput:
            _stop_process(process)
            raise
        if hit is not None:
            if hit.rel not in counts and not _retain_search_bytes(len(hit.rel.encode('utf-8')) + 64):
                _stop_process(process)
                return counts
            counts[hit.rel] = counts.get(hit.rel, 0) + 1
        state = _SEARCH_STATE.get()
        if state is not None and state.limited:
            _stop_process(process)
            return counts
    state = _SEARCH_STATE.get()
    if state is not None and state.limited:
        _stop_process(process)
    return counts


# LLM: 真实管道使用有界readline；迭代仅兼容既有替身，rg内部源读取量保持未知。
# 函数用途: 限制单条JSON、累计stdout及记录间可协作时间。
def _bounded_rg_records(stdout):
    state = _SEARCH_STATE.get()
    readline = getattr(stdout, 'readline', None)
    iterator = None if callable(readline) else iter(stdout)
    while True:
        raise_if_cancelled()
        if state is not None and time.monotonic() - state.started_at >= _FALLBACK_SCAN_MAX_SECONDS:
            state.mark('rg_time_limit')
            return
        remaining = _SEARCH_RG_MAX_BYTES - (state.rg_stdout_bytes if state is not None else 0)
        if remaining <= 0:
            if state is not None:
                state.mark('rg_stdout_byte_limit')
            return
        size = min(_SEARCH_RG_RECORD_MAX_BYTES + 1, remaining)
        raw = readline(size) if callable(readline) else next(iterator, b'')
        if not raw:
            return
        encoded = raw.encode('utf-8') if isinstance(raw, str) else raw
        if state is not None:
            state.rg_stdout_bytes += len(encoded)
        if (len(encoded) > _SEARCH_RG_RECORD_MAX_BYTES
                or (callable(readline) and len(encoded) >= size and not encoded.endswith(b'\n'))):
            if state is not None:
                state.mark('rg_record_or_stdout_limit')
            return
        try:
            yield encoded.decode('utf-8')
        except UnicodeDecodeError:
            raise MalformedRgOutput from None


# LLM: begin/end只用于best_effort来源观察，不是原子文件版本；事件解析不补上下文。
# 函数用途: 解析有界rg协议并观察来源。
def _hit_from_rg_line(
    tool: SearchTextTool,
    raw_line: str,
    request: SearchRequest,
    *,
    include_context: bool = False,
) -> SearchHit | None:
    if not raw_line.strip():
        return None
    try:
        event = json.loads(raw_line)
    except json.JSONDecodeError:
        raise MalformedRgOutput from None
    if not isinstance(event, dict):
        raise MalformedRgOutput
    if event.get('type') in {'begin', 'end'}:
        data = event.get('data')
        if not isinstance(data, dict):
            raise MalformedRgOutput
        path_text = rg_text(data.get('path'))
        if path_text:
            try:
                safe = tool.resolve_path(_rg_event_path(tool, path_text))
                _observe_search_source(tool, safe)
            except (OSError, ValueError):
                state = _SEARCH_STATE.get()
                if state is not None:
                    state.mark('rg_source_unavailable')
        return None
    if event.get('type') != 'match':
        return None
    return _search_hit_from_rg_event(tool, event, request, include_context=include_context)


# LLM: 只产生无context命中；offset前、哨兵和非正文模式不打开任何上下文窗口。
# 函数用途: 将有效rg匹配转换为命中并复核来源。
def _search_hit_from_rg_event(
    tool: SearchTextTool,
    event: dict[str, Any],
    request: SearchRequest,
    *,
    include_context: bool = False,
) -> SearchHit | None:
    data = event.get('data')
    if not isinstance(data, dict):
        raise MalformedRgOutput
    path_text = rg_text(data.get('path'))
    line_number = data.get('line_number')
    if not path_text or type(line_number) is not int or line_number < 1:
        raise MalformedRgOutput
    raw_path = _rg_event_path(tool, path_text)
    if raw_path.is_symlink():
        return None
    try:
        safe_item = tool.resolve_path(raw_path)
    except ValueError:
        return None
    try:
        _observe_search_source(tool, safe_item)
    except OSError:
        return None
    lines = data.get('lines')
    if not isinstance(lines, dict) or not isinstance(lines.get('text'), str):
        state = _SEARCH_STATE.get()
        if state is not None:
            state.mark('rg_source_decode_error')
        return None
    line = lines['text'].rstrip('\n').rstrip('\r')
    if len(line.encode('utf-8')) > _SEARCH_LINE_MAX_BYTES:
        state = _SEARCH_STATE.get()
        if state is not None:
            state.mark('line_byte_limit')
        return None
    return SearchHit(rel=_item_relative_path(tool, raw_path, safe_item),
                     line_number=line_number, line=line, context_lines=())


def _rg_event_path(tool: SearchTextTool, path_text: str) -> Path:
    raw_path = Path(path_text)
    return raw_path if raw_path.is_absolute() else tool.workspace_root / raw_path


# LLM: 单次真实FD读取至多64KiB，空档和预读计入预算；单行不能无界分配。
# 函数用途: 在时间、字节及行大小上限内解码来源。
def _bounded_source_lines(handle):
    state = _SEARCH_STATE.get()
    pending = b''
    index = 0
    while True:
        raise_if_cancelled()
        if state is not None and time.monotonic() - state.started_at >= _FALLBACK_SCAN_MAX_SECONDS:
            state.mark('read_time_limit')
            return
        newline = pending.find(b'\n')
        if newline >= 0:
            if newline > _SEARCH_LINE_MAX_BYTES:
                if state is not None:
                    state.mark('line_byte_limit')
                return
            raw, pending = pending[:newline], pending[newline + 1:]
            index += 1
            yield index, raw.decode('utf-8').rstrip('\r')
            continue
        if len(pending) > _SEARCH_LINE_MAX_BYTES:
            if state is not None:
                state.mark('line_byte_limit')
            return
        remaining = _SEARCH_READ_MAX_BYTES - (state.read_bytes if state is not None else 0)
        if remaining <= 0:
            if state is not None:
                state.mark('read_byte_limit')
            return
        chunk = handle.read(min(64 * 1024, remaining))
        if state is not None:
            state.read_bytes += len(chunk)
        if not chunk:
            if pending:
                yield index + 1, pending.decode('utf-8').rstrip('\r')
            return
        pending += chunk


# LLM: 合并只改变I/O次数，不改变每条命中的原显示窗口。
# 函数用途: 合并当前文件当前页重叠或相邻的上下文区间。
def _merged_search_windows(hits: list[SearchHit], context: int) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted((max(1, h.line_number - context), h.line_number + context) for h in hits):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


# LLM: 输入仅是当前content页；每文件补全只打开一次，不为offset之前或哨兵读取context。
# 函数用途: 按文件合并读取并恢复逐命中上下文展示。
def _hydrate_search_page(tool, page: list[SearchHit], request: SearchRequest) -> list[SearchHit]:
    if request.context <= 0 or not page:
        return page
    state = _SEARCH_STATE.get()
    grouped: dict[Path, list[tuple[int, SearchHit]]] = {}
    result = list(page)
    for position, hit in enumerate(page):
        raise_if_cancelled()
        try:
            path = tool.resolve_path(hit.rel)
            _observe_search_source(tool, path)
        except (OSError, ValueError):
            if state is not None:
                state.mark('context_source_unavailable', stale=True)
            continue
        grouped.setdefault(path, []).append((position, hit))
    for path, indexed_hits in grouped.items():
        windows = _merged_search_windows([hit for _, hit in indexed_hits], request.context)
        rows: dict[int, str] = {}
        window_index = 0
        failed = False
        try:
            with _checked_search_file(tool, path) as handle:
                for number, line in _bounded_source_lines(handle):
                    while window_index < len(windows) and number > windows[window_index][1]:
                        window_index += 1
                    if window_index == len(windows):
                        break
                    if windows[window_index][0] <= number <= windows[window_index][1]:
                        if not _retain_search_bytes(len(line.encode('utf-8')) + 64):
                            break
                        rows[number] = line
                    if number >= windows[-1][1]:
                        break
            failed = bool(state and state.limited)
        except UnicodeDecodeError:
            if state is not None:
                state.mark('context_decode_error')
            failed = True
        except (OSError, ValueError):
            if state is not None:
                state.mark('context_read_error')
            failed = True
        if state is not None and state.stale:
            continue
        for position, hit in indexed_hits:
            if rows.get(hit.line_number) != hit.line:
                if state is not None:
                    state.mark('context_hit_unavailable', stale=hit.line_number in rows or not failed)
                continue
            start, end = max(1, hit.line_number - request.context), hit.line_number + request.context
            context_rows = []
            for number, line in rows.items():
                if start <= number <= end:
                    if not _retain_search_bytes(len(line.encode('utf-8')) + 64):
                        break
                    context_rows.append((number, line))
            result[position] = replace(hit, context_lines=tuple(context_rows))
    _validate_search_sources(tool)
    return result


# LLM: 仅终止本调用创建的rg；TERM/KILL后wait均有界，kill成功后补wait以reap，不从取消回调关闭stdout。
# 函数用途: 尽快结束搜索子进程并回收僵尸；管道留给主读线程finally关闭。
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
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        process.wait(timeout=1)
