"""会话运行时 风格对话流的行前缀 Lexer。

用户行 `> ` 青色、助手 marker `⏺` magenta(其后正文默认色)、统计行 `⟿`
灰色——与 会话运行时-rs styles.md 的配色约定一致。TextArea 通过 lexer 参数接入,
样式名按 prompt_toolkit 惯例用 "class:" 前缀, 在 _make_tui_style 注册。
"""

from __future__ import annotations

from prompt_toolkit.lexers import Lexer


def transcript_line_fragments(line: str) -> list[tuple[str, str]]:
    """单行 → (style, text) 片段; 独立函数便于单测。"""
    if line.startswith("> "):
        return [("class:user-prompt", line)]
    if line.startswith("⏺"):
        return [("class:assistant-marker", "⏺"), ("", line[1:])]
    if line.startswith("⟿"):
        return [("class:footer", line)]
    return [("", line)]


class TranscriptLexer(Lexer):
    def lex_document(self, document):
        def get_line(lineno: int) -> list[tuple[str, str]]:
            return transcript_line_fragments(document.lines[lineno])

        return get_line
