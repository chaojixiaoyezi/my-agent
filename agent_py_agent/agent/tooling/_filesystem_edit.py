# LLM: edit_file 精确局部编辑工具(对照 5 主流 agent 的能力补齐,P0-1)。
#   背景:my-agent 此前只有 write_file(全量覆写,改一行也要重写整个文件,费
#   token 且易覆盖错)+ apply_patch(diff 格式,易写错),缺最高频的"把这段
#   改成那段"工具——对照组 4/5(通道运行时/长期助手/终端交互/工具运行时)都有。
#   设计超越点:不止精确匹配,带三级容错级联(精确 → 行首尾空白归一 → 行内
#   空白归一),抄 工具运行时 edit.ts 的多策略匹配思路,直接超越只做精确匹配的
#   会话运行时/终端交互——模型给的 old_string 缩进/空白和文件略有出入也能命中,
#   大幅降低编辑失败率。契约:①old_string 必须能唯一定位,多处命中且未开
#   replace_all 时报错并要求更多上下文(防误改);②容错命中时用文件里的真实
#   文本做替换(不是模型给的近似文本),保证字节正确;③复用 FileSystemTool
#   的路径解析 + 原子写(与 write_file/apply_patch 同一写入边界)。
# 模块用途: 让模型像人改代码一样"找到这段、改成那段",找不准时自动放宽空白
#   比对,而不是逼模型重写整个文件或硬拼 diff。
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ._filesystem_helpers import _MAX_WRITE_TEXT_CHARS, _text_param
from ._filesystem_read import FileSystemAccessOptions, FileSystemTool
from ._filesystem_write import _atomic_write_bytes
from .models import ToolExecutionResult, ToolSpec


