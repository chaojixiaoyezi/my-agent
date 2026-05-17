# LLM: Required content-line extraction provides a generic machine contract for non-web artifacts.
# 模块用途: 从任务验收文本里提取必须出现在产物中的字面内容行，供父级生成 content_check。

from __future__ import annotations

"""Extract explicit required content lines from task text."""

import json
import re
from typing import Any

_REQUIRED_CONTENT_RE = re.compile(
    r"\brequired_content_(?:lines|texts?)\s*[:=]\s*(.*)",
    re.IGNORECASE,
)


# LLM: required_content_lines_from_texts only accepts explicit structured contracts.
# 函数用途: 从 `required_content_lines: a | b` 或后续项目符号里提取必须出现的字面行；普通自然语言不猜。
def required_content_lines_from_texts(texts: list[object]) -> list[str]:
    """Return explicit required content lines from structured task text."""

    values: list[str] = []
    for value in texts:
        values.extend(_required_lines_from_text(str(value or "")))
    return _dedupe(values)[:100]


# LLM: required_content_lines_for_task centralizes task fields used by parent content checks.
# 函数用途: 从 task 的目标、说明和验收条件里提取 content_check 需要的字面内容行。
def required_content_lines_for_task(task: Any) -> list[str]:
    """Return explicit content-line contracts from a subagent task."""

    return required_content_lines_from_texts([
        getattr(task, "goal", ""),
        getattr(task, "thought", ""),
        getattr(task, "description", ""),
        *(getattr(task, "acceptance_checks", []) or []),
    ])


# LLM: _required_lines_from_text supports inline and simple bullet-list contracts without parsing prose.
# 函数用途: 扫描单段文本里的 required_content_lines 标记；只吃结构化行和紧随其后的项目符号。
def _required_lines_from_text(text: str) -> list[str]:
    lines = text.splitlines()
    values: list[str] = []
    index = 0
    while index < len(lines):
        match = _REQUIRED_CONTENT_RE.search(lines[index])
        if not match:
            index += 1
            continue
        tail = match.group(1).strip()
        if tail:
            values.extend(_inline_items(tail))
            index += 1
            continue
        consumed, bullet_values = _following_bullet_items(lines[index + 1 :])
        values.extend(bullet_values)
        index += consumed + 1
    return values


# LLM: _inline_items splits only on pipes so CSV commas and prose punctuation remain literal content.
# 函数用途: 解析 `a | b` 或 JSON 字符串数组；不会按逗号拆分，避免破坏 CSV 行。
def _inline_items(value: str) -> list[str]:
    parsed = _json_string_items(value)
    if parsed:
        return parsed
    return [_clean_item(item) for item in value.split("|") if _clean_item(item)]


# LLM: _json_string_items lets future structured packets pass exact strings without delimiter escaping.
# 函数用途: 如果值是 JSON 字符串数组，则直接读取数组元素；解析失败就回退到竖线分隔。
def _json_string_items(value: str) -> list[str]:
    if not value.startswith("["):
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [_clean_item(item) for item in payload if _clean_item(item)]


# LLM: _following_bullet_items accepts one compact block after an empty required_content_lines marker.
# 函数用途: 支持 `required_content_lines:` 下一行起用 `- 文本` 写多条字面内容。
def _following_bullet_items(lines: list[str]) -> tuple[int, list[str]]:
    values: list[str] = []
    consumed = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            consumed += 1
            break
        item = _bullet_item(stripped)
        if item is None:
            break
        values.append(item)
        consumed += 1
    return consumed, values


# LLM: _bullet_item keeps only plain list items and strips common Markdown markers.
# 函数用途: 将 `- 文本`、`* 文本`、`1. 文本` 这类项目符号还原为字面内容。
def _bullet_item(value: str) -> str | None:
    match = re.match(r"(?:[-*+]|\d+[.)])\s+(.+)", value)
    if not match:
        return None
    return _clean_item(match.group(1))


# LLM: _clean_item trims quoting wrappers while preserving internal spaces and commas.
# 函数用途: 去掉结构化条目前后的空白和一层引号；中间内容保持原样用于字面匹配。
def _clean_item(value: object) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text


# LLM: _dedupe preserves first occurrence order for stable generated test names.
# 函数用途: 去重内容行但保持任务合同里的顺序，避免重复生成同一 content_check。
def _dedupe(values: list[str]) -> list[str]:
    items: list[str] = []
    for value in values:
        if value and value not in items:
            items.append(value)
    return items
