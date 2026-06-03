
from __future__ import annotations

from typing import Any


def drop_non_executable_model_checklist_items(tests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in tests if not _non_executable_model_checklist_item(item)]


def _non_executable_model_checklist_item(item: dict[str, Any]) -> bool:
    method = str(item.get("validation_method") or "command").strip().lower() or "command"
    if method == "command":
        return not str(item.get("command") or "").strip()
    if method == "file_check":
        return not str(item.get("file_path") or "").strip()
    if method == "content_check":
        return not str(item.get("file_path") or "").strip() or not _content_pattern_value(item)
    if method == "static_site_check":
        return not str(item.get("site_root") or "").strip()
    return False


def _content_pattern_value(item: dict[str, Any]) -> str:
    for key in ("content_equals", "expected_content", "content_pattern"):
        value = str(item.get(key) or "")
        if value:
            return value
    return ""
