from __future__ import annotations

"""LLM: implements workspace-scoped filesystem tools with path containment checks.

给人看的解释：
这个文件只负责“读写本地工作区文件”。
模型想看目录、读文件、搜索文字、写文件、追加内容、局部替换内容，都会走这里。
最重要的安全规则也在这里：路径必须待在 workspace 里面，不能偷偷跑到项目外。
"""

from pathlib import Path
from typing import Any

from .models import BaseTool, ToolExecutionResult, ToolSpec

_MAX_PATH_CHARS = 4096
_MAX_SEARCH_QUERY_CHARS = 4000
_MAX_SEARCH_LINE_CHARS = 500
_MAX_WRITE_TEXT_CHARS = 1_000_000


def _has_control_chars(text: str) -> bool:
    return any(ord(char) < 32 for char in text)


def _required_path(value: Any, *, name: str = "path") -> str:
    if value is None:
        raise ValueError(f"缺少必填参数 {name}")
    if not isinstance(value, (str, Path)):
        raise ValueError(f"{name} 参数必须是字符串路径")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{name} 不能为空")
    if len(text) > _MAX_PATH_CHARS:
        raise ValueError(f"{name} 过长，最多 {_MAX_PATH_CHARS} 个字符")
    if _has_control_chars(text):
        raise ValueError(f"{name} 包含不支持的控制字符")
    return text


def _optional_path(value: Any, *, default: str = ".") -> str:
    if value is None:
        return default
    return _required_path(value)


def _text_param(
    value: Any,
    *,
    name: str,
    max_chars: int,
    allow_empty: bool = False,
    strip: bool = False,
) -> str:
    if value is None:
        raise ValueError(f"缺少必填参数 {name}")
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"{name} 参数必须是字符串或标量文本")
    text = str(value)
    if strip:
        text = text.strip()
    if not allow_empty and text == "":
        raise ValueError(f"{name} 不能为空")
    if len(text) > max_chars:
        raise ValueError(f"{name} 过长，最多 {max_chars} 个字符")
    return text


def _int_param(value: Any, *, name: str, default: int, min_value: int | None = None) -> int:
    if value is None:
        parsed = default
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须是整数") from exc
    if min_value is not None and parsed < min_value:
        raise ValueError(f"{name} 不能小于 {min_value}")
    return parsed


