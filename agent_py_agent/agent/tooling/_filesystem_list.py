
from __future__ import annotations

import fnmatch
import heapq
import json
import os
import stat
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NamedTuple

from ..common.cancellation import raise_if_cancelled
from ..path_access_policy import PATH_SCOPE_FULL, PATH_SCOPE_NORMAL, PATH_SCOPE_OWNER_WALL
from ._filesystem_helpers import (
    _FALLBACK_SCAN_MAX_SECONDS,
    _MAX_PATH_CHARS,
    _bool_param,
    _discovery_result_envelope,
    _ignored_discovery_fallback_notice,
    _int_param,
    _internal_agent_status_ref,
    _internal_agent_status_run_id,
    _optional_path,
    _safe_discovery_path,
    _text_param,
    path_resolution_error_outcome,
)
from ._filesystem_read import (
    _COMMON_FILE_DISCOVERY_IGNORES,
    UNWALLED_NORMAL_EXCEPTIONS_NOTE,
    FileSystemAccessOptions,
    FileSystemTool,
)
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

_LIST_FILES_DESCRIPTION_BASE = "列出目录中的文件和子目录，适合先摸清项目结构。"
_LIST_FILES_FULL_NOTE = (
    "可直接列任意绝对路径，包括 workspace 外、用户在任务里指定的输入目录，无需 shell 或额外授权——"
    "不要为查看输入目录提 capability_request。"
)
# 说明末句按本 run 的读取范围给出，与 read_file 同一句式、与 PathAccessPolicy.check 一一对应（full 句即原说明）。
_LIST_FILES_SCOPE_NOTES = {
    PATH_SCOPE_OWNER_WALL: (
        "只能列当前用户自己的数据目录、shared 公共区和本任务明确授权的外部工作目录；其它路径会在授权阶段被拒，"
        "返回 PATH_OWNER_SCOPE_BLOCKED 等 PATH_*_BLOCKED 错误码，换写法重试同一位置不会成功——需要墙外的文件时如实说明缺哪个文件。"
    ),
    PATH_SCOPE_FULL: _LIST_FILES_FULL_NOTE,
    PATH_SCOPE_NORMAL: _LIST_FILES_FULL_NOTE + UNWALLED_NORMAL_EXCEPTIONS_NOTE,
}


class ListFilesTool(FileSystemTool):

    path_scope_description_base = _LIST_FILES_DESCRIPTION_BASE
    path_scope_notes = _LIST_FILES_SCOPE_NOTES
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(parameter_names=("path",)),
        promotes_task=True,
    )

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
        self.model_spec = build_list_files_model_spec()

    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        try:
            request = _list_files_request_from_params(params, self.max_entries)
            target = self.resolve_path(request.raw_path)
        except ValueError as exc:
            # 参数/路径解析失败是"改参数可修"；路径策略拒绝则按原权限码上报并标 authorization，
            # 供"连续失败即停"统计识别，避免工具在无权路径上空转。
            return path_resolution_error_outcome("list_files", exc)
        if not target.exists():
            return missing_path_result(MissingPathRequest(
                tool_name="list_files",
                raw_path=request.raw_path,
                target=target,
                workspace_roots=self.workspace_roots,
                display_path=self.display_path(target),
                expected_kind="any",
                retry_tool="list_files",
            decide_access=self.check_path_access,
            ))
        internal_ref = _internal_agent_status_ref(target, include_agent_directory=True)
        if internal_ref:
            return ToolHandlerOutcome(
                "list_files",
                False,
                json.dumps(internal_ref, ensure_ascii=False, indent=2),
                error_code="WRONG_STATUS_SURFACE",
            )
        if target.is_file():
            return ToolHandlerOutcome("list_files", True, self.display_path(target))
        return self._list_target(target, request)

    def _list_target(self, target: Path, request: _ListFilesRequest) -> ToolHandlerOutcome:
        deadline = time.monotonic() + _FALLBACK_SCAN_MAX_SECONDS
        collected = _collect_list_entries(self, target, request, deadline=deadline)
        included_ignored_fallback = False
        if (
            not collected.entries
            and collected.seen == 0
            and bool(request.file_glob)
            and not request.include_ignored
            and not request.include_ignored_explicit
            and (not request.after_entry or _cursor_in_ignored_entries(target, request))
        ):
            fallback_request = replace(request, include_ignored=True)
            collected = _collect_list_entries(self, target, fallback_request, deadline=deadline)
            included_ignored_fallback = bool(collected.entries)
        lines = list(collected.entries)
        if included_ignored_fallback:
            lines.insert(0, _ignored_discovery_fallback_notice())
        if notice := _list_page_notice(request, collected):
            lines.append(notice)
        envelope = _discovery_result_envelope(
            _offset_page_window(
                _OffsetPageWindowRequest(
                    tool="list_files",
                    source_path=self.display_path(target),
                    offset=request.offset,
                    limit=request.limit,
                    returned=len(collected.entries),
                    has_more=collected.has_more,
                )
            ),
            included_ignored_fallback=included_ignored_fallback,
        )
        envelope.update(
            next_after_entry=collected.next_after_entry,
            scan_incomplete=collected.scan_incomplete,
        )
        return ToolHandlerOutcome(
            "list_files",
            True,
            "\n".join(lines) or "目录为空",
            result_envelope=envelope,
        )

    def _list_item_visible(
        self,
        item: Path,
        *,
        is_dir: bool,
        is_file: bool,
        request: _ListFilesRequest,
    ) -> bool:
        if (is_dir and not request.include_dirs) or (is_file and not request.include_files):
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
    after_entry: str = ""


