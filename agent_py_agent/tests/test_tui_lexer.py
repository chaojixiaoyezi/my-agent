from __future__ import annotations

"""会话运行时 风格对话流行前缀 Lexer 的单元测试。"""

from agent_py_agent.cli.chat_parts.tui_lexer import transcript_line_fragments


def test_user_line_is_cyan():
    assert transcript_line_fragments("> 帮我写个脚本") == [
        ("class:user-prompt", "> 帮我写个脚本")
    ]


def test_assistant_marker_is_magenta_rest_default():
    assert transcript_line_fragments("⏺ 好的, 这就开始。") == [
        ("class:assistant-marker", "⏺"),
        ("", " 好的, 这就开始。"),
    ]


def test_footer_line_is_dim():
    assert transcript_line_fragments("⟿ 3.2s · 5 轮 · 8.5K ctx") == [
        ("class:footer", "⟿ 3.2s · 5 轮 · 8.5K ctx")
    ]


def test_plain_line_has_no_style():
    assert transcript_line_fragments("普通正文") == [("", "普通正文")]
