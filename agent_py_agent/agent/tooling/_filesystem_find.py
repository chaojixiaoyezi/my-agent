
from __future__ import annotations

import fnmatch
import os
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
from .filesystem_path_recovery import MissingPathRequest, missing_path_result
from .models import ToolExecutionResult, ToolSpec


def _build_find_files_spec() -> ToolSpec:
    return ToolSpec(
        name="find_files",
        category="filesystem",
        effect="read_only",
        description="按 glob 查找文件路径，适合不知道文件具体位置但知道文件名模式时使用。",
        use_cases=[
            "找所有 Python、Markdown、配置或测试文件",
            "按文件名模式定位候选文件，再用 read_file 阅读",
        ],
        avoid_when=[
            "只是想看某个目录下一层有什么时，用目录查看工具",
            "想搜索文件正文内容时，用正文搜索工具",
        ],
        keywords=["find", "glob", "文件查找", "按模式找文件", "找文件", "文件名"],
        parameters={
            "pattern": "glob 模式，例如 *.py、**/*.md、src/**/*.ts",
            "path": "从哪个目录开始找，默认工作区根目录",
            "limit": "本次最多返回多少个文件，默认使用工具配置上限",
            "offset": "跳过前多少个结果，用于分页，默认 0",
            "include_ignored": "是否包含常见噪声目录，如 .git/node_modules，默认 false",
        },
        parameter_details={
            "pattern": "必填；匹配工作区相对路径或文件名。不是正文搜索，不会打开文件内容。",
            "path": "可选；把范围缩小到某个目录会更快。",
            "limit": "分页大小；结果很多时先看一小页，再用 next_offset 继续。",
            "offset": "上一页返回 next_offset 后，下一次传入这里继续看。",
            "include_ignored": "宽泛查找默认跳过 .git、node_modules 和常见缓存目录。未传该参数且显式 glob 在可见文件中零命中时，会自动检查忽略目录；传 false 可强制排除，传 true 可始终包含。",
        },
        parameter_schema={
            "limit": {"type": "integer", "minimum": 1},
            "offset": {"type": "integer", "minimum": 0},
            "include_ignored": {"type": "boolean"},
        },
        required_parameters=["pattern"],
        examples=[
            '{"tool": "find_files", "pattern": "**/*.py"}',
            '{"tool": "find_files", "pattern": "*.md", "path": "docs", "limit": 50}',
        ],
        promotes_task=True,
    )


class FindFilesTool(FileSystemTool):

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
        self.spec = _build_find_files_spec()

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            request = _find_files_request_from_params(params, self.max_matches)
            target = self.resolve_path(request.raw_path)
        except ValueError as exc:
            # 参数/路径解析失败→TOOL_INVALID_ARGUMENTS(改参可修)；漏码会兜底 UNKNOWN_ERROR
            # (retryable=False)误导模型放弃而非按 schema 改参重试。
            return ToolExecutionResult("find_files", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")
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

    def _find_in_single_file(self, target: Path, request: _FindFilesRequest) -> ToolExecutionResult:
        if _matches_find_pattern(self.display_path(target), target.name, request.pattern):
            return ToolExecutionResult(
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
        return ToolExecutionResult(
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

    def _find_in_directory(self, target: Path, request: _FindFilesRequest) -> ToolExecutionResult:
        results, seen, limit_reached = _find_directory_matches(self, target, request)
        included_ignored_fallback = False
        if (
            not results
            and seen == 0
            and not request.include_ignored
            and not request.include_ignored_explicit
        ):
            fallback_request = replace(request, include_ignored=True)
            results, seen, limit_reached = _find_directory_matches(
                self,
                target,
                fallback_request,
            )
            included_ignored_fallback = bool(results)
        return _find_directory_result(
            _FindDirectoryResultRequest(
                self,
                target,
                request,
                results,
                seen,
                limit_reached,
                included_ignored_fallback,
            )
        )


@dataclass(frozen=True)
class _FindDirectoryResultRequest:
    tool: FindFilesTool
    target: Path
    request: _FindFilesRequest
    results: list[str]
    seen: int
    limit_reached: bool
    included_ignored_fallback: bool


@dataclass(frozen=True)
class _OffsetPageWindowRequest:
    source_path: str
    offset: int
    limit: int
    returned: int
    has_more: bool


def _find_directory_matches(
    tool: FindFilesTool,
    target: Path,
    request: _FindFilesRequest,
) -> tuple[list[str], int, bool]:
    results: list[str] = []
    seen = 0
    limit_reached = False
    for item in _iter_find_candidates(target, include_ignored=request.include_ignored):
        rel = tool.display_path(item)
        if not _matches_find_pattern(rel, item.name, request.pattern):
            continue
        if seen < request.offset:
            seen += 1
            continue
        if len(results) >= request.limit:
            limit_reached = True
            break
        results.append(rel)
        seen += 1
    return results, seen, limit_reached


def _find_directory_result(result: _FindDirectoryResultRequest) -> ToolExecutionResult:
    if not result.results:
        return _find_directory_empty_result(result.tool, result.target, result.request)
    if result.limit_reached:
        result.results.append(
            f"... 已截断，next_offset={result.seen} limit={result.request.limit}；继续查找请再次调用 find_files 并传入 offset={result.seen}"
        )
    output = "\n".join(result.results)
    if result.included_ignored_fallback:
        output = f"{_ignored_discovery_fallback_notice()}\n{output}"
    return ToolExecutionResult(
        "find_files",
        True,
        output,
        result_envelope=_discovery_result_envelope(
            _offset_page_window(
                _OffsetPageWindowRequest(
                    source_path=result.tool.display_path(result.target),
                    offset=result.request.offset,
                    limit=result.request.limit,
                    returned=result.seen - result.request.offset,
                    has_more=result.limit_reached,
                )
            ),
            included_ignored_fallback=result.included_ignored_fallback,
        ),
    )


def _find_directory_empty_result(tool: FindFilesTool, target: Path, request: _FindFilesRequest) -> ToolExecutionResult:
    return ToolExecutionResult(
        "find_files",
        True,
        "没有找到匹配文件",
        result_envelope={
            "page_window": _offset_page_window(
                _OffsetPageWindowRequest(
                    source_path=tool.display_path(target),
                    offset=request.offset,
                    limit=request.limit,
                    returned=0,
                    has_more=False,
                )
            )
        },
    )


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


def _iter_find_candidates(target: Path, *, include_ignored: bool) -> list[Path]:
    candidates: list[Path] = []
    for root, dirnames, filenames in os.walk(target):
        if not include_ignored:
            dirnames[:] = [dirname for dirname in dirnames if dirname not in _COMMON_FILE_DISCOVERY_IGNORES]
        root_path = Path(root)
        candidates.extend(root_path / filename for filename in filenames)
    return sorted(candidates, key=lambda path: (path.as_posix().lower(), path.as_posix()))


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
        "complete": not request.has_more,
    }
