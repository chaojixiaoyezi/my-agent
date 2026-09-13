# LLM: 本模块把 CommonMark token 转成与 终端交互 几何对齐的 prompt_toolkit fragments；不得用 Markdown 正文推断工具、权限或运行状态。
# 模块用途: 渲染助手 Markdown 的标题、段落、列表、引用、代码、表格和行内样式，并按终端显示宽度换行。

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token as MarkdownToken
from pygments import lex
from pygments.lexers import get_lexer_by_name
from pygments.lexers.special import TextLexer
from pygments.token import Comment, Generic, Keyword, Name, Number, Operator, Punctuation, String
from pygments.util import ClassNotFound
from wcwidth import wcswidth

Fragment = tuple[str, str]
FormattedLine = tuple[Fragment, ...]

# LLM: 净化 memo 的边界：单条上限决定"不缓存长正文"，条目上限决定常驻内存；两者都必须有界。
# 常量用途: 短 fragment 净化缓存的最大条目数与单条最大字符数。
_SANITIZE_MEMO_ENTRIES = 8_192
_SANITIZE_MEMO_MAX_CHARS = 512

# LLM: cluster 宽度缓存条目上限；TUI 常用字符集远小于该值，命中率接近 100%，越界后 LRU 淘汰只影响速度。
# 常量用途: 单个 grapheme cluster 显示宽度缓存的条目数上限。
_CLUSTER_WIDTH_ENTRIES = 4_096


# LLM: Every model/tool/user string is untrusted terminal data. This helper removes C0/C1
# controls and lone surrogates while preserving newline/tab and all printable Unicode, including
# ZWJ/variation selectors needed by emoji and bidi scripts. Callers must apply it before bytes
# reach prompt_toolkit; terminal operations may only originate from explicit TUI code.
# 函数用途: 清除可能执行清屏、移光标、响铃、改标题、写剪贴板或开启控制串的字符；普通换行、
# 制表符、中文、RTL 和 Emoji 仍按原文显示。
def sanitize_terminal_text(text: object) -> str:
    value = str(text or "")
    # 渲染器与 view 会对同一帧净化两次（安全边界不合并），稳定历史每一帧又是同一批 fragment；
    # 短文本走有界 memo 后第二次与后续帧都命中，长文本/巨行不进 memo，避免缓存长正文本身。
    if len(value) <= _SANITIZE_MEMO_MAX_CHARS:
        return _memoized_sanitize(value)
    return _strip_terminal_controls(value)


# LLM: 净化是纯函数，memo 只影响速度；缓存键是 fragment 文本本身，条数与单条长度都设上限，
# 因此不会像渲染缓存那样随流式版本增长，长正文也不会被缓存持有。
# 函数用途: 为短 fragment 提供有界净化缓存。
@lru_cache(maxsize=_SANITIZE_MEMO_ENTRIES)
def _memoized_sanitize(value: str) -> str:
    return _strip_terminal_controls(value)


# LLM: Cc(C0/C1) 与 Cs(代理区) 是 Unicode 固定区间：U+0000..U+001F、U+007F..U+009F、U+D800..U+DFFF。
# 这里把它们预先编译成 translate 表，使逐字符 unicodedata.category 判定下沉为一次 C 级扫描；
# \t(U+0009) 与 \n(U+000A) 属于 Cc 但必须保留，因此显式排除在删除集合之外。
_TERMINAL_CONTROL_TABLE: dict[int, None] = dict.fromkeys(
    (
        *range(0x00, 0x09),
        *range(0x0B, 0x20),
        *range(0x7F, 0xA0),
        *range(0xD800, 0xE000),
    )
)


# LLM: 逐字符判定改成等价 translate 表后输出逐字符相同（同集合删除、同集合保留），只是不再有 Python 级循环；
# 调用方（sanitize_terminal_text、memo）与安全边界不变，新增控制字符仍需改这里而不是改 Category 判定。
# 函数用途: 实际执行控制字符过滤。
def _strip_terminal_controls(value: str) -> str:
    return value.translate(_TERMINAL_CONTROL_TABLE)


# LLM: MarkdownRenderContext 只承载可复现的宽度和外层前缀；业务身份不能进入语法渲染。
# 类用途: 指定 Markdown 内容可用列数和每行基础缩进/样式。
@dataclass(frozen=True)
class MarkdownRenderContext:
    width: int
    prefix: FormattedLine = ()

    # LLM: width 至少保留一列，避免 malformed resize 造成 renderer 无限换行。
    # 函数用途: 规范最小终端宽度。
    def __post_init__(self) -> None:
        object.__setattr__(self, "width", max(1, int(self.width or 1)))


