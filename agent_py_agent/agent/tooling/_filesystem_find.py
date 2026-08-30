
from __future__ import annotations

import fnmatch
import logging
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ._filesystem_helpers import (
    _bool_param,
    _discovery_result_envelope,
    _ignored_discovery_fallback_notice,
    _int_param,
    _optional_path,
    _text_param,
)
from ._filesystem_read import (
    _COMMON_FILE_DISCOVERY_IGNORES,
    FileSystemAccessOptions,
    FileSystemTool,
)
from .cancellation import raise_if_cancelled, register_cancellation_callback
from .filesystem_path_recovery import MissingPathRequest, missing_path_result
from .models import (
    ConcurrencyPolicy,
    EffectResolverPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

_LOGGER = logging.getLogger(__name__)
_FALLBACK_SCAN_MAX_FILES = 20_000
_FALLBACK_SCAN_MAX_SECONDS = 10.0


def _build_find_files_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name="find_files",
        description="按 glob 查找文件路径，适合不知道文件具体位置但知道文件名模式时使用。",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "必填；glob 模式，例如 *.py、**/*.md、src/**/*.ts。匹配路径或文件名，不会打开正文。",
                },
                "path": {
                    "type": "string",
                    "description": "可选；从哪个目录开始找，默认工作区根目录。缩小范围会更快。",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "分页大小；结果很多时先看一小页，再用 next_offset 继续。",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "上一页返回 next_offset 后，下一次传入这里继续看。",
                },
                "include_ignored": {
                    "type": "boolean",
                    "description": "是否包含 .git、node_modules 和常见缓存目录；默认 false。",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="filesystem",
            use_cases=(
                "找所有 Python、Markdown、配置或测试文件",
                "按文件名模式定位候选文件，再用 read_file 阅读",
            ),
            avoid_when=(
                "只是想看某个目录下一层有什么时，用目录查看工具",
                "想搜索文件正文内容时，用正文搜索工具",
            ),
            keywords=("find", "glob", "文件查找", "按模式找文件", "找文件", "文件名"),
            examples=(
                '{"tool": "find_files", "pattern": "**/*.py"}',
                '{"tool": "find_files", "pattern": "*.md", "path": "docs", "limit": 50}',
            ),
        ),
    )


