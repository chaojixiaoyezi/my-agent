
from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ._filesystem_helpers import (
    _bool_param,
    _discovery_result_envelope,
    _ignored_discovery_fallback_notice,
    _int_param,
    _internal_agent_status_ref,
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


class ListFilesTool(FileSystemTool):

    def __init__(
        self,
        workspace_root: Path,
        max_entries: int,
        workspace_roots: list[Path] | None = None,
        access_options: FileSystemAccessOptions | None = None,
    ):
        super().__init__(
            workspace_root,
            workspace_roots,
            access_options,
        )
        self.max_entries = max_entries
        self.spec = build_list_files_spec()

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            request = _list_files_request_from_params(params, self.max_entries)
            target = self.resolve_path(request.raw_path)
        except ValueError as exc:
            # 参数/路径解析失败是"改参数可修"，必须带 TOOL_INVALID_ARGUMENTS；
            # 漏传 error_code 会被 ToolExecutionResult 兜底成 UNKNOWN_ERROR(retryable=False)，
            # 误导模型"放弃报阻塞"而非按 schema 改参后重试。
            return ToolExecutionResult("list_files", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")
        if not target.exists():
            return missing_path_result(MissingPathRequest(
                tool_name="list_files",
                raw_path=request.raw_path,
                target=target,
                workspace_roots=self.workspace_roots,
                display_path=self.display_path(target),
                expected_kind="any",
                retry_tool="list_files",
            ))
        internal_ref = _internal_agent_status_ref(target, include_agent_directory=True)
        if internal_ref:
            return ToolExecutionResult(
                "list_files",
                False,
                json.dumps(internal_ref, ensure_ascii=False, indent=2),
                error_code="WRONG_STATUS_SURFACE",
            )
        if target.is_file():
            return ToolExecutionResult("list_files", True, self.display_path(target))
        return self._list_target(target, request)

    def _list_target(self, target: Path, request: _ListFilesRequest) -> ToolExecutionResult:
        collected = _collect_list_entries(self, target, request)
        included_ignored_fallback = False
        if (
            not collected.entries
            and collected.seen == 0
            and bool(request.file_glob)
            and not request.include_ignored
            and not request.include_ignored_explicit
        ):
            fallback_request = replace(request, include_ignored=True)
            collected = _collect_list_entries(self, target, fallback_request)
            included_ignored_fallback = bool(collected.entries)
        entries = list(collected.entries)
        if included_ignored_fallback:
            entries.insert(0, _ignored_discovery_fallback_notice())
        return ToolExecutionResult(
            "list_files",
            True,
            "\n".join(entries) or "目录为空",
            result_envelope=_discovery_result_envelope(
                _offset_page_window(
                    _OffsetPageWindowRequest(
                        tool="list_files",
                        source_path=self.display_path(target),
                        offset=request.offset,
                        limit=request.limit,
                        returned=collected.returned,
                        has_more=collected.paged_notice_added,
                    )
                ),
                included_ignored_fallback=included_ignored_fallback,
            ),
        )

    def _list_item_visible(
        self,
        item: Path,
        *,
        root: Path,
        request: _ListFilesRequest,
    ) -> bool:
        if item.is_dir() and not request.include_dirs:
            return False
        if item.is_file() and not request.include_files:
            return False
        if request.recursive and request.max_depth:
            try:
                depth = len(item.relative_to(root).parts)
            except ValueError:
                return False
            if depth > request.max_depth:
                return False
        if _internal_agent_status_ref(item, include_agent_directory=True):
            return False
        if not request.file_glob:
            return True
        display = self.display_path(item)
        return fnmatch.fnmatch(item.name, request.file_glob) or fnmatch.fnmatch(display, request.file_glob)


@dataclass(frozen=True)
class _ListFilesRequest:
    raw_path: str
    recursive: bool
    limit: int
    offset: int
    max_depth: int
    file_glob: str
    include_dirs: bool
    include_files: bool
    include_ignored: bool
    include_ignored_explicit: bool


@dataclass(frozen=True)
class _CollectedListEntries:
    entries: list[str]
    seen: int
    returned: int
    paged_notice_added: bool


@dataclass(frozen=True)
class _OffsetPageWindowRequest:
    tool: str
    source_path: str
    offset: int
    limit: int
    returned: int
    has_more: bool


def _list_files_request_from_params(params: dict[str, Any], max_entries: int) -> _ListFilesRequest:
    return _ListFilesRequest(
        raw_path=_optional_path(params.get("path", "."), default="."),
        recursive=_bool_param(params.get("recursive", False), default=False),
        limit=min(
            _int_param(params.get("limit"), name="limit", default=max_entries, min_value=1),
            max_entries,
        ),
        offset=_int_param(params.get("offset"), name="offset", default=0, min_value=0),
        max_depth=_int_param(params.get("max_depth"), name="max_depth", default=0, min_value=0),
        file_glob=_text_param(
            params.get("file_glob", ""),
            name="file_glob",
            max_chars=200,
            allow_empty=True,
            strip=True,
        ),
        include_dirs=_bool_param(params.get("include_dirs", True), default=True),
        include_files=_bool_param(params.get("include_files", True), default=True),
        include_ignored=_bool_param(params.get("include_ignored", False), default=False),
        include_ignored_explicit="include_ignored" in params,
    )


def _collect_list_entries(
    tool: ListFilesTool,
    target: Path,
    request: _ListFilesRequest,
) -> _CollectedListEntries:
    iterator = _iter_list_candidates(
        target,
        recursive=request.recursive,
        include_ignored=request.include_ignored,
    )
    entries: list[str] = []
    seen = 0
    returned = 0
    paged_notice_added = False
    for item in iterator:
        if not tool._list_item_visible(item, root=target, request=request):
            continue
        if seen < request.offset:
            seen += 1
            continue
        if len(entries) >= request.limit:
            entries.append(
                f"... 已截断，next_offset={seen} limit={request.limit}；继续查看请再次调用 list_files 并传入 offset={seen}"
            )
            paged_notice_added = True
            break
        suffix = "/" if item.is_dir() else ""
        entries.append(tool.display_path(item) + suffix)
        returned += 1
        seen += 1
    if entries and len(entries) >= request.limit and not paged_notice_added:
        entries.append(
            f"... 本页已满，next_offset={seen} limit={request.limit}；如需确认还有没有结果，可继续传入 offset={seen}"
        )
    return _CollectedListEntries(entries, seen, returned, paged_notice_added)


def _iter_list_candidates(target: Path, *, recursive: bool, include_ignored: bool) -> list[Path]:
    if not recursive:
        return sorted(
            [item for item in target.iterdir() if include_ignored or item.name not in _COMMON_FILE_DISCOVERY_IGNORES],
            key=_path_sort_key,
        )
    candidates: list[Path] = []
    for root, dirnames, filenames in os.walk(target):
        if not include_ignored:
            dirnames[:] = [dirname for dirname in dirnames if dirname not in _COMMON_FILE_DISCOVERY_IGNORES]
        root_path = Path(root)
        candidates.extend(root_path / dirname for dirname in dirnames)
        candidates.extend(root_path / filename for filename in filenames)
    return sorted(candidates, key=_path_sort_key)


def _path_sort_key(path: Path) -> tuple[str, str]:
    text = path.as_posix()
    return (text.lower(), text)


def _offset_page_window(request: _OffsetPageWindowRequest) -> dict[str, int | bool | str]:
    next_offset = request.offset + request.returned if request.has_more else 0
    return {
        "kind": "offset_page",
        "tool": request.tool,
        "source_path": request.source_path,
        "offset": request.offset,
        "limit": request.limit,
        "returned": request.returned,
        "next_offset": next_offset,
        "complete": not request.has_more,
    }


def build_list_files_spec() -> ToolSpec:
    return ToolSpec(
        name="list_files",
        category="filesystem",
        effect="read_only",
        description="列出目录中的文件和子目录，适合先摸清项目结构。可直接列任意绝对路径，包括 workspace 外、用户在任务里指定的输入目录，无需 shell 或额外授权——不要为查看输入目录提 capability_request。",
        use_cases=[
            "刚接手一个项目，先看看目录树大概长什么样",
            "不知道文件放在哪，先按目录层级摸排",
        ],
        avoid_when=[
            "已经知道目标文件路径时，别用它兜圈子，直接 read_file 更快",
        ],
        keywords=["目录", "文件树", "结构", "项目结构", "列文件", "list", "tree"],
        parameters={
            "path": "要查看的目录，默认是工作区根目录",
            "recursive": "是否递归展开子目录，默认 false",
            "limit": "本次最多返回多少条，默认使用工具配置上限",
            "offset": "从第几条开始返回，用于分页，默认 0",
            "max_depth": "递归时最多展开几层，默认不额外限制",
            "file_glob": "按 glob 过滤文件/目录名，例如 *.py",
            "include_dirs": "是否包含目录，默认 true",
            "include_files": "是否包含文件，默认 true",
            "include_ignored": "是否包含常见噪声目录。宽泛列表默认 false；显式 file_glob 零命中时会自动检查忽略目录，显式 false 可禁用该回退",
        },
        parameter_details=_list_files_parameter_details(),
        parameter_schema={
            "recursive": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1},
            "offset": {"type": "integer", "minimum": 0},
            "max_depth": {"type": "integer", "minimum": 0},
            "include_dirs": {"type": "boolean"},
            "include_files": {"type": "boolean"},
            "include_ignored": {"type": "boolean"},
        },
        examples=[
            '{"tool": "list_files", "path": "."}',
            '{"tool": "list_files", "path": "agent_py_agent/agent", "recursive": true, "limit": 50, "offset": 0}',
        ],
    )


def _list_files_parameter_details() -> dict[str, str]:
    return {
        "path": "相对工作区的目录路径；不传时默认从项目根目录开始列。",
        "recursive": "传 true 时会继续往下展开子目录；目录很大时要谨慎用，避免结果太长。",
        "limit": "分页大小；目录很多时先小批量查看，再用 next_offset 继续。",
        "offset": "上一页返回 next_offset 后，下一次传入这里继续看。",
        "max_depth": "只在 recursive=true 时生效；1 表示只看当前目录下一层。",
        "file_glob": "按工作区相对路径或文件名匹配；例如 *.py、src/*.ts。",
        "include_dirs": "false 时只返回文件。",
        "include_files": "false 时只返回目录。",
        "include_ignored": "宽泛列表默认跳过 .git、node_modules 和常见缓存目录。未传该参数且显式 file_glob 在可见文件中零命中时，会自动检查忽略目录；传 false 可强制排除，传 true 可始终包含。",
    }


__all__ = ["ListFilesTool"]