# LLM: _TableCollectionState 只在一次 table token 扫描内维护当前行/cell，不跨 Markdown block 共享。
# 类用途: 承载表格 token 收集时的行、单元格和是否已写正文状态。
@dataclass
class _TableCollectionState:
    rows: list[list[FormattedLine]]
    current_row: list[FormattedLine] | None = None
    in_cell: bool = False
    cell_has_content: bool = False


# LLM: parser 是只读 CommonMark+table token 服务；禁用 HTML，防止终端 renderer 接受隐式控制内容。
# 函数用途: 创建本模块唯一 MarkdownIt 配置。
def _make_markdown_parser() -> MarkdownIt:
    return MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")


_MARKDOWN = _make_markdown_parser()


# LLM: 公共入口只消费字符串并返回不可变行 fragments，调用方负责 assistant marker 和 block 间距。
# 函数用途: 将 Markdown 渲染成固定宽度的样式行。
def render_markdown(text: str, context: MarkdownRenderContext) -> tuple[FormattedLine, ...]:
    tokens = _MARKDOWN.parse(str(text or ""))
    lines = _render_blocks(tokens, 0, len(tokens), context, context.prefix)
    while lines and not fragments_text(lines[-1]):
        lines.pop()
    return tuple(lines or [context.prefix])


# LLM: 公共 wrapper 供非 Markdown block 复用同一 Unicode 宽度算法；首行与续行前缀可不同但都必须显式给出。
# 函数用途: 将普通样式 fragments 按宽度包装成不可变多行。
def wrap_fragments(
    fragments: FormattedLine,
    *,
    width: int,
    first_prefix: FormattedLine = (),
    continuation_prefix: FormattedLine | None = None,
) -> tuple[FormattedLine, ...]:
    context = MarkdownRenderContext(width=width)
    lines: list[FormattedLine] = []
    _append_wrapped(
        lines,
        fragments,
        context,
        first_prefix,
        continuation_prefix=continuation_prefix,
    )
    return tuple(lines)


# LLM: block walker 只按 markdown-it token type/nesting 路由；未知 block 有 content 时按普通文本显示，不能丢正文。
# 函数用途: 递归渲染一个 token 半开区间。
def _render_blocks(
    tokens: list[MarkdownToken],
    start: int,
    end: int,
    context: MarkdownRenderContext,
    prefix: FormattedLine,
) -> list[FormattedLine]:
    lines: list[FormattedLine] = []
    index = start
    while index < end:
        index = _render_block_token(tokens, index, end, context, prefix, lines)
    return lines


# LLM: 单 token 分派把循环与分支解耦，新增 block type 必须在此显式注册并保持未知 content 可见。
# 函数用途: 渲染当前位置的一个 block，并返回下一个 token 索引。
def _render_block_token(
    tokens: list[MarkdownToken],
    index: int,
    end: int,
    context: MarkdownRenderContext,
    prefix: FormattedLine,
    lines: list[FormattedLine],
) -> int:
    token = tokens[index]
    if token.type in {"heading_open", "paragraph_open"}:
        return _render_text_block(tokens, index, end, context, prefix, lines)
    if token.type in {"bullet_list_open", "ordered_list_open"}:
        return _render_list(tokens, index, end, context, prefix, lines)
    if token.type == "blockquote_open":
        return _render_blockquote(tokens, index, end, context, prefix, lines)
    if token.type in {"fence", "code_block"}:
        _append_code_block(lines, token, context, prefix)
        return index + 1
    if token.type == "table_open":
        return _render_table(tokens, index, end, context, prefix, lines)
    if token.type == "hr":
        _append_wrapped(lines, (("class:tui-muted", "─" * context.width),), context, prefix)
        _append_blank(lines)
        return index + 1
    if token.type == "inline":
        _append_wrapped(lines, _render_inline(token.children or []), context, prefix)
        return index + 1
    if token.content:
        _append_wrapped(lines, (("", token.content),), context, prefix)
    return index + 1


