# LLM: find_files is the dedicated file-discovery tool; keep it separate from directory listing and text search.
# 模块用途: 按 glob 查找工作区文件，默认跳过常见缓存/依赖目录，给模型稳定的文件导航结果。

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._filesystem_helpers import (
    _bool_param,
    _bundled_filesystem_param,
    _int_param,
    _optional_path,
    _text_param,
)
from ._filesystem_read import _COMMON_FILE_DISCOVERY_IGNORES, FileSystemTool
from .models import ToolExecutionResult, ToolSpec


# LLM: FindFilesTool is the dedicated glob-based file discovery tool.
# 类用途: 按 glob 查找文件路径，和 list_files/search_text 分工明确。
class FindFilesTool(FileSystemTool):

    # LLM: FindFilesTool.__init__ builds the model-facing tool metadata.
    # 函数用途: 初始化 find_files 的工作区范围、最大结果数和 ToolSpec。
    def __init__(self, workspace_root: Path, max_matches: int, workspace_roots: list[Path] | None = None):
        super().__init__(workspace_root, workspace_roots)
        self.max_matches = max_matches
        self.spec = ToolSpec(
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
                "include_ignored": "默认跳过 .git、node_modules 和常见缓存目录；确实要找这些目录里的文件时传 true。",
            },
            examples=[
                '{"tool": "find_files", "pattern": "**/*.py"}',
                '{"tool": "find_files", "pattern": "*.md", "path": "docs", "limit": 50}',
            ],
        )

    # LLM: FindFilesTool.execute validates params then dispatches file or directory search.
    # 函数用途: 执行 find_files 主流程并返回稳定 ToolExecutionResult。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            request = _find_files_request_from_params(params, self.max_matches)
            target = self.resolve_path(request.raw_path)
        except ValueError as exc:
            return ToolExecutionResult("find_files", False, str(exc))
        if not target.exists():
            return ToolExecutionResult("find_files", False, f"路径不存在: {self.display_path(target)}")
        if target.is_file():
            return self._find_in_single_file(target, request)
        return self._find_in_directory(target, request)

    # LLM: FindFilesTool._find_in_single_file handles the path-is-file case.
    # 函数用途: 当搜索范围本身是文件时，只判断该文件是否匹配 pattern。
    def _find_in_single_file(self, target: Path, request: _FindFilesRequest) -> ToolExecutionResult:
        if _matches_find_pattern(self.display_path(target), target.name, request.pattern):
            return ToolExecutionResult("find_files", True, self.display_path(target))
        return ToolExecutionResult("find_files", True, "没有找到匹配文件")

    # LLM: FindFilesTool._find_in_directory walks candidates with pagination.
    # 函数用途: 在目录内查找匹配文件，按 offset/limit 输出可继续翻页的结果。
    def _find_in_directory(self, target: Path, request: _FindFilesRequest) -> ToolExecutionResult:
        results: list[str] = []
        seen = 0
        limit_reached = False
        for item in _iter_find_candidates(target, include_ignored=request.include_ignored):
            rel = self.display_path(item)
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
        if not results:
            return ToolExecutionResult("find_files", True, "没有找到匹配文件")
        if limit_reached:
            results.append(
                f"... 已截断，next_offset={seen} limit={request.limit}；继续查找请再次调用 find_files 并传入 offset={seen}"
            )
        return ToolExecutionResult("find_files", True, "\n".join(results))


# LLM: _FindFilesRequest bundles validated find_files params.
# 类用途: 保存 find_files 的 glob、路径、分页和忽略目录开关。
@dataclass(frozen=True)
class _FindFilesRequest:
    pattern: str
    raw_path: str
    limit: int
    offset: int
    include_ignored: bool


# LLM: _find_files_request_from_params parses model JSON into a request object.
# 函数用途: 校验 pattern/path/limit/offset/include_ignored 参数。
def _find_files_request_from_params(params: dict[str, Any], max_matches: int) -> _FindFilesRequest:
    return _FindFilesRequest(
        pattern=_text_param(
            _bundled_filesystem_param(params, "pattern"),
            name="pattern",
            max_chars=300,
            strip=True,
        ),
        raw_path=_optional_path(_bundled_filesystem_param(params, "path", "."), default="."),
        limit=min(
            _int_param(_bundled_filesystem_param(params, "limit"), name="limit", default=max_matches, min_value=1),
            max_matches,
        ),
        offset=_int_param(_bundled_filesystem_param(params, "offset"), name="offset", default=0, min_value=0),
        include_ignored=_bool_param(_bundled_filesystem_param(params, "include_ignored", False), default=False),
    )


# LLM: _iter_find_candidates walks files in stable sorted order.
# 函数用途: 生成候选文件列表，默认跳过常见缓存和依赖目录。
def _iter_find_candidates(target: Path, *, include_ignored: bool) -> list[Path]:
    candidates: list[Path] = []
    for root, dirnames, filenames in os.walk(target):
        if not include_ignored:
            dirnames[:] = [dirname for dirname in dirnames if dirname not in _COMMON_FILE_DISCOVERY_IGNORES]
        root_path = Path(root)
        candidates.extend(root_path / filename for filename in filenames)
    return sorted(candidates, key=lambda path: (path.as_posix().lower(), path.as_posix()))


# LLM: _matches_find_pattern checks both relative path and basename.
# 函数用途: 让 glob 可匹配完整展示路径，也可只匹配文件名。
def _matches_find_pattern(display_path: str, basename: str, pattern: str) -> bool:
    return fnmatch.fnmatch(display_path, pattern) or fnmatch.fnmatch(basename, pattern)
