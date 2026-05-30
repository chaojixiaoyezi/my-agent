# LLM: HTML preview helpers keep web.py small and make web_fetch output useful for large pages.
# 模块用途: 从 HTML 工具结果中提取可见正文预览，并保留短 raw HTML 片段用于排查。

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

_HTML_PREVIEW_RAW_MAX_CHARS = 4_000
_HTML_SKIP_TAGS = {"head", "script", "style", "noscript", "svg"}
_HTML_BLOCK_TAGS = {
    "article",
    "br",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "main",
    "p",
    "section",
    "table",
    "td",
    "th",
    "tr",
}


# LLM: is_html_response is a deterministic content-type check, not a model judgment.
# 函数用途: 判断 HTTP 响应是否应按 HTML 提取可见正文。
def is_html_response(headers: Any) -> bool:
    return "html" in str(headers.get("Content-Type", "")).lower()


# LLM: format_html_response exposes visible body text before raw markup so large heads do not hide useful data.
# 函数用途: 将网页响应渲染为可读正文预览 + 短 raw 预览，避免模型反复读取同一大 HTML artifact。
def format_html_response(*, status: int, headers: Any, body: str, max_chars: int) -> str:
    raw_preview_chars = min(max_chars, _HTML_PREVIEW_RAW_MAX_CHARS)
    visible = visible_html_text(body)
    body_truncated = len(body) > raw_preview_chars
    visible_truncated = len(visible) > max_chars
    lines = [
        f"status={status}",
        f"content_type={headers.get('Content-Type', '')}",
        f"body_chars={len(body)}",
        f"body_truncated={str(body_truncated).lower()}",
        "",
        "visible_text_preview:",
        visible[:max_chars],
    ]
    if visible_truncated:
        lines.append("... visible_text_truncated")
    lines.extend(["", "raw_html_preview:", body[:raw_preview_chars]])
    if body_truncated:
        lines.append("... 已截断")
    return "\n".join(lines)


# LLM: visible_html_text keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def visible_html_text(body: str) -> str:
    parser = _VisibleHtmlParser()
    try:
        parser.feed(body)
        parser.close()
    except Exception:
        return ""
    return re.sub(r"[ \t\r\f\v]+", " ", "\n".join(parser.parts)).strip()


# LLM: _VisibleHtmlParser extracts text and hrefs without executing page code.
# 类用途: 从 HTML 中提取可见文本和链接引用，供工具结果预览使用。
class _VisibleHtmlParser(HTMLParser):
    # LLM: __init__ initializes parser state without reading HTML text as a runtime fact.
    # 函数用途: 初始化可见文本片段和跳过深度，供后续标签回调按结构化状态更新。
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    # LLM: handle_starttag keeps this runtime helper grounded in structured fields.
    # 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized in _HTML_SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if normalized in _HTML_BLOCK_TAGS:
            self.parts.append("\n")
        if normalized == "a":
            href = _attr_value(attrs, "href")
            if href:
                self.parts.append(f" [href={href}] ")

    # LLM: handle_endtag keeps this runtime helper grounded in structured fields.
    # 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in _HTML_SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if not self._skip_depth and normalized in _HTML_BLOCK_TAGS:
            self.parts.append("\n")

    # LLM: handle_data keeps this runtime helper grounded in structured fields.
    # 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text)


# LLM: _attr_value keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _attr_value(attrs: list[tuple[str, str | None]], name: str) -> str:
    for key, value in attrs:
        if key.lower() == name and value:
            return value
    return ""
