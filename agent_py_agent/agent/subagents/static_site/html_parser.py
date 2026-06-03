
from __future__ import annotations

from html.parser import HTMLParser


class StaticSiteHTMLParser(HTMLParser):
    """Extract local refs and basic controls from HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[tuple[str, str]] = []
        self.controls: list[dict[str, object]] = []
        self.element_ids: list[str] = []
        self.form_ids: list[str] = []
        self._current_control: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        for ref_attr in ("href", "src", "action"):
            if attr_map.get(ref_attr):
                self.refs.append((ref_attr, attr_map[ref_attr]))
        if attr_map.get("id"):
            self.element_ids.append(attr_map["id"])
        if tag == "form" and attr_map.get("id"):
            self.form_ids.append(attr_map["id"])
        if tag in {"button", "a", "input", "select", "textarea"}:
            control = {
                "tag": tag,
                "href": attr_map.get("href", ""),
                "onclick": attr_map.get("onclick", ""),
                "type": attr_map.get("type", ""),
                "disabled": "disabled" in attr_map,
                "text": "",
            }
            if tag == "input":
                control["text"] = attr_map.get("aria-label") or attr_map.get("placeholder") or attr_map.get("value") or ""
                self.controls.append(control)
                return
            self._current_control = control

    def handle_data(self, data: str) -> None:
        if self._current_control is not None:
            text = str(self._current_control.get("text") or "")
            self._current_control["text"] = (text + data).strip()[:80]

    def handle_endtag(self, tag: str) -> None:
        if self._current_control and self._current_control.get("tag") == tag:
            self.controls.append(dict(self._current_control))
            self._current_control = None
