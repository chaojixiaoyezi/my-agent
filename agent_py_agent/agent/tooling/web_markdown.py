# LLM: web_markdown keeps lightweight HTML-to-Markdown conversion out of network tool entrypoints.
# 模块用途: 将网页 HTML 的标题、段落、列表和链接转成模型可读预览，不参与网络请求和安全判断。

from __future__ import annotations

import re
from html.parser import HTMLParser

from .web_html_preview import visible_html_text

_SKIP_TAGS = {"script", "style", "noscript", "svg", "head"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BREAK_TAGS = {"p", "div", "section", "article", "main", "tr", "br"}
_BLOCK_END_TAGS = _HEADING_TAGS | {"p", "div", "section", "article", "main", "li", "tr"}


def html_to_markdown(body: str) -> str:
    parser = MarkdownHtmlParser()
    try:
        parser.feed(body)
        parser.close()
    except Exception:
        return visible_html_text(body)
    return parser.render()


class MarkdownHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._link_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        self._handle_visible_starttag(tag, attrs)

    def _handle_visible_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        prefix = _markdown_tag_prefix(tag)
        if prefix:
            self.parts.append(prefix)
        if tag == "a":
            self._link_stack.append(attr_value(attrs, "href"))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "a" and self._link_stack:
            self._append_link_suffix()
        elif tag in _BLOCK_END_TAGS:
            self.parts.append("\n")

    def _append_link_suffix(self) -> None:
        href = self._link_stack.pop()
        if href:
            self.parts.append(f"]({href})")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if text:
            self.parts.append(f"[{text}" if self._link_stack else text)

    def render(self) -> str:
        text = re.sub(r"\n{3,}", "\n\n", "".join(self.parts))
        return re.sub(r"[ \t]{2,}", " ", text).strip()


def attr_value(attrs: list[tuple[str, str | None]], name: str) -> str:
    for key, value in attrs:
        if key.lower() == name and value:
            return value
    return ""


def _markdown_tag_prefix(tag: str) -> str:
    if tag in _HEADING_TAGS:
        return "\n" + "#" * int(tag[1]) + " "
    if tag in _BREAK_TAGS:
        return "\n"
    if tag == "li":
        return "\n- "
    return ""


__all__ = ["html_to_markdown"]
