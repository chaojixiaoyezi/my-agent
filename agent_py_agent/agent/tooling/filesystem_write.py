from __future__ import annotations

"""LLM: implements workspace-scoped filesystem write, append, and replace tools with path containment.

新手说明:
这个文件负责"写本地工作区文件"：写文件、追加内容、局部替换。
读文件、列目录、搜索文字等只读工具在 filesystem.py 里。
所有工具都继承 FileSystemTool，路径安全规则和校验 helper 也从那里来。
"""

from pathlib import Path
from typing import Any

from .filesystem import (
    _MAX_WRITE_TEXT_CHARS,
    FileSystemTool,
    _int_param,
    _required_path,
    _text_param,
)
from .models import ToolExecutionResult, ToolSpec


class WriteFileTool(FileSystemTool):

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
            old_text = _text_param(
                params.get("old"),
                name="old",
                max_chars=_MAX_WRITE_TEXT_CHARS,
            )
            new_text = _text_param(
                params.get("new"),
                name="new",
                max_chars=_MAX_WRITE_TEXT_CHARS,
                allow_empty=True,
            )
            target = self.resolve_path(raw_path)
        except ValueError as exc:
            return ToolExecutionResult("replace_in_file", False, str(exc))
        if not target.exists():
            return ToolExecutionResult("replace_in_file", False, f"文件不存在: {self.display_path(target)}")
        if not target.is_file():
            return ToolExecutionResult("replace_in_file", False, f"目标不是文件: {self.display_path(target)}")

        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolExecutionResult("replace_in_file", False, "文件不是有效 UTF-8 文本，无法替换。")

        matches = content.count(old_text)
        if matches == 0:
            return ToolExecutionResult("replace_in_file", False, "没有找到要替换的原文，请先 read_file 确认上下文")

        try:
            raw_count = _int_param(params.get("count"), name="count", default=1)
        except ValueError as exc:
            return ToolExecutionResult("replace_in_file", False, str(exc))
        replace_count = matches if raw_count <= 0 else raw_count
        updated = content.replace(old_text, new_text, replace_count)
        changed = min(matches, replace_count)
        target.write_text(updated, encoding="utf-8")
        return ToolExecutionResult(
            "replace_in_file",
            True,
            f"已修改文件: {self.display_path(target)}；替换 {changed} 处；原文共命中 {matches} 处",
        )