# LLM: heading/paragraph 必须读取紧随其后的 inline token，缺失时 fail-soft 为空块并继续，而非越界。
# 函数用途: 渲染一个标题或段落并跳到对应 close token 之后。
def _render_text_block(
    tokens: list[MarkdownToken],
    index: int,
    end: int,
    context: MarkdownRenderContext,
    prefix: FormattedLine,
    lines: list[FormattedLine],
) -> int:
    opening = tokens[index]
    closing = _matching_close(tokens, index, end)
    inline = next((token for token in tokens[index + 1 : closing] if token.type == "inline"), None)
    if inline is not None:
        base_style = "class:tui-heading" if opening.type == "heading_open" else ""
        _append_wrapped(lines, _render_inline(inline.children or [], base_style=base_style), context, prefix)
    _append_blank(lines)
    return closing + 1


# LLM: list renderer 使用 token nesting 定位 item，序号只取结构化 ordered start，不读取正文前缀。
# 函数用途: 渲染有序或无序列表及其嵌套块。
def _render_list(
    tokens: list[MarkdownToken],
    index: int,
    end: int,
    context: MarkdownRenderContext,
    prefix: FormattedLine,
    lines: list[FormattedLine],
) -> int:
    opening = tokens[index]
    closing = _matching_close(tokens, index, end)
    ordered = opening.type == "ordered_list_open"
    counter = int(opening.attrGet("start") or 1)
    cursor = index + 1
    while cursor < closing:
        if tokens[cursor].type != "list_item_open":
            cursor += 1
            continue
        item_close = _matching_close(tokens, cursor, closing)
        marker = f"{counter}. " if ordered else "- "
        _render_list_item(tokens, cursor + 1, item_close, context, prefix, marker, lines)
        counter += 1
        cursor = item_close + 1
    _append_blank(lines)
    return closing + 1


# LLM: item 第一行使用 marker，视觉换行、后续段落和嵌套列表都使用等宽空白 continuation prefix；序号不能在自动换行时重复。
# 函数用途: 渲染单个列表项内部块。
def _render_list_item(
    tokens: list[MarkdownToken],
    start: int,
    end: int,
    context: MarkdownRenderContext,
    prefix: FormattedLine,
    marker: str,
    lines: list[FormattedLine],
) -> None:
    inline = next((token for token in tokens[start:end] if token.type == "inline"), None)
    item_prefix = prefix + (("", marker),)
    continuation_prefix = prefix + (("", " " * display_width_text(marker)),)
    if inline is not None:
        _append_wrapped(
            lines,
            _render_inline(inline.children or []),
            context,
            item_prefix,
            continuation_prefix=continuation_prefix,
        )
    nested_start = next(
        (position for position in range(start, end) if tokens[position].type.endswith("_list_open")),
        None,
    )
    if nested_start is not None:
        nested_lines = _render_blocks(
            tokens,
            nested_start,
            end,
            context,
            continuation_prefix,
        )
        lines.extend(nested_lines)


# LLM: quote 几何由结构化 blockquote token 产生，正文中的 `>` 字符不会被当成权限或状态。
# 函数用途: 渲染带竖线和缩进的引用块。
def _render_blockquote(
    tokens: list[MarkdownToken],
    index: int,
    end: int,
    context: MarkdownRenderContext,
    prefix: FormattedLine,
    lines: list[FormattedLine],
) -> int:
    closing = _matching_close(tokens, index, end)
    quote_prefix = prefix + (("class:tui-quote-mark", "▎ "),)
    quoted = _render_blocks(tokens, index + 1, closing, context, quote_prefix)
    while quoted and not fragments_text(quoted[-1]):
        quoted.pop()
    lines.extend(quoted)
    _append_blank(lines)
    return closing + 1


# LLM: fenced code 的 lexer 只由显式 info 选择；未知语言回退 TextLexer，代码内容永不执行。
# 函数用途: 语法着色并渲染 fenced/indented code block。
def _append_code_block(
    lines: list[FormattedLine],
    token: MarkdownToken,
    context: MarkdownRenderContext,
    prefix: FormattedLine,
) -> None:
    language = str(token.info or "").strip().split(maxsplit=1)[0] if token.info else ""
    try:
        lexer = get_lexer_by_name(language) if language else TextLexer()
    except ClassNotFound:
        lexer = TextLexer()
    fragments = tuple((_pygments_style(token_type), value) for token_type, value in lex(token.content, lexer))
    for code_line in _split_explicit_lines(fragments):
        _append_wrapped(lines, code_line, context, prefix)
    _append_blank(lines)