def _bool_param(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _read_text_safe(path: Path) -> str | None:
    """Read text file as UTF-8, returning None on error."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def _parse_count_param(value: Any) -> int:
    """Parse count parameter, returning 1 on error."""
    if value is None:
        return 1
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


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


class WriteFileTool(FileSystemTool):
    """写文件或覆盖文件。"""

    def __init__(self, workspace_root: Path):
        super().__init__(workspace_root)
        self.spec = ToolSpec(
            name="write_file",
            category="filesystem",
            description="写入或覆盖一个文本文件，适合生成新代码、脚本和配置。",
            use_cases=[
                "新建代码文件、配置文件或文档",
                "已经明确要重写某个文件的完整内容",
            ],
            avoid_when=[
                "只想补几行内容时别整文件重写，优先 append_file 或后续更细粒度编辑工具",
            ],
            keywords=["写文件", "生成代码", "创建文件", "覆盖", "save file", "write"],
            parameters={
                "path": "要写入的文件路径",
                "content": "完整文本内容",
            },
            parameter_details={
                "path": "相对工作区的目标文件路径；父目录不存在时会自动创建。",
                "content": "会直接成为文件的新内容；原文件存在时会被整体覆盖。",
            },
            examples=[
                '{"tool": "write_file", "path": "src/demo.py", "content": "print(\\"hello\\")\\n"}',
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            raw_path = _required_path(params.get("path"))
            content = _text_param(
                params.get("content"),
                name="content",
                max_chars=_MAX_WRITE_TEXT_CHARS,
                allow_empty=True,
            )
            target = self.resolve_path(raw_path)
        except ValueError as exc:
            return ToolExecutionResult("write_file", False, str(exc))
        target.parent.mkdir(parents=True, exist_ok=True)
        target = self.resolve_path(target)
        target.write_text(content, encoding="utf-8")
        return ToolExecutionResult(
            "write_file",
            True,
            f"已写入文件: {self.display_path(target)}",
        )


class AppendFileTool(FileSystemTool):
    """向文件末尾追加内容。"""

    def __init__(self, workspace_root: Path):
        super().__init__(workspace_root)
        self.spec = ToolSpec(
            name="append_file",
            category="filesystem",
            description="向文本文件末尾追加内容，适合补日志、补文档和补配置片段。",
            use_cases=[
                "往日志、Markdown、结果汇总文件后面追加一段内容",
                "在不覆盖原文件的前提下补充说明",
            ],
            avoid_when=[
                "需要精确修改文件中间某一段时，不要拿它硬凑",
            ],
            keywords=["追加", "append", "补文档", "补日志", "末尾添加"],
            parameters={
                "path": "要追加的文件路径",
                "content": "要追加的文本内容",
            },
            parameter_details={
                "path": "相对工作区的目标文件路径；父目录不存在时会自动创建。",
                "content": "会直接拼接到文件尾部，不会替换已有内容。",
            },
            examples=[
                '{"tool": "append_file", "path": "RUNLOG.md", "content": "\\n- 新增一条记录"}',
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            raw_path = _required_path(params.get("path"))
            content = _text_param(
                params.get("content"),
                name="content",
                max_chars=_MAX_WRITE_TEXT_CHARS,
                allow_empty=True,
            )
            target = self.resolve_path(raw_path)
        except ValueError as exc:
            return ToolExecutionResult("append_file", False, str(exc))
        target.parent.mkdir(parents=True, exist_ok=True)
        target = self.resolve_path(target)
        with target.open("a", encoding="utf-8") as file:
            file.write(content)
        return ToolExecutionResult(
            "append_file",
            True,
            f"已追加文件: {self.display_path(target)}",
        )


class ReplaceInFileTool(FileSystemTool):
    """精确替换文件中的一段文本。

    这个工具是给“差异化编辑”准备的。
    大白话说：如果只需要改一个函数、一行配置或一小段说明，就不要整文件覆盖。
    """

    def __init__(self, workspace_root: Path):
        super().__init__(workspace_root)
        self.spec = ToolSpec(
            name="replace_in_file",
            category="filesystem",
            description="在文本文件中精确替换一段已有内容，适合小范围改代码和改配置。",
            use_cases=[
                "只改一个函数、一段注释、一行配置或一小段文档",
                "已经通过 read_file 看过上下文，知道要替换的原文",
                "希望保留文件其他部分不动，避免 write_file 整文件覆盖",
            ],
            avoid_when=[
                "要创建新文件时用 write_file",
                "只是往文件末尾补内容时用 append_file",
                "不知道原文是否唯一时，先 read_file 或 search_text 确认上下文",
            ],
            keywords=[
                "替换",
                "修改代码",
                "局部编辑",
                "差异化编辑",
                "replace",
                "patch",
                "refactor",
                "edit",
            ],
            parameters={
                "path": "要修改的文件路径",
                "old": "文件中已经存在的原文",
                "new": "替换后的新内容",
                "count": "最多替换几处，默认 1",
            },
            parameter_details={
                "path": "相对工作区的文本文件路径，必须是已有文件。",
                "old": "必填，必须和文件里的原文完全一致；建议先用 read_file 获取准确片段。",
                "new": "必填，用来替换 old 的新文本。",
                "count": "可选，默认只替换第一处；传 0 或负数表示替换全部匹配。",
            },
            examples=[
                '{"tool": "replace_in_file", "path": "agent_py_agent/agent/core.py", "old": "max_tool_rounds: int = 5", "new": "max_tool_rounds: int = 8"}',
                '{"tool": "replace_in_file", "path": "README.md", "old": "old text", "new": "new text", "count": 1}',
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            raw_path = _required_path(params.get("path"))
            old_text = _text_param(params.get("old"), name="old", max_chars=_MAX_WRITE_TEXT_CHARS)
            new_text = _text_param(params.get("new"), name="new", max_chars=_MAX_WRITE_TEXT_CHARS, allow_empty=True)
            target = self.resolve_path(raw_path)
        except ValueError as exc:
            return ToolExecutionResult("replace_in_file", False, str(exc))
        if not target.exists():
            return ToolExecutionResult("replace_in_file", False, f"文件不存在: {self.display_path(target)}")
        if not target.is_file():
            return ToolExecutionResult("replace_in_file", False, f"目标不是文件: {self.display_path(target)}")
        content = _read_text_safe(target)
        if content is None:
            return ToolExecutionResult("replace_in_file", False, "文件不是有效 UTF-8 文本，无法替换。")
        matches = content.count(old_text)
        if matches == 0:
            return ToolExecutionResult("replace_in_file", False, "没有找到要替换的原文，请先 read_file 确认上下文")
        raw_count = _parse_count_param(params.get("count"))
        replace_count = matches if raw_count <= 0 else raw_count
        updated = content.replace(old_text, new_text, replace_count)
        changed = min(matches, replace_count)
        target.write_text(updated, encoding="utf-8")
        return ToolExecutionResult(
            "replace_in_file", True,
            f"已修改文件: {self.display_path(target)}；替换 {changed} 处；原文共命中 {matches} 处",
        )
