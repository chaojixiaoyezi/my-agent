from __future__ import annotations

from agent_py_agent.cli.chat_parts.tui_markdown import (
    MarkdownRenderContext,
    display_width_fragments,
    fragments_text,
    render_markdown,
)


def _texts(lines) -> list[str]:
    return [fragments_text(line) for line in lines]


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