# LLM: table 只读取 th/td 内 inline token，列宽按终端显示宽度计算；超宽时交给统一 wrapper，不猜文件类型。
# 函数用途: 将 Markdown table 画成 终端交互 风格 Unicode box。
def _render_table(
    tokens: list[MarkdownToken],
    index: int,
    end: int,
    context: MarkdownRenderContext,
    prefix: FormattedLine,
    lines: list[FormattedLine],
) -> int:
    closing = _matching_close(tokens, index, end)
    rows = _collect_table_rows(tokens, index + 1, closing)
    if not rows:
        return closing + 1
    column_count = max(len(row) for row in rows)
    widths = [0] * column_count
    for row in rows:
        for column, cell in enumerate(row):
            widths[column] = max(widths[column], display_width_fragments(cell))
    _append_table_border(lines, "┌", "┬", "┐", widths, context, prefix)
    for row_index, row in enumerate(rows):
        _append_table_row(lines, row, widths, context, prefix)
        if row_index == 0 and len(rows) > 1:
            _append_table_border(lines, "├", "┼", "┤", widths, context, prefix)
    _append_table_border(lines, "└", "┴", "┘", widths, context, prefix)
    _append_blank(lines)
    return closing + 1


# LLM: row/cell 收集只依据 token type，inline 缺失时保留空 cell，避免列错位。
# 函数用途: 从 table token 区间提取样式 cell 行。
def _collect_table_rows(
    tokens: list[MarkdownToken],
    start: int,
    end: int,
) -> list[list[FormattedLine]]:
    state = _TableCollectionState(rows=[])
    for token in tokens[start:end]:
        _consume_table_token(state, token)
    return state.rows


# LLM: table token 分派使用独立早返回，避免 elif 嵌套随新 token type 增长。
# 函数用途: 将一个 table token 应用到收集状态。
def _consume_table_token(state: _TableCollectionState, token: MarkdownToken) -> None:
    if token.type == "tr_open":
        state.current_row = []
        return
    if token.type in {"th_open", "td_open"}:
        state.in_cell = True
        state.cell_has_content = False
        return
    if token.type == "inline":
        state.cell_has_content = _append_table_cell(state.current_row, state.in_cell, token)
        return
    if token.type in {"th_close", "td_close"}:
        _close_table_cell(state)
        return
    if token.type == "tr_close":
        state.current_row = _finish_table_row(state.rows, state.current_row)


# LLM: cell helper 只在明确 cell scope 内追加 inline fragments，避免 table walker 叠加嵌套判断。
# 函数用途: 将当前 inline token 加到表格行。
def _append_table_cell(
    current_row: list[FormattedLine] | None,
    in_cell: bool,
    token: MarkdownToken,
) -> bool:
    if in_cell and current_row is not None:
        current_row.append(_render_inline(token.children or []))
        return True
    return False


# LLM: 空 cell 在 close 时补一个明确空 fragment，避免后续列左移；状态复位不依赖下一 token。
# 函数用途: 结束当前表格单元格。
def _close_table_cell(state: _TableCollectionState) -> None:
    if state.in_cell and not state.cell_has_content and state.current_row is not None:
        state.current_row.append(())
    state.in_cell = False
    state.cell_has_content = False


# LLM: 行终结只接受已开启 row；无 row 的异常 close 被忽略且不会污染下一行。
# 函数用途: 将当前表格行提交到 rows 并清空 current 指针。
def _finish_table_row(
    rows: list[list[FormattedLine]],
    current_row: list[FormattedLine] | None,
) -> None:
    if current_row is not None:
        rows.append(current_row)
    return None


# LLM: inline renderer 用显式 token stack 合并 strong/em/link 样式；未知 token 仅在有 content 时显示正文。
# 函数用途: 把一组 inline token 转成未换行 fragments。
def _render_inline(tokens: list[MarkdownToken], *, base_style: str = "") -> FormattedLine:
    fragments: list[Fragment] = []
    style_stack: list[str] = [base_style] if base_style else []
    for token in tokens:
        _render_inline_token(token, style_stack, fragments)
    return tuple(_merge_fragments(fragments))