class EditFileTool(FileSystemTool):
    def __init__(
        self,
        workspace_root: Path,
        workspace_roots: list[Path] | None = None,
        access_options: FileSystemAccessOptions | None = None,
    ):
        super().__init__(workspace_root, workspace_roots, access_options)
        self.spec = ToolSpec(
            name="edit_file",
            category="filesystem",
            effect="mutating",
            requires_idempotency=True,
            description="把已有文本文件里的 old_string 精确替换成 new_string（带空白容错匹配）。改几行时首选，比 write_file 省、比 apply_patch 简单。",
            use_cases=[
                "修改已有代码/配置/文档里的一处或几处文本",
                "把 new_string 设为空串即可删除 old_string 这段",
            ],
            avoid_when=[
                "新建文件用 write_file",
                "整文件重写用 write_file",
                "一次要改很多文件用 apply_patch",
            ],
            keywords=["编辑", "替换", "改文件", "edit", "str_replace", "局部修改"],
            parameters={
                "path": "要编辑的文件路径",
                "old_string": "文件中要被替换的原文（需足够唯一以精确定位）",
                "new_string": "替换成的新文本（空串=删除 old_string）",
                "replace_all": "可选，true 时替换所有匹配处（默认只换唯一一处）",
            },
            parameter_details={
                "old_string": (
                    "必须能在文件中唯一定位；若只给一行而文件多处相同会报错，"
                    "这时多带几行上下文，或设 replace_all=true。"
                    "缩进/行首尾空白和文件略有出入也能容错命中（按真实文本替换）。"
                ),
                "new_string": "允许空串（删除）；保持与 old_string 一致的缩进风格。",
                "replace_all": "布尔，默认 false。",
            },
            examples=[
                '{"tool": "edit_file", "path": "app.py", "old_string": "timeout = 30", "new_string": "timeout = 60"}',
                '{"tool": "edit_file", "path": "config.yaml", "old_string": "debug: true", "new_string": "debug: false", "replace_all": true}',
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            target = self.resolve_path(_text_param(params.get("path"), name="path", max_chars=4096, strip=True))
            old = _text_param(params.get("old_string"), name="old_string", max_chars=_MAX_WRITE_TEXT_CHARS)
            new = _text_param(params.get("new_string"), name="new_string", max_chars=_MAX_WRITE_TEXT_CHARS, allow_empty=True)
            replace_all = bool(params.get("replace_all"))
            if old == new:
                raise ValueError("old_string 与 new_string 相同，无需编辑")
            if not target.exists():
                raise ValueError(f"文件不存在: {self.display_path(target)}")
            content = target.read_text(encoding="utf-8")
            updated, strategy, count = _replace_in_content(content, old, new, replace_all=replace_all)
        except ValueError as exc:
            return ToolExecutionResult("edit_file", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")
        _atomic_write_bytes(target, updated.encode("utf-8"))
        note = "" if strategy == "exact" else f"（{strategy} 容错匹配）"
        return ToolExecutionResult("edit_file", True, f"已编辑 {self.display_path(target)}：替换 {count} 处{note}")


# 函数用途: 在文件内容里定位并替换 old→new,精确优先,失配按三级容错降级。
def _replace_in_content(content: str, old: str, new: str, *, replace_all: bool) -> tuple[str, str, int]:
    exact_count = content.count(old)
    if exact_count > 0:
        if exact_count > 1 and not replace_all:
            raise ValueError(
                f"old_string 在文件中出现 {exact_count} 次,不唯一;请多带几行上下文使其唯一,或设 replace_all=true。"
            )
        replaced = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        return replaced, "exact", (exact_count if replace_all else 1)
    return _fuzzy_replace(content, old, new, replace_all=replace_all)


_LINE_TRIM = ("line_trimmed", lambda line: line.strip())
_WS_NORM = ("whitespace_normalized", lambda line: re.sub(r"\s+", " ", line).strip())


# 函数用途: 精确失配时的容错替换——按行规范化(先行首尾空白,再行内空白)滑窗
#   找连续块,唯一命中即用文件里的真实行替换;多处命中且未开 replace_all 则报错。
def _fuzzy_replace(content: str, old: str, new: str, *, replace_all: bool) -> tuple[str, str, int]:
    content_lines = content.split("\n")
    old_lines = old.split("\n")
    while old_lines and old_lines[-1] == "":
        old_lines.pop()
    if not old_lines:
        raise ValueError("old_string 为空白,无法定位。")
    for label, normalizer in (_LINE_TRIM, _WS_NORM):
        spans = _matching_spans(content_lines, old_lines, normalizer)
        if not spans:
            continue
        if len(spans) > 1 and not replace_all:
            raise ValueError(
                f"容错匹配({label})在文件中找到 {len(spans)} 处,不唯一;请多带上下文或设 replace_all=true。"
            )
        new_block = new.split("\n")
        result = list(content_lines)
        applied = spans if replace_all else spans[:1]
        for start, end in reversed(applied):
            result[start:end] = _reindent(new_block, old_lines, content_lines[start:end])
        return "\n".join(result), label, len(applied)
    raise ValueError(
        "old_string 未在文件中找到(精确/行空白/全空白三级匹配均失败);"
        "请先用 read_file 确认确切文本再编辑。"
    )


# 函数用途: 取一行的前导空白(缩进)。
def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


# 函数用途: 容错命中时把"真实块比 old_string 多出的公共前导缩进"补到 new 每行——
#   模型给无缩进的 old/new,在缩进代码里替换后也能保持缩进对齐(超越点)。
def _reindent(new_block: list[str], old_lines: list[str], real_lines: list[str]) -> list[str]:
    old_min = min((_leading_ws(line) for line in old_lines if line.strip()), default="", key=len)
    real_min = min((_leading_ws(line) for line in real_lines if line.strip()), default="", key=len)
    if not real_min.startswith(old_min) or len(real_min) <= len(old_min):
        return new_block
    delta = real_min[len(old_min):]
    return [delta + line if line.strip() else line for line in new_block]


# 函数用途: 用给定的行规范化函数,在内容里找出所有与 old_lines 连续匹配的行区间。
def _matching_spans(content_lines: list[str], old_lines: list[str], normalizer: Any) -> list[tuple[int, int]]:
    norm_old = [normalizer(line) for line in old_lines]
    window = len(norm_old)
    spans: list[tuple[int, int]] = []
    index = 0
    limit = len(content_lines) - window
    while index <= limit:
        if [normalizer(content_lines[index + offset]) for offset in range(window)] == norm_old:
            spans.append((index, index + window))
            index += window
        else:
            index += 1
    return spans


__all__ = ["EditFileTool"]
