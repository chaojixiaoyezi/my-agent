# LLM: 本模块是文本文件工具到富 TUI 的唯一 diff/write 展示构造器；返回值只供 UI 投影，不参与写入、授权、验收或工具成功判断。
# 模块用途: 捕获文件修改当时完整的增删行/写入内容，供不可变展示归档及有界预览。

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any


# LLM: diff行由before/after结构化比较生成；原始行表完整交给展示归档，UI预览不能成为历史唯一来源。
# 函数用途: 保存带旧/新行号的完整修改快照，避免后续全展开时内容已经丢失。
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
    return {
        "kind": "diff",
        "path": str(path),
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "lines": rows,
        "hidden_lines": 0,
    }


# LLM: 写入时捕获完整不可变正文交给展示归档；传输预览预算由公开投影负责，不能提前丢掉历史。
# 函数用途: 保存工具执行当时的完整写入展示；二进制仅展示字节数。
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
    return {
        "kind": "write",
        "path": str(path),
        "mode": str(mode),
        "bytes": max(0, int(bytes_written or 0)),
        "binary": False,
        "total_lines": len(all_lines),
        "lines": all_lines,
        "hidden_lines": 0,
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


__all__ = [
    "build_text_diff_display",
    "build_write_display",
    "existing_utf8_text_for_display",
]
