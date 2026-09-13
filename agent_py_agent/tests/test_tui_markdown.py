from __future__ import annotations

import random
import unicodedata

from wcwidth import wcswidth

from agent_py_agent.cli.chat_parts.tui_markdown import (
    MarkdownRenderContext,
    _cluster_width,
    _graphemes,
    _iter_chunk_parts,
    _strip_terminal_controls,
    display_width_fragments,
    fragments_text,
    render_markdown,
    wrap_fragments,
)


def _texts(lines) -> list[str]:
    return [fragments_text(line) for line in lines]


# LLM: 这是优化前的逐 cluster 换行实现，只作为等价性基准存在于测试中；
# 产品代码若再改换行器，必须让 wrap_fragments 继续与它逐字节一致，否则需要显式更新契约。
# 函数用途: 复刻历史实现，供等价性对照使用。
def _reference_merge(fragments):
    merged = []
    for style, text in fragments:
        if not text:
            continue
        if merged and merged[-1][0] == style:
            merged[-1] = (style, merged[-1][1] + text)
        else:
            merged.append((style, text))
    return merged


def _reference_wrap(fragments, *, width, first_prefix=(), continuation_prefix=None):
    next_prefix = first_prefix if continuation_prefix is None else continuation_prefix
    prefix_width = display_width_fragments(first_prefix)
    current = list(first_prefix)
    current_width = prefix_width
    lines = []
    for style, text in fragments:
        normalized = str(text).replace("\t", "    ")
        for cluster in _graphemes(normalized):
            if cluster == "\n":
                lines.append(tuple(_reference_merge(current)))
                current = list(next_prefix)
                current_width = display_width_fragments(next_prefix)
                prefix_width = current_width
                continue
            cluster_width = max(0, wcswidth(cluster))
            if current_width > prefix_width and current_width + cluster_width > width:
                lines.append(tuple(_reference_merge(current)))
                current = list(next_prefix)
                current_width = display_width_fragments(next_prefix)
                prefix_width = current_width
                if cluster.isspace():
                    continue
            current.append((style, cluster))
            current_width += cluster_width
    lines.append(tuple(_reference_merge(current)))
    return tuple(lines)


# LLM: 语料必须覆盖会走快路径的纯 ASCII、混合中英、换行、tab、组合符、VS16、ZWJ、区域码与控制字符，
# 否则等价性用例会漏掉真实分歧；字符集本身可以随新发现的风险字符扩充。
_ALPHABET = (
    "abcXYZ019 \t\n`*_[]()!<>#>-+|~=\\\"'&$%^:;,.?/@{}"
    "，。：；、（）「」“”…—中文宽字符测试你好终端"
    "\u0301\u200d\ufe0f\U0001f3fb"
    "👩\u200d💻🇨🇳😀"
    "\x00\x1b\x7f\x9b\ud800"
    "\u00a0\u2028\u3000"
)


