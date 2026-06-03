
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


def is_html_response(headers: Any) -> bool:
    return "html" in str(headers.get("Content-Type", "")).lower()


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


def visible_html_text(body: str) -> str:
    parser = _VisibleHtmlParser()
    try:
        parser.feed(body)
        parser.close()
    except Exception:
        return ""
    return re.sub(r"[ \t\r\f\v]+", " ", "\n".join(parser.parts)).strip()


class _VisibleHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

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

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in _HTML_SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if not self._skip_depth and normalized in _HTML_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text)


def _attr_value(attrs: list[tuple[str, str | None]], name: str) -> str:
    for key, value in attrs:
        if key.lower() == name and value:
            return value
    return ""