@dataclass(frozen=True)
class _CollectedListEntries:
    entries: list[str]
    seen: int
    has_more: bool
    next_after_entry: str
    scan_incomplete: bool


@dataclass(frozen=True)
class _OffsetPageWindowRequest:
    tool: str
    source_path: str
    offset: int
    limit: int
    returned: int
    has_more: bool


def _list_files_request_from_params(params: dict[str, Any], max_entries: int) -> _ListFilesRequest:
    after_entry = params.get("after_entry", "")
    if (not isinstance(after_entry, str) or "\0" in after_entry or
            len(after_entry) > _MAX_PATH_CHARS or ".." in Path(after_entry).parts):
        raise ValueError(
            f"after_entry 须为不超过 {_MAX_PATH_CHARS} 字符且不含空字符或上级跳转的路径字符串。"
            "请复制上一页的 next_after_entry，或省略该字段。未扫描目录。"
        )
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
        after_entry=after_entry,
    )


def _cursor_in_ignored_entries(target: Path, request: _ListFilesRequest) -> bool:
    cursor = target / request.after_entry
    if not cursor.is_relative_to(target):
        return False
    parts = cursor.relative_to(target).parts
    # 递归模式只忽略目录名。返回的目录游标保留斜杠，区分同名的普通文件。
    if request.recursive and not request.after_entry.endswith("/"):
        parts = parts[:-1]
    return any(part in _COMMON_FILE_DISCOVERY_IGNORES for part in parts)


def _collect_list_entries(
    tool: ListFilesTool,
    target: Path,
    request: _ListFilesRequest,
    *,
    deadline: float,
) -> _CollectedListEntries:
    walk = _LazyListWalk(tool, target, request)
    entries: list[str] = []
    seen = 0
    last_item = None
    suffix = ""
    has_more = False
    # 限时只能在已交付至少一条之后生效，否则 offset 或稀疏 glob 无法用末条游标推进。
    while (found := walk.next_visible(deadline=deadline if entries else None)) is not None:
        item, is_dir = found
        if seen < request.offset:
            seen += 1
            continue
        if len(entries) >= request.limit:
            has_more = True
            break
        last_item = item
        suffix = "/" if is_dir else ""
        entries.append(tool.display_path(item) + suffix)
        seen += 1
    raise_if_cancelled()
    has_more = has_more or walk.scan_incomplete
    cursor = last_item.relative_to(target).as_posix() + suffix if has_more and last_item is not None else ""
    return _CollectedListEntries(entries, seen, has_more, cursor, walk.scan_incomplete)


