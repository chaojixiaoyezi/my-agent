# LLM: 本模块仅分页已公开的不可变显示快照，不读当前文件、不调用模型，不修改 canonical 历史。
# 模块用途: 把长正文、思考及工具原始输出分成有界页，保留所有已有字符并按终端宽度换行。

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .tui_markdown import FormattedLine, sanitize_terminal_text, wrap_fragments
from .tui_view_model import TuiBlock, TuiViewSnapshot

DETAIL_PAGE_ROWS = 256
DETAIL_PAGE_CHARS = 12_000
DETAIL_ROW_CHARS = 2_000


# LLM: 页只持有公开字符串及样式，不持有执行回调；字符预算限制单次排版工作量。
# 类用途: 保存一页原文以及其中每行的样式，避免把长历史一次渲染到终端。
@dataclass(frozen=True)
class CompleteDetailPage:
    rows: tuple[tuple[str, str], ...]
    reference: dict[str, object] | None = None
    remote_page: int = 0

    # LLM: 只对当前页做净化与显示宽度换行，原始页字符串不被改变。
    # 函数用途: 在当前终端宽度下完整显示一页，不裁掉长行右侧内容。
    def render(self, width: int) -> tuple[FormattedLine, ...]:
        rendered: list[FormattedLine] = []
        for style, text in self.rows:
            safe = sanitize_terminal_text(text).replace("\t", "    ")
            rendered.extend(wrap_fragments(((style, safe),), width=max(1, width)) or ((),))
        return tuple(rendered)


# LLM: 归档只保存每个输出的一条引用，不为百万远端页分配百万对象；页数不授权文件读取。
# 类用途: 通过稀疏索引定位当前原文页，长历史的远端页表不会挤占TUI内存。
class CompleteDetailPages(Sequence[CompleteDetailPage]):
    # LLM: 本地页计1、远端段计manifest声明页数；只构造累计索引，不拉取原文。
    # 函数用途: 建立显示段到完整页码的映射。
    def __init__(self, segments: list[CompleteDetailPage]) -> None:
        self.segments = tuple(segments)
        self.ends: list[int] = []
        total = 0
        for page in segments:
            total += int(page.reference["page_count"]) if page.reference else 1
            self.ends.append(total)

    # LLM: 返回逻辑页数，不展开远端段。
    # 函数用途: 为页脚和翻页提供总页数。
    def __len__(self) -> int:
        return self.ends[-1] if self.ends else 0

    # LLM: 只按整数页定位，读取仍由Gateway鉴权；负索引保持Sequence约定。
    # 函数用途: 二分定位当前页并按需生成轻量远端描述符。
    def __getitem__(self, index: int) -> CompleteDetailPage:
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        segment = bisect_right(self.ends, index)
        page = self.segments[segment]
        return replace(page, remote_page=index - (self.ends[segment - 1] if segment else 0)) if page.reference else page


# LLM: 数据来自同一 frozen snapshot，分页只创建显示索引；不把缺失历史伪装成完整结果。
# 函数用途: 按行数及字符数双预算拆分已加载历史，页面格式化成本不随整个会话增长。
def build_complete_detail_pages(snapshot: TuiViewSnapshot) -> CompleteDetailPages:
    pages: list[CompleteDetailPage] = []
    rows: list[tuple[str, str]] = []
    chars = 0
    blocks = sorted((*snapshot.stable_blocks, *snapshot.active_blocks),
                    key=lambda block: (block.created_seq, block.updated_seq, block.block_id))
    for block in blocks:
        reference = block.metadata.get("display_archive_ref")
        if (isinstance(reference, dict) and reference.get("schema") == "display_archive_ref.v1"
                and isinstance(reference.get("archive_id"), str)
                and type(reference.get("page_count")) is int and reference["page_count"] > 0):
            if rows:
                pages.append(CompleteDetailPage(tuple(rows)))
                rows, chars = [], 0
            pages.append(CompleteDetailPage((), dict(reference)))
            continue
        for style, text in _block_rows(block):
            for part_index, part in enumerate(_split_row(text)):
                if rows and (len(rows) >= DETAIL_PAGE_ROWS or chars + len(part) > DETAIL_PAGE_CHARS):
                    pages.append(CompleteDetailPage(tuple(rows)))
                    rows, chars = [], 0
                if part_index and rows:
                    rows[-1] = (style, rows[-1][1] + part)
                else:
                    rows.append((style, part))
                chars += len(part)
    if rows or not pages:
        pages.append(CompleteDetailPage(tuple(rows)))
    return CompleteDetailPages(pages)


# LLM: 切分长逻辑行只影响分页，必须保留每个字符，不使用带省略号的截断函数。
# 函数用途: 让单行巨量输出也能分段查看，避免一行撑爆单页字符预算。
def _split_row(text: str) -> Iterator[str]:
    if not text:
        yield ""
    for start in range(0, len(text), DETAIL_ROW_CHARS):
        yield text[start:start + DETAIL_ROW_CHARS]


