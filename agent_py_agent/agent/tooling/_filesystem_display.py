# LLM: 本模块是文本文件工具到富 TUI 的唯一 diff/write 展示构造器；返回值只供 UI 投影，不参与写入、授权、验收或工具成功判断。
# 模块用途: 把文件修改整理成带行号的增删行，或把整文件写入整理成有界内容预览。

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any


# LLM: diff 行由 before/after 结构化比较生成，renderer 不得从自然语言 output 反推；行表有硬上限但总增删数保持完整。
# 函数用途: 生成带旧/新行号、上下文和隐藏数量的终端 diff 展示数据。
def build_text_diff_display(path: str, before: str, after: str) -> dict[str, Any]:
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    rows: list[dict[str, Any]] = []
    lines_added = 0
    lines_removed = 0
    for group in matcher.get_grouped_opcodes(n=3):
        first = group[0]
        last = group[-1]
        old_start, old_end = first[1], last[2]
        new_start, new_end = first[3], last[4]
        rows.append(
            {
                "kind": "header",
                "text": (
                    f"@@ -{_diff_range_start(old_start, old_end)},"
                    f"{old_end - old_start} +{_diff_range_start(new_start, new_end)},"
                    f"{new_end - new_start} @@"
                ),
            }
        )
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for offset, line in enumerate(before_lines[i1:i2]):
                    rows.append(
                        {
                            "kind": "context",
                            "old_line": i1 + offset + 1,
                            "new_line": j1 + offset + 1,
                            "text": line,
                        }
                    )
                continue
            if tag in {"replace", "delete"}:
                lines_removed += i2 - i1
                for offset, line in enumerate(before_lines[i1:i2]):
                    rows.append(
                        {
                            "kind": "remove",
                            "old_line": i1 + offset + 1,
                            "new_line": None,
                            "text": line,
                        }
                    )
            if tag in {"replace", "insert"}:
                lines_added += j2 - j1
                for offset, line in enumerate(after_lines[j1:j2]):
                    rows.append(
                        {
                            "kind": "add",
                            "old_line": None,
                            "new_line": j1 + offset + 1,
                            "text": line,
                        }
                    )
    visible, hidden = _bounded_diff_rows(rows, max_rows=180)
    return {
        "kind": "diff",
        "path": str(path),
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "lines": visible,
        "hidden_lines": hidden,
    }


# LLM: 文本预览按行裁剪并显式报告 hidden_lines；二进制只展示字节事实，绝不尝试解码或把 base64 当正文。
# 函数用途: 生成 终端交互 风格的写文件摘要和可展开内容行。
def build_write_display(
    *,
    path: str,
    content: str | None,
    mode: str,
    bytes_written: int,
) -> dict[str, Any]:
    if content is None:
        return {
            "kind": "write",
            "path": str(path),
            "mode": str(mode),
            "bytes": max(0, int(bytes_written or 0)),
            "binary": True,
            "total_lines": 0,
            "lines": [],
            "hidden_lines": 0,
        }
    all_lines = content.splitlines()
    if content and not all_lines:
        all_lines = [content]
    max_lines = 180
    visible_lines = all_lines[:max_lines]
    return {
        "kind": "write",
        "path": str(path),
        "mode": str(mode),
        "bytes": max(0, int(bytes_written or 0)),
        "binary": False,
        "total_lines": len(all_lines),
        "lines": visible_lines,
        "hidden_lines": max(0, len(all_lines) - len(visible_lines)),
    }


# LLM: 覆盖写 diff 的旧正文只能来自写入前同一路径；不存在、非文件或非 UTF-8 时返回 None 并降级为 write preview，不影响真实写入。
# 函数用途: 尝试读取覆盖前的文本，供整文件重写显示增删差异。
def existing_utf8_text_for_display(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


# LLM: 零长度 unified range 使用前一行位置；此格式只影响展示 header，不改变真实行号字段。
# 函数用途: 把 SequenceMatcher 的零基区间起点转成常见 diff 范围起点。
def _diff_range_start(start: int, end: int) -> int:
    return start if start == end else start + 1


# LLM: 大 diff 保留头尾而不是只留头，确保终端仍能看到结束处的错误/收尾改动；hidden_lines 是显式展示事实。
# 函数用途: 将结构化 diff 行裁到有界大小并返回省略数量。
def _bounded_diff_rows(
    rows: list[dict[str, Any]],
    *,
    max_rows: int,
) -> tuple[list[dict[str, Any]], int]:
    limit = max(1, int(max_rows or 1))
    if len(rows) <= limit:
        return rows, 0
    head = limit * 2 // 3
    tail = limit - head
    return [*rows[:head], *rows[-tail:]], len(rows) - limit


__all__ = [
    "build_text_diff_display",
    "build_write_display",
    "existing_utf8_text_for_display",
]