def _list_page_notice(request: _ListFilesRequest, collected: _CollectedListEntries) -> str:
    seen = collected.seen
    if collected.scan_incomplete:
        notice = f"... 扫描达到时间预算，next_offset={seen} limit={request.limit}"
    elif collected.has_more:
        notice = f"... 已截断，next_offset={seen} limit={request.limit}；继续查看请再次调用 list_files 并传入 offset={seen}"
    elif len(collected.entries) >= request.limit:
        return f"... 本页已满，next_offset={seen} limit={request.limit}；如需确认还有没有结果，可继续传入 offset={seen}"
    else:
        return ""
    if not collected.next_after_entry:
        return notice
    cursor = json.dumps(collected.next_after_entry, ensure_ascii=False)
    return f"{notice}；next_after_entry={cursor}，可保持其余筛选并传入 after_entry={cursor} 继续，省略 offset。"


_ListKey = tuple[str, str]


def _path_sort_key(text: str) -> _ListKey:
    return (text.lower(), text)


class _ListWalkNode(NamedTuple):
    key: _ListKey
    scan_directory: str
    depth: int
    is_dir: bool = False
    is_symlink: bool = False
    is_file: bool = False
    resume_scan: bool = False
    resolved_directory: Path | None = None
    is_reparse_point: bool = False