def _fuzz_texts(count: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    texts = ["".join(rng.choice(_ALPHABET) for _ in range(rng.randrange(0, 40))) for _ in range(count)]
    for _ in range(count // 4):
        texts.append("\n".join(
            "".join(rng.choice(_ALPHABET) for _ in range(rng.randrange(0, 20)))
            for _ in range(rng.randrange(1, 6))
        ))
    return texts


def test_wrap_matches_pre_optimization_reference_on_corpus() -> None:
    texts = [
        "", " ", "a", "hello world", "中文宽字符测试内容", "a\tb", "a\nb", "a\n\nb",
        "👩\u200d💻 组合 e\u0301 🇨🇳", "\x1b[2J control \x9b", "line " * 40, "中" * 60, "x" * 200,
        "a b\tc\nd", "a\u0301b", "|\ufe0f你", "abc\ufe0fdef", "🇦🇧🇨abc", "读取 config_1.yaml 后写入 report_1.md。",
    ] + _fuzz_texts(120, 20240617)
    prefixes = [
        ((), None),
        ((("class:m", "❯ "),), (("class:m", "  "),)),
        ((("class:m", ""),), (("class:m", ""),)),
        ((("class:a", "ab"), ("class:b", "cd")), None),
    ]
    for text in texts:
        for width in (1, 2, 3, 5, 6, 8, 13, 20, 40, 79, 120):
            for first, continuation in prefixes:
                fragments = (("class:body", text),)
                assert wrap_fragments(
                    fragments, width=width, first_prefix=first, continuation_prefix=continuation
                ) == _reference_wrap(
                    fragments, width=width, first_prefix=first, continuation_prefix=continuation
                ), (text, width, first, continuation)


def test_wrap_golden_lines_for_ascii_cjk_and_control_inputs() -> None:
    # 这些 golden 行取自优化前的实现；同输入同宽度必须继续逐字节一致（样式、顺序、尾随空格都不能变）。
    assert wrap_fragments((("class:body", "hello world"),), width=20) == (
        (("class:body", "hello world"),),
    )
    assert wrap_fragments((("class:body", "hello world wrap me please"),), width=10) == (
        (("class:body", "hello worl"),),
        (("class:body", "d wrap me "),),
        (("class:body", "please"),),
    )
    assert wrap_fragments((("class:body", "读取 config_1.yaml 后写入 report_1.md。"),), width=20) == (
        (("class:body", "读取 config_1.yaml "),),
        (("class:body", "后写入 report_1.md。"),),
    )
    assert wrap_fragments((("class:body", "中文宽字符 abcd efgh"),), width=6) == (
        (("class:body", "中文宽"),),
        (("class:body", "字符 a"),),
        (("class:body", "bcd ef"),),
        (("class:body", "gh"),),
    )
    assert wrap_fragments((("class:body", "a\tb"),), width=8) == ((("class:body", "a    b"),),)
    assert wrap_fragments((("class:body", "line1\nline2"),), width=20) == (
        (("class:body", "line1"),),
        (("class:body", "line2"),),
    )
    # 组合符与 VS16 必须留在拉丁基字符的同一行，不能被批处理从中间切开。
    combining = wrap_fragments((("class:body", "a\u0301 b\u0301c"),), width=3)
    assert [fragments_text(line) for line in combining] == ["a\u0301 b\u0301", "c"]
    assert wrap_fragments((("class:body", "|\ufe0f你"),), width=1) == (
        (("class:body", "|\ufe0f"),),
        (("class:body", "你"),),
    )
    markdown_lines = render_markdown(
        "读取 `config_1.yaml` 后写入 report_1.md。", MarkdownRenderContext(width=18)
    )
    assert [[list(fragment) for fragment in line] for line in markdown_lines] == [
        [["", "读取 "], ["class:tui-code-inline", "config_1.yaml"]],
        [["", "后写入 report_1.md"]],
        [["", "。"]],
    ]


def test_chunk_partition_keeps_grapheme_boundaries() -> None:
    # 快路径的前提：切片只能落在真实 cluster 边界上，且拼回去必须等于原文。
    texts = [
        "abc", "中文abc", "abc中", "a\u0301b", "abc\ufe0fdef", "|\ufe0f你", "🇦🇧🇨abc",
        "读取 config_1.yaml 后写入 report_1.md。",
    ] + _fuzz_texts(80, 9091)
    for text in texts:
        normalized = text.replace("\t", "    ")
        if "\u200d" in normalized:
            continue
        parts = list(_iter_chunk_parts(normalized))
        assert "".join(part for part, _is_plain in parts) == normalized
        expected = list(_graphemes(normalized))
        actual = [cluster for part, _is_plain in parts for cluster in _graphemes(part)]
        assert actual == expected, (text, parts)


def test_cluster_width_cache_matches_wcswidth() -> None:
    for cluster in ("a", " ", "中", "\n", "\x1b", "e\u0301", "👩\u200d💻", "🇨🇳", "|\ufe0f", ""):
        assert _cluster_width(cluster) == max(0, wcswidth(cluster))
    assert _cluster_width("中") == 2


def test_sanitizer_matches_category_rule_on_control_ranges() -> None:
    # translate 表必须与"删除 Cc/Cs、保留 \n 与 \t"的逐字符规则完全等价。
    def legacy(value: str) -> str:
        kept = []
        for char in value:
            if char in {"\n", "\t"}:
                kept.append(char)
                continue
            if unicodedata.category(char) in {"Cc", "Cs"}:
                continue
            kept.append(char)
        return "".join(kept)

    samples = [
        "".join(chr(codepoint) for codepoint in range(0x00, 0x300)),
        "".join(chr(codepoint) for codepoint in range(0x7F, 0xA1)),
        "".join(chr(codepoint) for codepoint in range(0xD7F0, 0xE010)),
        "".join(chr(codepoint) for codepoint in range(0xFFF0, 0x10010)),
        "中文\t\n👩\u200d💻\x1b]0;x\x07",
    ]
    for sample in samples:
        assert _strip_terminal_controls(sample) == legacy(sample)


def test_markdown_fixture_matches_free_code_block_geometry_and_roles() -> None:
    markdown = """## Fixture 标题

- 中文宽字符：你好，终端
- **粗体**、`inline_code()` 与 https://example.com

> *这是引用行。*

```python
print("fixture")
```

| 列 A | 列 B |
|---|---|
| 1 | 二 |
"""
    lines = render_markdown(markdown, MarkdownRenderContext(width=118))
    texts = _texts(lines)
    assert texts == [
        "Fixture 标题",
        "",
        "- 中文宽字符：你好，终端",
        "- 粗体、inline_code() 与 https://example.com",
        "",
        "▎ 这是引用行。",
        "",
        'print("fixture")',
        "",
        "┌──────┬──────┐",
        "│ 列 A │ 列 B │",
        "├──────┼──────┤",
        "│ 1    │ 二   │",
        "└──────┴──────┘",
    ]
    styles = [(style, text) for line in lines for style, text in line]
    assert ("class:tui-heading", "Fixture 标题") in styles
    assert any("tui-strong" in style and text == "粗体" for style, text in styles)
    assert ("class:tui-code-inline", "inline_code()") in styles
    assert any("tui-em" in style and text == "这是引用行。" for style, text in styles)
    assert any("tui-code-builtin" in style and text == "print" for style, text in styles)
    assert any("tui-code-string" in style and text == '"fixture"' for style, text in styles)


def test_markdown_wrap_uses_terminal_width_for_cjk_combining_emoji_and_prefix() -> None:
    prefix = (("class:prefix", "● "),)
    lines = render_markdown(
        "你好 e\u0301 👩\u200d💻 🇨🇳 end",
        MarkdownRenderContext(width=12, prefix=prefix),
    )
    texts = _texts(lines)
    assert all(display_width_fragments(line) <= 12 for line in lines)
    assert all(text.startswith("● ") for text in texts)
    assert "e\u0301" in "".join(texts)
    assert "👩\u200d💻" in "".join(texts)
    assert "🇨🇳" in "".join(texts)


def test_nested_list_uses_structural_indent_and_ordered_start() -> None:
    lines = render_markdown(
        "3. outer\n   - inner **bold**\n4. next\n",
        MarkdownRenderContext(width=40),
    )
    texts = _texts(lines)
    assert "3. outer" in texts
    assert "   - inner bold" in texts
    assert "4. next" in texts


def test_ordered_list_wrap_indents_continuation_without_repeating_marker() -> None:
    lines = render_markdown(
        "1. 第一项包含足够多的中文字符，需要在窄终端中自动换行。\n"
        "2. 第二项保持独立序号。\n",
        MarkdownRenderContext(width=24),
    )
    texts = _texts(lines)

    assert texts[0].startswith("1. ")
    assert texts[1].startswith("   ")
    assert not texts[1].lstrip().startswith("1. ")
    assert sum(text.startswith("2. ") for text in texts) == 1
    assert all(display_width_fragments(line) <= 24 for line in lines)


def test_unknown_fence_language_falls_back_to_plain_text() -> None:
    lines = render_markdown(
        "```not-a-real-language\nopaque <value>\n```",
        MarkdownRenderContext(width=40),
    )
    assert _texts(lines) == ["opaque <value>"]


def test_diff_fence_uses_structured_insert_delete_and_header_styles() -> None:
    lines = render_markdown(
        "```diff\n--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n```",
        MarkdownRenderContext(width=80),
    )
    styled = [(style, text) for line in lines for style, text in line]

    assert any(
        style == "class:tui-diff-remove" and "-old" in text
        for style, text in styled
    )
    assert any(
        style == "class:tui-diff-add" and "+new" in text
        for style, text in styled
    )
    assert any(
        style == "class:tui-diff-header" and "@@" in text
        for style, text in styled
    )
