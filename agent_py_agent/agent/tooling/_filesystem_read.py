
# LLM: 路径解析必须持续限制在工作区根内，避免读越界。
# 模块用途: 工作区内文件列举、读取和文本搜索工具实现。

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..path_recovery_hints import suggest_workspace_typo_target
from ._filesystem_helpers import (
    _bool_param,
    _bundled_filesystem_param,
    _int_param,
    _is_under_any_root,
    _normalized_workspace_roots,
    _optional_path,
    _required_path,
    _text_param,
)
from .filesystem_artifact_guard import tool_output_artifact_typo_hint
from .filesystem_read_file import execute_read_file
from .models import BaseTool, ToolExecutionResult, ToolSpec


# LLM: FileSystemTool 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: FileSystemTool 数据模型，集中保存 工具系统 的结构化状态。
class FileSystemTool(BaseTool):

    # LLM: FileSystemTool.__init__ 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 初始化 FileSystemTool 的依赖、配置和运行期字段。
    def __init__(self, workspace_root: Path, workspace_roots: list[Path] | None = None):
        self.workspace_root = workspace_root.resolve()
        self.workspace_roots = _normalized_workspace_roots(self.workspace_root, workspace_roots)

    # LLM: FileSystemTool.resolve_path 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 解析 resolve_path 并确认结果仍在允许边界内。
    def resolve_path(self, raw_path: str | Path) -> Path:

        raw_text = _required_path(raw_path)
        candidate = Path(raw_text)
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        try:
            candidate = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError("路径解析失败，请检查路径是否有效。") from exc
        if _is_under_any_root(candidate, self.workspace_roots):
            return candidate
        try:
            candidate.relative_to(self.workspace_root)
        except ValueError as exc:
            hint = _workspace_typo_error(raw_text, self.workspace_root, self.workspace_roots)
            if hint:
                raise ValueError(hint) from exc
            raise ValueError("路径超出允许的工作区范围，请使用工作区内路径。") from exc
        return candidate

    # LLM: FileSystemTool.display_path 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把内部路径转换成调用方可读的展示路径。
    def display_path(self, path: Path) -> str:

        for root in self.workspace_roots:
            if root == self.workspace_root:
                continue
            try:
                path.relative_to(root)
            except ValueError:
                continue
            return str(path).replace("\\", "/")
        try:
            return str(path.relative_to(self.workspace_root)).replace("\\", "/")
        except ValueError:
            return "<outside-workspace>"


# LLM: _workspace_typo_error gives models a precise retry path for near-miss workspace paths.
# 函数用途: 当读/列文件路径只是工作区前缀拼错时，返回机器可读的 suggested_target 提示。
def _workspace_typo_error(raw_path: str, workspace_root: Path, workspace_roots: list[Path]) -> str:
    suggested = suggest_workspace_typo_target(raw_path, workspace_roots)
    if not suggested:
        return ""
    artifact_hint = tool_output_artifact_typo_hint(raw_path, workspace_root, suggested)
    if artifact_hint:
        return artifact_hint
    return (
        "路径疑似拼写错误，已拒绝访问。"
        f" suspected_path_typo=true target={raw_path} workspace_root={workspace_root}"
        f" suggested_target={suggested}。"
        " 这是路径拼写错误，不是权限缺口；请使用 suggested_target 重试。"
    )