class _LazyListWalk:
    """按原排序增量扫描，堆里只放已触及目录的有界批次和继续扫描的位置。"""

    # 每次目录选取最多保留 4096 项，用继续扫描标记推进超宽目录。
    _MAX_BATCH_ENTRIES = 4_096
    # 无序枚举和堆出队每 256 项检查一次取消。
    _CANCELLATION_CHECK_ENTRIES = 256

    def __init__(
        self,
        tool: FileSystemTool,
        target: Path,
        request: _ListFilesRequest,
        *,
        file_filter: Callable[[str, str], bool] | None = None,
    ):
        self._tool = tool
        self._target = target
        self._request = request
        self._file_filter = file_filter
        self._after_key = _path_sort_key((target / request.after_entry).as_posix()) if request.after_entry else None
        self._pending: list[_ListWalkNode] = []
        self._visited = 0
        self.scan_incomplete = False
        self._schedule_descent(target.as_posix(), depth=0)

    @classmethod
    def for_discovery(
        cls, tool: FileSystemTool, target: Path, *, include_ignored: bool,
        after_entry: str, file_filter: Callable[[str, str], bool],
    ) -> _LazyListWalk:
        request = _ListFilesRequest(
            ".", True, 0, 0, 0, "", False, True, include_ignored, True, after_entry,
        )
        return cls(tool, target, request, file_filter=file_filter)

    def _schedule_descent(self, directory: str, *, depth: int, resolved_directory: Path | None = None) -> None:
        # "/" 中断 Unicode 大小写上下文。每个后代的排序键都大于这个前缀，
        # 所以能按真实全局顺序延迟下钻，即使相邻目录仅大小写不同。
        prefix = directory.rstrip("/") + "/"
        heapq.heappush(
            self._pending,
            _ListWalkNode(_path_sort_key(prefix), directory, depth, resolved_directory=resolved_directory),
        )

    def _can_descend(self, depth: int) -> bool:
        return self._request.recursive and (not self._request.max_depth or depth < self._request.max_depth)

    def next_visible(self, *, deadline: float | None) -> tuple[Path, bool] | None:
        policy = self._tool.path_access_policy
        while self._pending:
            if deadline is not None and time.monotonic() >= deadline:
                self.scan_incomplete = True
                return None
            node = heapq.heappop(self._pending)
            if node.scan_directory:
                self._children(node)
                continue
            self._visited += 1
            if self._visited % self._CANCELLATION_CHECK_ENTRIES == 0:
                raise_if_cancelled()

            item = Path(node.key[1])
            run_id = _internal_agent_status_run_id(item, include_agent_directory=True)
            if run_id:
                continue
            if self._file_filter is not None:
                safe = _safe_discovery_path(
                    self._tool, item, root=self._target, include_ignored=self._request.include_ignored,
                    ignored_names=_COMMON_FILE_DISCOVERY_IGNORES,
                )
                if safe is not None:
                    if node.is_dir:
                        self._schedule_descent(node.key[1], depth=node.depth)
                    elif node.is_file and (self._after_key is None or node.key > self._after_key):
                        return item, False
                continue
            resolved = item if node.resolved_directory is None else node.resolved_directory / item.name
            if node.is_reparse_point:
                try:
                    resolved = resolved.resolve(strict=False)
                except (OSError, RuntimeError, ValueError):
                    continue
            if node.is_symlink:
                managed = policy.host_managed_store(item) is not None
            else:
                managed = policy.is_host_managed_store_resolved(resolved)
            if node.is_dir and self._can_descend(node.depth) and (
                not managed or policy.is_admin_readable_service_log_ancestor(resolved)
            ):
                self._schedule_descent(node.key[1], depth=node.depth, resolved_directory=resolved if resolved != item else None)
            if run_id is not None or managed or (self._after_key is not None and node.key <= self._after_key):
                continue
            is_dir = node.is_dir or (node.is_symlink and item.is_dir())
            is_file = node.is_file or (
                node.is_symlink and not is_dir and not self._request.include_files and item.is_file()
            )
            if self._tool._list_item_visible(item, is_dir=is_dir, is_file=is_file, request=self._request):
                return item, is_dir
        return None

    def _children(self, marker: _ListWalkNode) -> None:
        raise_if_cancelled()
        directory = marker.scan_directory
        # 递归前重查目录是否被替换为链接，与原来的 os.walk 不跟随链接一致。
        if marker.depth and os.path.islink(directory):
            return
        prefix = directory.rstrip("/") + "/"
        folded_prefix = prefix.lower()
        depth = marker.depth + 1
        can_descend = self._can_descend(depth)
        cursor = self._after_key
        batch_after = marker.key if marker.resume_scan else None
        accepted = 0

        def candidates(entries: Iterator[os.DirEntry[str]]) -> Iterator[_ListWalkNode]:
            nonlocal accepted
            for index, entry in enumerate(entries):
                if index % self._CANCELLATION_CHECK_ENTRIES == 0:
                    raise_if_cancelled()
                name = entry.name
                key = (folded_prefix + name.lower(), prefix + name)
                if batch_after is not None and key <= batch_after:
                    continue
                follows_cursor = cursor is None or key > cursor
                if not follows_cursor:
                    descent_prefix = key[0] + "/"
                    if not can_descend or (descent_prefix < cursor[0] and not cursor[0].startswith(descent_prefix)):
                        continue
                is_dir = entry.is_dir(follow_symlinks=False)
                if not follows_cursor and not is_dir:
                    continue
                if not self._request.include_ignored and name in _COMMON_FILE_DISCOVERY_IGNORES:
                    # 非递归原来按名称跳过，递归原来只剪掉目录（含指向目录的链接）。
                    if not self._request.recursive or is_dir or entry.is_dir():
                        continue
                is_symlink = entry.is_symlink()
                if self._file_filter is not None:
                    if is_symlink or (not is_dir and not self._file_filter(key[1], name)):
                        continue
                is_file = (
                    (self._file_filter is not None or not self._request.include_files)
                    and not is_symlink and not is_dir and entry.is_file(follow_symlinks=False)
                )
                # Windows 的目录联接点不一定是符号链接。读取 DirEntry 的重解析属性，
                # 只为这类条目解析真实路径，避免对子树按联接点的显示路径误判托管区。
                is_reparse_point = os.name == "nt" and not is_symlink and bool(
                    entry.stat(follow_symlinks=False).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                )
                if self._file_filter is not None and (is_reparse_point or not (is_dir or is_file)):
                    continue
                accepted += 1
                yield _ListWalkNode(
                    key, "", depth, is_dir, is_symlink, is_file,
                    resolved_directory=marker.resolved_directory, is_reparse_point=is_reparse_point,
                )

        try:
            with os.scandir(directory) as entries:
                batch = heapq.nsmallest(self._MAX_BATCH_ENTRIES, candidates(entries))
        except OSError:
            if not self._request.recursive or self._file_filter is not None:
                raise
            return  # 保留递归枚举跳过不可读或消失目录的行为。
        raise_if_cancelled()
        for child in batch:
            heapq.heappush(self._pending, child)
        if accepted > len(batch):
            # 同键的真实条目先出队。下一批只选位于该键之后的同层条目。
            heapq.heappush(self._pending, marker._replace(key=batch[-1].key, resume_scan=True))


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


