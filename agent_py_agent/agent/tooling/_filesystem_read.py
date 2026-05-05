"""Read-only filesystem tools."""

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


class FileSystemTool(BaseTool):
    """文件系统类工具的安全边界。

    大白话解释：
    不管模型多聪明，都不能让它随便跳出工作区去乱读乱写。
    这里统一把路径钉死在工作区下面，减少误操作风险。
    """

    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root.resolve()

    def resolve_path(self, raw_path: str | Path) -> Path:
        """解析路径，并强制限制在工作区内部。"""

        raw_text = _required_path(raw_path)
        candidate = Path(raw_text)
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        try:
            candidate = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError("路径解析失败，请检查路径是否有效。") from exc
        try:
            candidate.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError("路径超出允许的工作区范围，请使用工作区内路径。") from exc
        return candidate

    def display_path(self, path: Path) -> str:
        """把路径转成可审计但不泄露工作区绝对路径的格式。"""

        try:
            return str(path.relative_to(self.workspace_root))
        except ValueError:
            return "<outside-workspace>"


class ListFilesTool(FileSystemTool):
    """列目录内容。"""

    def __init__(self, workspace_root: Path, max_entries: int):
        super().__init__(workspace_root)
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
            entries.append(str(item.relative_to(self.workspace_root)) + suffix)
            if len(entries) >= self.max_entries:
                entries.append(f"... 已截断，最多显示 {self.max_entries} 条")
                break
        return ToolExecutionResult("list_files", True, "\n".join(entries) or "目录为空")


class ReadFileTool(FileSystemTool):
    """读取文本文件。"""

    def __init__(self, workspace_root: Path, max_chars: int):
        super().__init__(workspace_root)
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


class SearchTextTool(FileSystemTool):
    """在工作区里做纯文本搜索。"""

    def __init__(self, workspace_root: Path, max_matches: int):
        super().__init__(workspace_root)
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

    def _search_item_for_query(self, item: Path, query: str, matches: list[str]) -> str:
        """Search one file item for query; return 'full' if max matches reached, '' otherwise."""
        try:
            safe_item = self.resolve_path(item)
        except ValueError:
            return ""
        try:
            for idx, line in enumerate(safe_item.read_text(encoding="utf-8").splitlines(), start=1):
                if query not in line:
                    continue
                snippet = self._make_snippet(line)
                rel = self._item_relative_path(item, safe_item)
                matches.append(f"{rel}:{idx}: {snippet}")
                if len(matches) >= self.max_matches:
                    matches.append(f"... 已截断，最多显示 {self.max_matches} 条")
                    return "full"
        except UnicodeDecodeError:
            pass
        return ""

    def _make_snippet(self, line: str) -> str:
        """Make a truncated snippet from a line."""
        snippet = line.strip()
        if len(snippet) > _MAX_SEARCH_LINE_CHARS:
            snippet = snippet[:_MAX_SEARCH_LINE_CHARS] + "... 已截断"
        return snippet

    def _item_relative_path(self, item: Path, safe_item: Path) -> Path:
        """Get relative path, trying item first then safe_item."""
        try:
            return item.relative_to(self.workspace_root)
        except ValueError:
            return safe_item.relative_to(self.workspace_root)