class FindFilesTool(FileSystemTool):

    model_spec = _build_find_files_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(parameter_names=("path",)),
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
            request = _find_files_request_from_params(params, self.max_matches)
            target = self.resolve_path(request.raw_path)
        except ValueError as exc:
            # 参数/路径解析失败→TOOL_INVALID_ARGUMENTS(改参可修)；漏码会兜底 UNKNOWN_ERROR
            # (retryable=False)误导模型放弃而非按 schema 改参重试。
            return ToolHandlerOutcome("find_files", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")
        if not target.exists():
            # 目标路径不存在是状态问题，应带 PATH_NOT_FOUND + candidate_paths(与 list/search 一致)，
            # 而非无码兜底 UNKNOWN_ERROR——后者让模型放弃，前者引导改用候选路径或先 list_files 定位。
            return missing_path_result(MissingPathRequest(
                tool_name="find_files",
                raw_path=request.raw_path,
                target=target,
                workspace_roots=self.workspace_roots,
                display_path=self.display_path(target),
                expected_kind="any",
                retry_tool="find_files",
            ))
        if target.is_file():
            return self._find_in_single_file(target, request)
        return self._find_in_directory(target, request)

    def _find_in_single_file(self, target: Path, request: _FindFilesRequest) -> ToolHandlerOutcome:
        if _matches_find_pattern(self.display_path(target), target.name, request.pattern):
            return ToolHandlerOutcome(
                "find_files",
                True,
                self.display_path(target),
                result_envelope={
                    "page_window": _offset_page_window(
                        _OffsetPageWindowRequest(
                            source_path=self.display_path(target),
                            offset=request.offset,
                            limit=request.limit,
                            returned=1,
                            has_more=False,
                        )
                    )
                },
            )
        return ToolHandlerOutcome(
            "find_files",
            True,
            "没有找到匹配文件",
            result_envelope={
                "page_window": _offset_page_window(
                    _OffsetPageWindowRequest(
                        source_path=self.display_path(target),
                        offset=request.offset,
                        limit=request.limit,
                        returned=0,
                        has_more=False,
                    )
                )
            },
        )

    def _find_in_directory(self, target: Path, request: _FindFilesRequest) -> ToolHandlerOutcome:
        batch = _find_directory_matches(self, target, request)
        included_ignored_fallback = False
        if (
            not batch.results
            and batch.seen == 0
            and batch.scan_complete
            and not request.include_ignored
            and not request.include_ignored_explicit
        ):
            fallback_request = replace(request, include_ignored=True)
            batch = _find_directory_matches(
                self,
                target,
                fallback_request,
            )
            included_ignored_fallback = bool(batch.results)
        return _find_directory_result(
            _FindDirectoryResultRequest(
                self,
                target,
                request,
                batch,
                included_ignored_fallback,
            )
        )


@dataclass(frozen=True)
class _FindDirectoryResultRequest:
    tool: FindFilesTool
    target: Path
    request: _FindFilesRequest
    batch: _FindMatchBatch
    included_ignored_fallback: bool


@dataclass(frozen=True)
class _OffsetPageWindowRequest:
    source_path: str
    offset: int
    limit: int
    returned: int
    has_more: bool
    scan_complete: bool = True


# LLM: This is one bounded file-discovery execution result. scan_complete is
# execution coverage, not a claim about repository or task completeness.
# 类用途: 保存文件查找的一页结果、后端和扫描边界，避免把受限空结果误报成“没有文件”。
@dataclass(frozen=True)
class _FindMatchBatch:
    results: list[str]
    seen: int
    has_more: bool
    scan_complete: bool
    backend: str
    scanned_files: int
    scanned_files_known: bool
    limit_reason: str = ""


# LLM: The Python fallback is deliberately bounded; hitting the boundary must
# be projected as incomplete and must never be converted into a no-match fact.
# 类用途: 记录没有 rg 时已经扫描多少文件，并在目录过大时及时停下而不是卡住 Gateway。
@dataclass
class _FindScanState:
    started_at: float
    candidates: int = 0
    limited: bool = False
    limit_reason: str = ""


def _find_directory_matches(
    tool: FindFilesTool,
    target: Path,
    request: _FindFilesRequest,
) -> _FindMatchBatch:
    rg_batch = _find_directory_matches_with_rg(tool, target, request)
    if rg_batch is not None:
        return rg_batch
    return _find_directory_matches_with_python(tool, target, request)


# LLM: The fallback walk streams candidates in a stable per-directory order,
# stops after one page sentinel, and reports objective liveness limits.
# 函数用途: 在系统没有 rg 时逐个遍历文件；够一页就返回，避免先把整个用户目录装进内存再排序。
def _find_directory_matches_with_python(
    tool: FindFilesTool,
    target: Path,
    request: _FindFilesRequest,
) -> _FindMatchBatch:
    results: list[str] = []
    seen = 0
    has_more = False
    scan_state = _FindScanState(started_at=time.monotonic())
    for item in _iter_find_candidates(
        target,
        include_ignored=request.include_ignored,
        scan_state=scan_state,
    ):
        rel = tool.display_path(item)
        if not _matches_find_pattern(rel, item.name, request.pattern):
            continue
        if seen < request.offset:
            seen += 1
            continue
        if len(results) >= request.limit:
            has_more = True
            break
        results.append(rel)
        seen += 1
    return _FindMatchBatch(
        results=results,
        seen=seen,
        has_more=has_more,
        scan_complete=not has_more and not scan_state.limited,
        backend="python",
        scanned_files=scan_state.candidates,
        scanned_files_known=True,
        limit_reason=scan_state.limit_reason,
    )


# LLM: Ripgrep is the primary discovery backend, matching 会话运行时/终端交互's
# bounded native file-search path while keeping the Python fallback portable.
# 函数用途: 用 rg 快速查大目录，只读取当前分页和一条“还有更多”的哨兵结果。
def _find_directory_matches_with_rg(
    tool: FindFilesTool,
    target: Path,
    request: _FindFilesRequest,
) -> _FindMatchBatch | None:
    process = _start_rg_find_process(target, request)
    if process is None:
        return None
    with register_cancellation_callback(lambda: _stop_find_process(process)):
        return _read_rg_find_process(tool, target, request, process)


# LLM: The subprocess receives only typed path/glob arguments and never a shell
# command. Its lifetime is owned by the current tool cancellation token.
# 函数用途: 启动不经过 shell 的 rg 文件枚举进程；启动失败时让调用方走 Python 后备。
def _start_rg_find_process(
    target: Path,
    request: _FindFilesRequest,
) -> subprocess.Popen[str] | None:
    rg_path = shutil.which("rg")
    if not rg_path:
        return None
    args = [
        rg_path,
        "--files",
        "--hidden",
        "--no-ignore",
        "--no-config",
        "--no-messages",
        "--no-follow",
        "--glob",
        request.pattern,
    ]
    if not request.include_ignored:
        for ignored in sorted(_COMMON_FILE_DISCOVERY_IGNORES):
            args.extend(["--glob", f"!{ignored}/**", "--glob", f"!**/{ignored}/**"])
    args.extend(["--", "."])
    try:
        return subprocess.Popen(
            args,
            cwd=str(target),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        _LOGGER.debug("rg file discovery failed to start; using Python fallback", exc_info=True)
        return None


# LLM: Output is consumed only to offset+limit+1 and every path is resolved
# through the canonical access policy before it can reach the model.
# 函数用途: 读取 rg 的一页文件结果；看到下一页哨兵后立刻结束进程，并保持路径权限校验。
def _read_rg_find_process(
    tool: FindFilesTool,
    target: Path,
    request: _FindFilesRequest,
    process: subprocess.Popen[str],
) -> _FindMatchBatch | None:
    stdout = process.stdout
    if stdout is None:
        _stop_find_process(process)
        return None
    results: list[str] = []
    seen = 0
    has_more = False
    stopped_early = False
    try:
        for raw_line in stdout:
            raise_if_cancelled()
            raw_path = Path(raw_line.rstrip("\r\n"))
            if not raw_path.name:
                continue
            candidate = raw_path if raw_path.is_absolute() else target / raw_path
            if candidate.is_symlink():
                continue
            try:
                safe_candidate = tool.resolve_path(candidate)
            except ValueError:
                continue
            if seen < request.offset:
                seen += 1
                continue
            if len(results) >= request.limit:
                has_more = True
                stopped_early = True
                _stop_find_process(process)
                break
            results.append(tool.display_path(safe_candidate))
            seen += 1
        return_code = process.wait(timeout=2)
    except Exception:
        _LOGGER.debug("rg file discovery failed while reading; using Python fallback", exc_info=True)
        _stop_find_process(process)
        return None
    if not stopped_early and return_code not in {0, 1}:
        return None
    return _FindMatchBatch(
        results=results,
        seen=seen,
        has_more=has_more,
        scan_complete=not has_more,
        backend="rg",
        scanned_files=0,
        scanned_files_known=False,
    )


# LLM: Termination is idempotent and bounded so /stop cannot wait behind a
# blocked rg stdout reader.
# 函数用途: 结束当前文件查找子进程；普通完成时不做事，超时后再强制结束。
def _stop_find_process(process: subprocess.Popen[str]) -> None:
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


def _find_directory_result(result: _FindDirectoryResultRequest) -> ToolHandlerOutcome:
    if not result.batch.results:
        return _find_directory_empty_result(
            result.tool,
            result.target,
            result.request,
            result.batch,
        )
    output_rows = list(result.batch.results)
    if result.batch.has_more:
        output_rows.append(
            f"... 已截断，next_offset={result.batch.seen} limit={result.request.limit}；继续查找请再次调用 find_files 并传入 offset={result.batch.seen}"
        )
    output = "\n".join(output_rows)
    if result.included_ignored_fallback:
        output = f"{_ignored_discovery_fallback_notice()}\n{output}"
    envelope = _find_result_envelope(result)
    return ToolHandlerOutcome(
        "find_files",
        True,
        output,
        result_envelope=envelope,
    )


def _find_directory_empty_result(
    tool: FindFilesTool,
    target: Path,
    request: _FindFilesRequest,
    batch: _FindMatchBatch | None = None,
) -> ToolHandlerOutcome:
    batch = batch or _FindMatchBatch([], 0, False, True, "single", 1, True)
    result = _FindDirectoryResultRequest(tool, target, request, batch, False)
    output = "没有找到匹配文件"
    if not batch.scan_complete:
        output = (
            "文件查找未完整覆盖；请缩小 path 或使用更具体的 pattern 后重试，"
            "不能据此认定工作区没有匹配文件"
        )
    return ToolHandlerOutcome(
        "find_files",
        True,
        output,
        result_envelope=_find_result_envelope(result),
    )


# LLM: Result metadata distinguishes matches/no-match/incomplete execution and
# never promotes a liveness cutoff into a repository-level fact.
# 函数用途: 给文件查找结果附上分页、后端和扫描完整性，供模型决定翻页还是缩小范围。
def _find_result_envelope(result: _FindDirectoryResultRequest) -> dict[str, object]:
    batch = result.batch
    envelope = _discovery_result_envelope(
        _offset_page_window(
            _OffsetPageWindowRequest(
                source_path=result.tool.display_path(result.target),
                offset=result.request.offset,
                limit=result.request.limit,
                returned=len(batch.results),
                has_more=batch.has_more,
                scan_complete=batch.scan_complete,
            )
        ),
        included_ignored_fallback=result.included_ignored_fallback,
    )
    status = "matches" if batch.results else ("no_matches" if batch.scan_complete else "incomplete")
    envelope["file_search_result"] = {
        "schema": "local_file_search.v1",
        "scope": "local_workspace",
        "source_path": result.tool.display_path(result.target),
        "backend": batch.backend,
        "status": status,
        "scan_complete": batch.scan_complete,
        "scanned_files": batch.scanned_files,
        "scanned_files_known": batch.scanned_files_known,
        "scan_limit_reason": batch.limit_reason,
        "suggested_actions": ["narrow_path", "use_specific_pattern"] if status == "incomplete" else [],
    }
    return envelope


@dataclass(frozen=True)
class _FindFilesRequest:
    pattern: str
    raw_path: str
    limit: int
    offset: int
    include_ignored: bool
    include_ignored_explicit: bool


def _find_files_request_from_params(params: dict[str, Any], max_matches: int) -> _FindFilesRequest:
    return _FindFilesRequest(
        pattern=_text_param(
            params.get("pattern"),
            name="pattern",
            max_chars=300,
            strip=True,
        ),
        raw_path=_optional_path(params.get("path", "."), default="."),
        limit=min(
            _int_param(params.get("limit"), name="limit", default=max_matches, min_value=1),
            max_matches,
        ),
        offset=_int_param(params.get("offset"), name="offset", default=0, min_value=0),
        include_ignored=_bool_param(params.get("include_ignored", False), default=False),
        include_ignored_explicit="include_ignored" in params,
    )


# LLM: The portable fallback yields paths lazily and stops at objective file/time
# limits; callers must preserve the resulting incomplete marker.
# 函数用途: 在没有 rg 时按目录和文件名顺序逐个产出候选文件，不预先堆积整个目录树。
def _iter_find_candidates(
    target: Path,
    *,
    include_ignored: bool,
    scan_state: _FindScanState,
) -> Iterable[Path]:
    for root, dirnames, filenames in os.walk(target):
        raise_if_cancelled()
        dirnames.sort()
        filenames.sort()
        if not include_ignored:
            dirnames[:] = [dirname for dirname in dirnames if dirname not in _COMMON_FILE_DISCOVERY_IGNORES]
        root_path = Path(root)
        for filename in filenames:
            if not _admit_find_candidate(scan_state):
                return
            yield root_path / filename


# LLM: This liveness fence only bounds the Python fallback and is reported as
# incomplete; it never authorizes a no-match conclusion.
# 函数用途: 在后备文件遍历超过 2 万文件或 10 秒前停止，保护单 Gateway 不被大目录长期占住。
def _admit_find_candidate(state: _FindScanState) -> bool:
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


def _matches_find_pattern(display_path: str, basename: str, pattern: str) -> bool:
    return fnmatch.fnmatch(display_path, pattern) or fnmatch.fnmatch(basename, pattern)


def _offset_page_window(request: _OffsetPageWindowRequest) -> dict[str, int | bool | str]:
    return {
        "kind": "offset_page",
        "tool": "find_files",
        "source_path": request.source_path,
        "offset": request.offset,
        "limit": request.limit,
        "returned": request.returned,
        "next_offset": request.offset + request.returned if request.has_more else 0,
        "complete": not request.has_more and request.scan_complete,
    }