def build_list_files_model_spec() -> ToolModelSpec:
    details = _list_files_parameter_details()
    return ToolModelSpec(
        name="list_files",
        description=_LIST_FILES_DESCRIPTION_BASE + _LIST_FILES_FULL_NOTE,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": details["path"]},
                "recursive": {"type": "boolean", "description": details["recursive"]},
                "limit": {"type": "integer", "minimum": 1, "description": details["limit"]},
                "offset": {"type": "integer", "minimum": 0, "description": details["offset"]},
                "after_entry": {"type": "string", "description": details["after_entry"]},
                "max_depth": {"type": "integer", "minimum": 0, "description": details["max_depth"]},
                "file_glob": {"type": "string", "description": details["file_glob"]},
                "include_dirs": {"type": "boolean", "description": details["include_dirs"]},
                "include_files": {"type": "boolean", "description": details["include_files"]},
                "include_ignored": {"type": "boolean", "description": details["include_ignored"]},
            },
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="filesystem",
            use_cases=(
                "刚接手一个项目，先看看目录树大概长什么样",
                "不知道文件放在哪，先按目录层级摸排",
            ),
            avoid_when=("已经知道目标文件路径时，别用它兜圈子，直接 read_file 更快",),
            keywords=("目录", "文件树", "结构", "项目结构", "列文件", "list", "tree"),
            examples=(
                '{"tool": "list_files", "path": "."}',
                '{"tool": "list_files", "path": "agent_py_agent/agent", "recursive": true, "limit": 50, "offset": 0}',
                '{"tool": "list_files", "path": ".", "recursive": true, "limit": 50, "after_entry": "src/main.py"}',
            ),
        ),
    )


def _list_files_parameter_details() -> dict[str, str]:
    return {
        "path": "相对工作区的目录路径；不传时默认从项目根目录开始列。",
        "recursive": "传 true 时会继续往下展开子目录；目录很大时要谨慎用，避免结果太长。",
        "limit": "分页大小。用回执的 next_after_entry 继续大目录的列表。",
        "offset": "跳过多少条匹配结果。提供 after_entry 时，从该游标之后开始计数。",
        "after_entry": (
            "从指定条目之后继续。保持 path 和筛选条件，复制回执的 next_after_entry；也接受相对于 path 的条目路径或绝对路径。"
            "例如 src/main.py。scan_incomplete=true 表示扫描达到时间预算，可用游标继续。"
        ),
        "max_depth": "只在 recursive=true 时生效；1 表示只看当前目录下一层。",
        "file_glob": "按工作区相对路径或文件名匹配；例如 *.py、src/*.ts。",
        "include_dirs": "false 时只返回文件。",
        "include_files": "false 时只返回目录。",
        "include_ignored": "宽泛列表默认跳过 .git、node_modules 和常见缓存目录。未传该参数且显式 file_glob 在可见文件中零命中时，会自动检查忽略目录；传 false 可强制排除，传 true 可始终包含。",
    }


__all__ = ["ListFilesTool"]