# LLM: inline token 处理与 token 循环分离，close 只弹当前样式栈且不会解析 content 语义。
# 函数用途: 把一个 inline token 应用到样式栈或输出 fragments。
def _render_inline_token(
    token: MarkdownToken,
    style_stack: list[str],
    fragments: list[Fragment],
) -> None:
    if token.type in {"strong_open", "em_open", "link_open", "s_open"}:
        style_stack.append(_inline_open_style(token.type))
        return
    if token.type in {"strong_close", "em_close", "link_close", "s_close"}:
        _pop_inline_style(style_stack)
        return
    if token.type == "code_inline":
        fragments.append(("class:tui-code-inline", token.content))
        return
    if token.type in {"softbreak", "hardbreak"}:
        fragments.append(("", "\n"))
        return
    if token.type == "image":
        fragments.append((_joined_style(style_stack), token.content or "[image]"))
        return
    if token.content:
        fragments.append((_joined_style(style_stack), token.content))


# LLM: style pop 是显式有界操作，坏 token 流的多余 close 不得抛异常或弹出不存在层级。
# 函数用途: 安全结束一层 inline 样式。
def _pop_inline_style(style_stack: list[str]) -> None:
    if style_stack:
        style_stack.pop()


# LLM: open token 到 style role 的映射是纯展示协议，不能携带 URL 或执行行为。
# 函数用途: 返回 strong/em/link/strike 的样式类。
def _inline_open_style(token_type: str) -> str:
    return {
        "strong_open": "class:tui-strong",
        "em_open": "class:tui-em",
        "link_open": "class:tui-link",
        "s_open": "class:tui-strike",
    }.get(token_type, "")


# LLM: pygments token 映射保持小型稳定角色表，具体终端颜色由主题控制。
# 函数用途: 选择代码 token 的 TUI 样式类。
def _pygments_style(token_type: Any) -> str:
    if token_type in Generic.Inserted:
        return "class:tui-diff-add"
    if token_type in Generic.Deleted:
        return "class:tui-diff-remove"
    if token_type in {Generic.Heading, Generic.Subheading}:
        return "class:tui-diff-header"
    if token_type in String:
        return "class:tui-code-string"
    if token_type in Keyword:
        return "class:tui-code-keyword"
    if token_type in Comment:
        return "class:tui-code-comment"
    if token_type in Number:
        return "class:tui-code-number"
    if token_type in Name.Builtin:
        return "class:tui-code-builtin"
    if token_type in Operator:
        return "class:tui-code-operator"
    if token_type in Punctuation:
        return "class:tui-code-punctuation"
    return ""


# LLM: table border 使用计算列宽构造，随后仍走统一宽度 wrapper，窄屏不会写越界。
# 函数用途: 追加一行 Unicode 表格边框。
def _append_table_border(
    lines: list[FormattedLine],
    left: str,
    middle: str,
    right: str,
    widths: list[int],
    context: MarkdownRenderContext,
    prefix: FormattedLine,
) -> None:
    text = left + middle.join("─" * (width + 2) for width in widths) + right
    _append_wrapped(lines, (("", text),), context, prefix)


# LLM: table row padding 按显示列而非 len，中文和 emoji 不得挤坏右边框。
# 函数用途: 追加一行带样式 cell 的 Unicode 表格内容。
def _append_table_row(
    lines: list[FormattedLine],
    row: list[FormattedLine],
    widths: list[int],
    context: MarkdownRenderContext,
    prefix: FormattedLine,
) -> None:
    fragments: list[Fragment] = [("", "│")]
    for index, width in enumerate(widths):
        cell = row[index] if index < len(row) else ()
        fragments.append(("", " "))
        fragments.extend(cell)
        fragments.append(("", " " * (width - display_width_fragments(cell) + 1)))
        fragments.append(("", "│"))
    _append_wrapped(lines, tuple(fragments), context, prefix)


# LLM: cluster 宽度只由 cluster 文本决定，缓存命中不会改变几何；maxsize 是有界内存约束，
# 淘汰只影响速度。emoji ZWJ 序列与含组合符的 cluster 也会入缓存，因此键必须保留完整 cluster 文本。
# 函数用途: 带缓存的 cluster 显示宽度计算。
@lru_cache(maxsize=_CLUSTER_WIDTH_ENTRIES)
def _cluster_width(cluster: str) -> int:
    return max(0, wcswidth(cluster))


# LLM: 只在"文本里没有 ZWJ"时使用：此时可打印 ASCII 字符必然自成 cluster（能并入前一 cluster 的
# 只有组合符/变体选择符/emoji 修饰符/ZWJ/区域码配对，它们全都不是 ASCII），因此可以按段批处理。
# 命中该正则的连续 ASCII 段宽度恒等于字符数，未命中的段落仍交给 _graphemes 做完整聚类。
_PLAIN_ASCII_RUN = re.compile(r"[ -~]+")


