# LLM: Static site HTML parsing is isolated from validation decisions.
# 模块用途: 使用标准库 HTMLParser 提取本地资源引用、控件、DOM id 和表单 id，不执行页面代码。

from __future__ import annotations

from html.parser import HTMLParser


# LLM: StaticSiteHTMLParser extracts only refs and simple controls from one HTML file.
# 类用途: 使用标准库 HTMLParser 扫描 href/src/action 和按钮/链接控件，不执行网页代码。
class StaticSiteHTMLParser(HTMLParser):
    """Extract local refs and basic controls from HTML."""

    # LLM: __init__ stores per-file scanner state and avoids retaining large body text.
    # 函数用途: 初始化单个 HTML 文件的扫描容器；只保留 refs 和控件摘要。
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[tuple[str, str]] = []
        self.controls: list[dict[str, object]] = []
        self.element_ids: list[str] = []
        self.form_ids: list[str] = []
        self._current_control: dict[str, object] | None = None

    # LLM: handle_starttag records href/src/action refs and simple clickable controls.
    # 函数用途: 收集本地资源引用和可点击控件，后续判断是否失效或明显无动作。
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        for ref_attr in ("href", "src", "action"):
            if attr_map.get(ref_attr):
                self.refs.append((ref_attr, attr_map[ref_attr]))
        if attr_map.get("id"):
            self.element_ids.append(attr_map["id"])
        if tag == "form" and attr_map.get("id"):
            self.form_ids.append(attr_map["id"])
        if tag in {"button", "a"}:
            self._current_control = {
                "tag": tag,
                "href": attr_map.get("href", ""),
                "onclick": attr_map.get("onclick", ""),
                "type": attr_map.get("type", ""),
                "text": "",
            }

    # LLM: handle_data keeps only short button/link text for diagnostics.
    # 函数用途: 给控件问题生成可读摘要；不会保存完整页面正文。
    def handle_data(self, data: str) -> None:
        if self._current_control is not None:
            text = str(self._current_control.get("text") or "")
            self._current_control["text"] = (text + data).strip()[:80]

    # LLM: handle_endtag closes one simple control candidate.
    # 函数用途: 结束 button/a 扫描并把控件摘要放入列表。
    def handle_endtag(self, tag: str) -> None:
        if self._current_control and self._current_control.get("tag") == tag:
            self.controls.append(dict(self._current_control))
            self._current_control = None