# LLM: role 和 display.kind 是唯一格式来源；正文不能决定状态，内部执行参数不进入展示。
# 函数用途: 将公开消息转换为原文行；空系统占位不画假标题，思考灰色、差异保留增删颜色。
def _block_rows(block: TuiBlock) -> Iterator[tuple[str, str]]:
    if block.role in {"todo", "background", "connection", "session"}:
        return
    if block.role == "system" and not (block.text or block.detail or block.title):
        return
    labels = {"user": "用户", "assistant": "助手", "thinking": "思考", "tool": "工具"}
    style = "class:tui-thinking-detail" if block.role == "thinking" else "class:tui-tool-output"
    if block.role == "user":
        style = "class:tui-user-text"
    yield "class:tui-muted", f"── {labels.get(block.role, block.role)} {block.title} ──"
    display = block.metadata.get("display")
    if block.role == "tool" and isinstance(display, dict) and display.get("kind"):
        yield from _display_rows(display)
    else:
        text = block.text or block.detail
        for line in str(text or "").split("\n"):
            yield style, line
    if block.metadata.get("history_incomplete"):
        yield "class:tui-muted", "此历史记录未保存完整内容；不能从当前文件重建原始结果。"
    if block.role == "tool" and not block.metadata.get("display_archive_ref"):
        yield "class:tui-muted", "此记录没有原文归档引用；以下为已保存内容，执行端未保存的部分无法恢复。"
    if isinstance(block.metadata.get("display_archive_ref"), dict):
        yield "class:tui-muted", "完整原文归档失败，以下仅为保留的预览，不代表全部输出。"
    yield "", ""


# LLM: 完整浏览使用保存的 display envelope；hidden_* 只声明缺失，不从当前磁盘文件补造旧差异。
# 函数用途: 展示完整写入、差异、命令输出或明确的历史缺口提示。
def _display_rows(display: dict[str, Any]) -> Iterator[tuple[str, str]]:
    kind = display.get("kind")
    style = "class:tui-tool-output"
    if display.get("path"):
        yield "class:tui-tool-title", str(display["path"])
    if kind == "patch":
        for item in display.get("files", []):
            if isinstance(item, dict):
                yield from _display_rows(item)
    if kind == "command":
        for stream in ("stdout", "stderr"):
            color = "class:tui-error" if stream == "stderr" else style
            for line in str(display.get(stream) or "").split("\n"):
                yield color, line
    if kind == "diff":
        for item in display.get("lines", []):
            if not isinstance(item, dict):
                continue
            row_kind = item.get("kind")
            mark = "+" if row_kind == "add" else "-" if row_kind == "remove" else " "
            color = "class:tui-diff-add" if mark == "+" else "class:tui-diff-remove" if mark == "-" else style
            number = item.get("new_line") if mark == "+" else item.get("old_line")
            yield color, f"{number or ''} {mark} {item.get('text') or ''}"
    if kind == "write":
        if display.get("binary"):
            yield "class:tui-muted", f"二进制内容，{display.get('bytes', 0)} 字节"
        for number, line in enumerate(display.get("lines", []), 1):
            yield style, f"{number} {line}"
    for field, unit in (("hidden_lines", "行"), ("hidden_files", "个文件")):
        if display.get(field):
            yield "class:tui-muted", f"原记录另有 {display[field]} {unit}未保存全文；Ctrl+E不能恢复缺失内容。"


# LLM: Gateway返回的kind只是公开显示样式，不得进入状态裁决；未知类型按普通原文处理。
# 函数用途: 把已鉴权的一页归档行映射为本地样式，保留分片的所有字符。
def archive_page_rows(rows: object) -> CompleteDetailPage:
    if not isinstance(rows, list) or len(rows) > DETAIL_PAGE_ROWS:
        raise ValueError("invalid archive rows")
    if any(not isinstance(row, dict) or not isinstance(row.get("text"), str)
           or len(row["text"]) > DETAIL_ROW_CHARS for row in rows):
        raise ValueError("invalid archive row")
    if sum(len(row["text"]) for row in rows) > DETAIL_PAGE_CHARS:
        raise ValueError("archive page over budget")
    styles = {"add": "class:tui-diff-add", "remove": "class:tui-diff-remove",
              "stderr": "class:tui-error", "header": "class:tui-tool-title",
              "notice": "class:tui-muted", "thinking": "class:tui-thinking-detail"}
    rendered: list[tuple[str, str]] = []
    previous_row = None
    for row in rows:
        style = styles.get(str(row.get("kind")), "class:tui-tool-output")
        if rendered and row.get("row_index") is not None and row.get("row_index") == previous_row:
            rendered[-1] = (style, rendered[-1][1] + row["text"])
        else:
            rendered.append((style, row["text"]))
        previous_row = row.get("row_index")
    return CompleteDetailPage(tuple(rendered))