# LLM: ListFilesTool 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: ListFilesTool 数据模型，集中保存 工具系统 的结构化状态。
class ListFilesTool(FileSystemTool):

    # LLM: ListFilesTool.__init__ 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 初始化 ListFilesTool 的依赖、配置和运行期字段。
    def __init__(self, workspace_root: Path, max_entries: int, workspace_roots: list[Path] | None = None):
        super().__init__(workspace_root, workspace_roots)
        self.max_entries = max_entries
        self.spec = ToolSpec(
            name="list_files",
            category="filesystem",
            description="列出目录中的文件和子目录，适合先摸清项目结构。",
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
            },
            parameter_details={
                "path": "相对工作区的目录路径；不传时默认从项目根目录开始列。",
                "recursive": "传 true 时会继续往下展开子目录；目录很大时要谨慎用，避免结果太长。",
                "limit": "分页大小；目录很多时先小批量查看，再用 next_offset 继续。",
                "offset": "上一页返回 next_offset 后，下一次传入这里继续看。",
                "max_depth": "只在 recursive=true 时生效；1 表示只看当前目录下一层。",
                "file_glob": "按工作区相对路径或文件名匹配；例如 *.py、src/*.ts。",
                "include_dirs": "false 时只返回文件。",
                "include_files": "false 时只返回目录。",
            },
            examples=[
                '{"tool": "list_files", "path": "."}',
                '{"tool": "list_files", "path": "agent_py_agent/agent", "recursive": true, "limit": 50, "offset": 0}',
            ],
        )

    # LLM: ListFilesTool.execute 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 执行 ListFilesTool 的主流程并返回 ToolExecutionResult。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            request = _list_files_request_from_params(params, self.max_entries)
            target = self.resolve_path(request.raw_path)
        except ValueError as exc:
            return ToolExecutionResult("list_files", False, str(exc))
        if not target.exists():
            return ToolExecutionResult("list_files", False, f"路径不存在: {self.display_path(target)}")
        if target.is_file():
            return ToolExecutionResult("list_files", True, self.display_path(target))
        return self._list_target(target, request)

    # LLM: ListFilesTool._list_target keeps execute focused on validation and path safety.
    # 函数用途: 遍历目标目录，按分页和过滤参数生成 list_files 输出。
    def _list_target(self, target: Path, request: _ListFilesRequest) -> ToolExecutionResult:
        iterator = target.rglob("*") if request.recursive else target.iterdir()
        entries: list[str] = []
        seen = 0
        paged_notice_added = False
        for item in iterator:
            if not self._list_item_visible(item, root=target, request=request):
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
            entries.append(self.display_path(item) + suffix)
            seen += 1
        if entries and len(entries) >= request.limit and not paged_notice_added:
            entries.append(
                f"... 本页已满，next_offset={seen} limit={request.limit}；如需确认还有没有结果，可继续传入 offset={seen}"
            )
        return ToolExecutionResult("list_files", True, "\n".join(entries) or "目录为空")

    # LLM: _list_item_visible applies paging filters without changing workspace safety checks.
    # 函数用途: 根据 depth、glob 和文件/目录开关判断 list_files 是否返回某个条目。
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
        if not request.file_glob:
            return True
        display = self.display_path(item)
        return fnmatch.fnmatch(item.name, request.file_glob) or fnmatch.fnmatch(display, request.file_glob)


# LLM: _ListFilesRequest bundles list_files filters so paging can expand without long signatures.
# 类用途: 保存 list_files 的路径、分页、递归和过滤参数。
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


# LLM: _list_files_request_from_params validates model JSON before any directory traversal.
# 函数用途: 从 list_files 工具参数中解析长期可配置的分页和过滤参数。
def _list_files_request_from_params(params: dict[str, Any], max_entries: int) -> _ListFilesRequest:
    return _ListFilesRequest(
        raw_path=_optional_path(_bundled_filesystem_param(params, "path", "."), default="."),
        recursive=_bool_param(_bundled_filesystem_param(params, "recursive", False), default=False),
        limit=min(
            _int_param(_bundled_filesystem_param(params, "limit"), name="limit", default=max_entries, min_value=1),
            max_entries,
        ),
        offset=_int_param(_bundled_filesystem_param(params, "offset"), name="offset", default=0, min_value=0),
        max_depth=_int_param(_bundled_filesystem_param(params, "max_depth"), name="max_depth", default=0, min_value=0),
        file_glob=_text_param(
            _bundled_filesystem_param(params, "file_glob", ""),
            name="file_glob",
            max_chars=200,
            allow_empty=True,
            strip=True,
        ),
        include_dirs=_bool_param(_bundled_filesystem_param(params, "include_dirs", True), default=True),
        include_files=_bool_param(_bundled_filesystem_param(params, "include_files", True), default=True),
    )


# LLM: ReadFileTool 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: ReadFileTool 数据模型，集中保存 工具系统 的结构化状态。
class ReadFileTool(FileSystemTool):

    # LLM: ReadFileTool.__init__ 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 初始化 ReadFileTool 的依赖、配置和运行期字段。
    def __init__(self, workspace_root: Path, max_chars: int, workspace_roots: list[Path] | None = None):
        super().__init__(workspace_root, workspace_roots)
        self.max_chars = max_chars
        self.spec = ToolSpec(
            name="read_file",
            category="filesystem",
            description="读取文本文件内容，适合看代码、配置和文档。",
            use_cases=[
                "查看某个 Python 文件、配置文件或 Markdown 文档",
                "定位报错后，按行阅读相关代码",
            ],
            avoid_when=[
                "只想知道关键字在哪些文件出现过时，先用 search_text 更省",
            ],
            keywords=["读文件", "查看文件", "代码", "配置", "文档", "cat", "open file"],
            parameters={
                "path": "要读取的文件路径",
                "start_line": "起始行号，可选",
                "end_line": "结束行号，可选",
            },
            parameter_details={
                "path": "相对工作区的文本文件路径，必须是文件而不是目录。",
                "start_line": "从第几行开始读，默认从第 1 行开始。",
                "end_line": "读到第几行结束，包含该行；不传时默认读到文件结尾。",
            },
            examples=[
                '{"tool": "read_file", "path": "agent_py_agent/agent/core.py"}',
                '{"tool": "read_file", "path": "agent_py_agent/agent/core.py", "start_line": 1, "end_line": 120}',
            ],
        )

    # LLM: ReadFileTool.execute 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 执行 ReadFileTool 的主流程并返回 ToolExecutionResult。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        return execute_read_file(self, params, self.max_chars)