# LLM: 与 _graphemes 的 joins 判定同源：这些字符会把前一个字符并进同一 cluster，因此不能作为
# "可批量 ASCII 段"的结束位置。ZWJ 与区域码配对不在此列——前者会整体退回通用聚类，后者要求两侧
# 都是区域码，而 ASCII 段里不含区域码。
# 函数用途: 判断一个字符是否会与前一字符组成同一个 grapheme cluster。
def _joins_previous_cluster(char: str) -> bool:
    codepoint = ord(char)
    return bool(
        unicodedata.combining(char)
        or 0xFE00 <= codepoint <= 0xFE0F
        or 0x1F3FB <= codepoint <= 0x1F3FF
    )


# LLM: 把一段文本切成"可打印 ASCII 连续段"与"其余段落"交替的序列，供 _append_wrapped 批处理；
# 切分只允许落在真实 cluster 边界上（段尾 ASCII 字符若会被后面的组合符并入 cluster，就交回通用段落），
# 因此顺序、内容与聚类结果都和整段交给 _graphemes 时一致。
# 函数用途: 为换行器产出 (文本, 是否为纯可打印 ASCII 段) 序列。
def _iter_chunk_parts(text: str):
    position = 0
    for match in _PLAIN_ASCII_RUN.finditer(text):
        start, end = match.start(), match.end()
        if end < len(text) and _joins_previous_cluster(text[end]):
            end -= 1
        if end <= start:
            continue
        if start > position:
            yield text[position:start], False
        yield text[start:end], True
        position = end
    if position < len(text):
        yield text[position:], False


# LLM: wrapper 以 grapheme cluster 和 wcswidth 计算列，显式 newline 强制换行；控制字符宽度按零处理。
# 纯可打印 ASCII 段落走整段快路径，它与逐 cluster 循环逐字等价：这些字符各自成 cluster、宽度恒为 1、
# 整段放得下时循环里的换行/丢空白分支都不会命中，追加后由行尾 _merge_fragments 合并回同一样式。
# 出现 ZWJ 时 ASCII 可能被并入前一 cluster，因此整段退回通用聚类，保证与旧实现完全一致。
# 函数用途: 将一组 fragments 包装成不超过 context.width 的多行并追加到结果。
def _append_wrapped(
    lines: list[FormattedLine],
    fragments: FormattedLine,
    context: MarkdownRenderContext,
    prefix: FormattedLine,
    *,
    continuation_prefix: FormattedLine | None = None,
) -> None:
    next_prefix = prefix if continuation_prefix is None else continuation_prefix
    prefix_width = display_width_fragments(prefix)
    current: list[Fragment] = list(prefix)
    current_width = prefix_width
    for style, text in fragments:
        normalized = str(text).replace("\t", "    ")
        if not normalized:
            continue
        if "\u200d" in normalized:
            parts = ((normalized, False),)
        else:
            parts = _iter_chunk_parts(normalized)
        for part, is_plain_ascii in parts:
            if is_plain_ascii and current_width + len(part) <= context.width:
                current.append((style, part))
                current_width += len(part)
                continue
            for cluster in _graphemes(part):
                if cluster == "\n":
                    lines.append(tuple(_merge_fragments(current)))
                    current = list(next_prefix)
                    current_width = display_width_fragments(next_prefix)
                    prefix_width = current_width
                    continue
                cluster_width = _cluster_width(cluster)
                # 换行判定与"换行点空白丢弃"合并成同一层分支，保持与历史实现相同的嵌套深度和语义：
                # 一旦在此换行，落在行首的空白 cluster 不写入。
                wraps = current_width > prefix_width and current_width + cluster_width > context.width
                if wraps:
                    lines.append(tuple(_merge_fragments(current)))
                    current = list(next_prefix)
                    current_width = display_width_fragments(next_prefix)
                    prefix_width = current_width
                if not (wraps and cluster.isspace()):
                    current.append((style, cluster))
                    current_width += cluster_width
    lines.append(tuple(_merge_fragments(current)))


