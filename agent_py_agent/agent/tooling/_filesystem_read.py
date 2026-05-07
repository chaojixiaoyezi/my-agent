
# LLM: 路径解析必须持续限制在工作区根内，避免读越界。
# 模块用途: 工作区内文件列举、读取和文本搜索工具实现。

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._filesystem_helpers import (
    _MAX_SEARCH_LINE_CHARS,
    _MAX_SEARCH_QUERY_CHARS,
    _bool_param,
    _int_param,
    _optional_path,
    _required_path,
    _text_param,
)
from .models import BaseTool, ToolExecutionResult, ToolSpec

_MAX_WRITE_TEXT_CHARS = 1_000_000


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
            },
            parameter_details={
                "path": "相对工作区的目录路径；不传时默认从项目根目录开始列。",
                "recursive": "传 true 时会继续往下展开子目录；目录很大时要谨慎用，避免结果太长。",
            },
            examples=[
                '{"tool": "list_files", "path": "."}',
                '{"tool": "list_files", "path": "agent_py_agent/agent", "recursive": true}',
            ],
        )

    # LLM: ListFilesTool.execute 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 执行 ListFilesTool 的主流程并返回 ToolExecutionResult。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            raw_path = _optional_path(params.get("path"), default=".")
            recursive = _bool_param(params.get("recursive"), default=False)
            target = self.resolve_path(raw_path)
        except ValueError as exc:
            return ToolExecutionResult("list_files", False, str(exc))
        if not target.exists():
            return ToolExecutionResult("list_files", False, f"路径不存在: {self.display_path(target)}")
        if target.is_file():
            return ToolExecutionResult("list_files", True, self.display_path(target))

        iterator = target.rglob("*") if recursive else target.iterdir()
        entries: list[str] = []
        for item in iterator:
            suffix = "/" if item.is_dir() else ""
            entries.append(self.display_path(item) + suffix)
            if len(entries) >= self.max_entries:
                entries.append(f"... 已截断，最多显示 {self.max_entries} 条")
                break
        return ToolExecutionResult("list_files", True, "\n".join(entries) or "目录为空")


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
        try:
            raw_path = _required_path(params.get("path"))
            target = self.resolve_path(raw_path)
        except ValueError as exc:
            return ToolExecutionResult("read_file", False, str(exc))
        if not target.exists():
            return ToolExecutionResult("read_file", False, f"文件不存在: {self.display_path(target)}")
        if not target.is_file():
            return ToolExecutionResult("read_file", False, f"目标不是文件: {self.display_path(target)}")

        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolExecutionResult("read_file", False, "文件不是有效 UTF-8 文本，无法读取。")
        lines = content.splitlines()
        try:
            start_line = _int_param(params.get("start_line"), name="start_line", default=1, min_value=1)
            end_line = _int_param(params.get("end_line"), name="end_line", default=max(len(lines), 1), min_value=1)
        except ValueError as exc:
            return ToolExecutionResult("read_file", False, str(exc))
        if end_line < start_line:
            return ToolExecutionResult("read_file", False, "end_line 不能小于 start_line")
        selected = lines[start_line - 1 : end_line]
        numbered = [f"{idx}: {line}" for idx, line in enumerate(selected, start=start_line)]
        result = "\n".join(numbered)
        if len(result) > self.max_chars:
            result = result[: self.max_chars] + "\n... 已截断"
        return ToolExecutionResult("read_file", True, result or "(空文件)")


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
            },
            parameter_details={
                "query": "必填，直接按文本包含关系匹配，不做正则解析。",
                "path": "可选，把搜索范围缩小到某个子目录时更高效。",
            },
            examples=[
                '{"tool": "search_text", "query": "PromptBuilder"}',
                '{"tool": "search_text", "query": "max_tool_rounds", "path": "agent_py_agent"}',
            ],
        )

    # LLM: SearchTextTool.execute 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 执行 SearchTextTool 的主流程并返回 ToolExecutionResult。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            query = _text_param(params.get("query"), name="query", max_chars=_MAX_SEARCH_QUERY_CHARS, strip=True)
            raw_path = _optional_path(params.get("path"), default=".")
            target = self.resolve_path(raw_path)
        except ValueError as exc:
            return ToolExecutionResult("search_text", False, str(exc))
        if not target.exists():
            return ToolExecutionResult("search_text", False, f"路径不存在: {self.display_path(target)}")
        search_root = target if target.is_dir() else target.parent
        candidates = [target] if target.is_file() else list(search_root.rglob("*"))
        matches: list[str] = []
        for item in candidates:
            if not item.is_file():
                continue
            found = self._search_item_for_query(item, query, matches)
            if found == "full":
                return ToolExecutionResult("search_text", True, "\n".join(matches))
        return ToolExecutionResult("search_text", True, "\n".join(matches) or "没有找到匹配项")

    # LLM: SearchTextTool._search_item_for_query 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 工具系统 中的 search_item_for_query 步骤，并保持调用方依赖的数据形状。
    def _search_item_for_query(self, item: Path, query: str, matches: list[str]) -> str:
        try:
            safe_item = self.resolve_path(item)
        except ValueError:
            return ""
        try:
            return self._search_lines(item, safe_item, query, matches)
        except UnicodeDecodeError:
            return ""
        return ""

    # LLM: SearchTextTool._search_lines 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 工具系统 中的 search_lines 步骤，并保持调用方依赖的数据形状。
    def _search_lines(
        self,
        item: Path,
        safe_item: Path,
        query: str,
        matches: list[str],
    ) -> str:
        for idx, line in enumerate(safe_item.read_text(encoding="utf-8").splitlines(), start=1):
            if query not in line:
                continue
            self._append_search_match(self._item_relative_path(item, safe_item), idx, line, matches)
            if len(matches) >= self.max_matches:
                matches.append(f"... 已截断，最多显示 {self.max_matches} 条")
                return "full"
        return ""

    # LLM: SearchTextTool._append_search_match 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 向结果或告警集合加入 append_search_match，同时保留调用方依赖的顺序。
    def _append_search_match(
        self,
        rel: str,
        line_number: int,
        line: str,
        matches: list[str],
    ) -> None:
        snippet = self._make_snippet(line)
        matches.append(f"{rel}:{line_number}: {snippet}")

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


# LLM: _normalized_workspace_roots 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 解析并去重工作区根目录，保留第一个主工作区。
def _normalized_workspace_roots(primary: Path, roots: list[Path] | None) -> list[Path]:
    resolved: list[Path] = []
    for raw in [primary, *(roots or [])]:
        path = Path(raw).resolve()
        if path not in resolved:
            resolved.append(path)
    return resolved


# LLM: _is_under_any_root 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 判断 is_under_any_root 是否满足安全或状态条件。
def _is_under_any_root(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False
