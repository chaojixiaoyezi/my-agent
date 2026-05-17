# LLM: Checklist filtering keeps inferred machine tests from being masked by model-only assertions.
# 模块用途: 过滤缺字段、无法执行的模型自报测试清单；只有已有机器兜底时才由调用方使用。

from __future__ import annotations

from typing import Any


# LLM: drop_non_executable_model_checklist_items removes schema-shaped assertions that cannot execute.
# 函数用途: 有静态站点或内容机器验收兜底时，丢弃缺必要字段的模型空壳测试项。
def drop_non_executable_model_checklist_items(tests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in tests if not _non_executable_model_checklist_item(item)]


# LLM: _non_executable_model_checklist_item recognizes assertions missing the fields needed by TestExecutor.
# 函数用途: 判断测试项是否只是模型清单文字而非可执行检查；没有兜底时调用方仍应保留它。
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


# LLM: _content_pattern_value mirrors executor pattern aliases without importing executor internals.
# 函数用途: 判断 content_check 是否提供了可执行的字面匹配内容，支持旧字段和 v2 字段。
def _content_pattern_value(item: dict[str, Any]) -> str:
    for key in ("content_equals", "expected_content", "content_pattern"):
        value = str(item.get(key) or "")
        if value:
            return value
    return ""