# LLM: grapheme 近似覆盖 combining、variation selector、emoji modifier、ZWJ 和国旗区域码，宽度最终由 wcswidth 裁决。
# _append_wrapped 直接以它迭代"已展开 tab"的文本；删除调用方时不要退回按 UTF-16 码位切分。
# 函数用途: 将文本拆成不会把常见 emoji/组合字符从中间断开的单元。
def _graphemes(text: str):
    cluster = ""
    regional_count = 0
    for char in text:
        if char == "\n":
            if cluster:
                yield cluster
                cluster = ""
            regional_count = 0
            yield char
            continue
        codepoint = ord(char)
        regional = 0x1F1E6 <= codepoint <= 0x1F1FF
        joins = bool(
            cluster
            and (
                unicodedata.combining(char)
                or 0xFE00 <= codepoint <= 0xFE0F
                or 0x1F3FB <= codepoint <= 0x1F3FF
                or char == "\u200d"
                or cluster.endswith("\u200d")
                or (regional and regional_count == 1)
            )
        )
        if cluster and not joins:
            yield cluster
            cluster = ""
            regional_count = 0
        cluster += char
        regional_count = regional_count + 1 if regional else 0
    if cluster:
        yield cluster


# LLM: explicit line splitter 保持样式跨行，且只删除 lexer 末尾多出的单个空行。
# 函数用途: 将含 newline 的代码 fragments 拆成逻辑行。
def _split_explicit_lines(fragments: FormattedLine) -> list[FormattedLine]:
    lines: list[list[Fragment]] = [[]]
    for style, text in fragments:
        parts = str(text).split("\n")
        for index, part in enumerate(parts):
            if part:
                lines[-1].append((style, part))
            if index < len(parts) - 1:
                lines.append([])
    if len(lines) > 1 and not lines[-1]:
        lines.pop()
    return [tuple(_merge_fragments(line)) for line in lines]


# LLM: matching close 只按 markdown-it nesting 累计，遇到坏 token 流返回区间末端的安全位置。
# 函数用途: 查找 opening token 对应 close token 的索引。
def _matching_close(tokens: list[MarkdownToken], opening_index: int, end: int) -> int:
    level = 0
    opening_type = tokens[opening_index].type
    close_type = opening_type.removesuffix("_open") + "_close"
    for index in range(opening_index, end):
        token = tokens[index]
        if token.type == opening_type:
            level += 1
        elif token.type == close_type:
            level -= 1
            if level == 0:
                return index
    return max(opening_index, end - 1)


# LLM: blank 合并避免多个 block close 产生无界空行，同时保留 终端交互 块间单空行。
# 函数用途: 在结果末尾最多追加一个空行。
def _append_blank(lines: list[FormattedLine]) -> None:
    if not lines or fragments_text(lines[-1]):
        lines.append(())


# LLM: 相邻同 style fragment 合并减少 prompt_toolkit 渲染对象，不改变字符或列宽。
# 函数用途: 压缩一行 fragment 序列。
def _merge_fragments(fragments: list[Fragment]) -> list[Fragment]:
    merged: list[Fragment] = []
    for style, text in fragments:
        if not text:
            continue
        if merged and merged[-1][0] == style:
            merged[-1] = (style, merged[-1][1] + text)
        else:
            merged.append((style, text))
    return merged


# LLM: style stack 只拼 prompt_toolkit class 标记，空样式不会制造多余空格。
# 函数用途: 合并 inline 基础与嵌套样式。
def _joined_style(styles: list[str]) -> str:
    return " ".join(style for style in styles if style)


# LLM: 显示宽度使用 wcswidth 完整字符串算法，负值控制字符按零宽处理。
# 函数用途: 计算普通文本占用的终端列数。
def display_width_text(text: str) -> int:
    return max(0, wcswidth(str(text)))


# LLM: fragment 宽度只累加文本显示列，style 名称绝不影响几何。
# 函数用途: 计算一组样式片段占用的终端列数。
def display_width_fragments(fragments: FormattedLine) -> int:
    return sum(display_width_text(text) for _style, text in fragments)


# LLM: 人读/测试投影只拼文本，不解析或移除样式中的语义。
# 函数用途: 返回一行 fragments 的纯文本。
def fragments_text(fragments: FormattedLine) -> str:
    return "".join(text for _style, text in fragments)


__all__ = [
    "FormattedLine",
    "Fragment",
    "MarkdownRenderContext",
    "display_width_fragments",
    "display_width_text",
    "fragments_text",
    "render_markdown",
    "sanitize_terminal_text",
    "wrap_fragments",
]
