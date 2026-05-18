# LLM: Required content contracts are protocol fields, not natural-language guesses.
# 模块用途: 从 required_content_lines / required_content_texts 机器字段读取普通文件内容验收合同。

from __future__ import annotations

"""Extract explicit required content lines from structured contracts."""

import re
from typing import Any

_REQUIRED_CONTENT_RE = re.compile(
    r"^\s*(?:[-*]\s*)?required_content_(?:lines|texts?)\s*[:=]\s*(?P<tail>.*)$",
    re.IGNORECASE,
)
_REQUIRED_CONTENT_FILE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?required_content_(?:lines|texts?)\[([^\]]+)\]\s*[:=]\s*(?P<tail>.*)$",
    re.IGNORECASE,
)
_ANY_STRUCTURED_FIELD_RE = re.compile(r"^\s*(?:[-*]\s*)?[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?\s*[:=]")
_BULLET_RE = re.compile(r"^\s*[-*]\s*(?P<value>.*)$")


# LLM: required_content_lines_from_texts reads only structured content contract fields.
# 函数用途: 从 `required_content_lines: a | b` 或其 bullet/fence 续行提取必须出现的字面行。
def required_content_lines_from_texts(texts: list[object]) -> list[str]:
    """Return explicit required content lines from structured task text."""

    values: list[str] = []
    for value in texts:
        values.extend(_required_lines_from_text(str(value or "")))
    return _dedupe(values)[:100]


# LLM: required_content_lines_by_file_from_texts reads per-file structured content contracts.
# 函数用途: 从 `required_content_lines[file.md]: ...` 提取多文件内容验收映射。
def required_content_lines_by_file_from_texts(texts: list[object]) -> dict[str, list[str]]:
    """Return explicit per-file content-line contracts from task text."""

    merged: dict[str, list[str]] = {}
    for value in texts:
        _merge_file_line_mapping(merged, _required_lines_by_file_from_text(str(value or "")))
    return dict(list(merged.items())[:50])


# LLM: required_content_lines_for_task reads runtime contracts from task attributes only.
# 函数用途: 从 task.attributes 读取 content_check 需要的结构化字面内容行，不解析 goal/acceptance 文本。
def required_content_lines_for_task(task: Any) -> list[str]:
    """Return explicit content-line contracts from a subagent task."""

    return _dedupe(_string_list(_task_attributes(task).get("required_content_lines")))[:100]


# LLM: required_content_lines_by_file_for_task reads per-file runtime contracts from attributes only.
# 函数用途: 从 task.attributes.required_content_files 读取文件到 required lines 的映射。
def required_content_lines_by_file_for_task(task: Any) -> dict[str, list[str]]:
    """Return explicit per-file content contracts from a subagent task."""

    return _content_files_from_attributes(_task_attributes(task))


# LLM: _task_attributes normalizes task attributes across dataclass and namespace tests.
# 函数用途: 读取 task.attributes 字典；没有或类型不对时返回空字典。
def _task_attributes(task: Any) -> dict[str, Any]:
    attributes = getattr(task, "attributes", {})
    return attributes if isinstance(attributes, dict) else {}


# LLM: _content_files_from_attributes returns explicit per-file content contracts.
# 函数用途: 支持 required_content_files 字典或列表形式，保持文件名和值都是字面结构化字段。
def _content_files_from_attributes(attributes: dict[str, Any]) -> dict[str, list[str]]:
    raw = attributes.get("required_content_files")
    if isinstance(raw, dict):
        return _content_files_from_mapping(raw)
    if isinstance(raw, list):
        return _content_files_from_rows(raw)
    return {}


# LLM: _content_files_from_mapping keeps dict-shaped contracts deterministic and bounded.
# 函数用途: 将 {"file.md": ["line"]} 归一成去重、限量的 per-file 内容合同。
def _content_files_from_mapping(raw: dict[object, object]) -> dict[str, list[str]]:
    items: dict[str, list[str]] = {}
    for key, value in raw.items():
        file_key = _clean_file_key(key)
        lines = _dedupe(_string_list(value))[:100]
        if file_key and lines:
            items[file_key] = lines
    return dict(list(items.items())[:50])


# LLM: _content_files_from_rows supports config-friendly list rows without parsing prose.
# 函数用途: 支持 [{"path": "a.md", "lines": [...]}] 或 {"file": ..., "required_content_lines": ...}。
def _content_files_from_rows(rows: list[object]) -> dict[str, list[str]]:
    items: dict[str, list[str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        file_key = _clean_file_key(row.get("path") or row.get("file") or row.get("file_path"))
        lines = _dedupe(
            _string_list(row.get("lines") or row.get("required_content_lines") or row.get("required_lines"))
        )[:100]
        if file_key and lines:
            items[file_key] = lines
    return dict(list(items.items())[:50])


# LLM: _required_lines_from_text scans unscoped structured content fields.
# 函数用途: 只处理 required_content_lines/required_content_texts，不从普通说明或代码块中猜。
def _required_lines_from_text(text: str) -> list[str]:
    lines = str(text or "").splitlines()
    values: list[str] = []
    index = 0
    while index < len(lines):
        match = _REQUIRED_CONTENT_RE.match(lines[index].strip())
        if not match:
            index += 1
            continue
        extracted, consumed = _content_from_structured_match(match.group("tail"), lines[index + 1 :])
        values.extend(extracted)
        index += consumed + 1
    return values


# LLM: _required_lines_by_file_from_text scans file-scoped structured content fields.
# 函数用途: 多文件任务必须写 required_content_lines[file]，否则代码层不猜目标文件。
def _required_lines_by_file_from_text(text: str) -> dict[str, list[str]]:
    lines = str(text or "").splitlines()
    values: dict[str, list[str]] = {}
    index = 0
    while index < len(lines):
        match = _REQUIRED_CONTENT_FILE_RE.match(lines[index].strip())
        if not match:
            index += 1
            continue
        extracted, consumed = _content_from_structured_match(match.group("tail"), lines[index + 1 :])
        _extend_file_lines(values, _clean_file_key(match.group(1)), extracted)
        index += consumed + 1
    return values


# LLM: _content_from_structured_match supports inline, bullet, and fenced values after a machine field.
# 函数用途: 字段尾部有内容就按 `|` 拆；尾部为空时读取后续 bullet 或 fenced block。
def _content_from_structured_match(tail: str, following_lines: list[str]) -> tuple[list[str], int]:
    if tail.strip():
        return _inline_items(tail), 0
    fenced, consumed = _following_fenced_content(following_lines)
    if fenced:
        return fenced, consumed
    consumed, bullet_values = _following_bullet_items(following_lines)
    return bullet_values, consumed


# LLM: _following_fenced_content reads a code fence only after a structured required_content field.
# 函数用途: 支持 `required_content_lines:\n```...``` `，但不接受没有字段锚点的自然语言代码块。
def _following_fenced_content(lines: list[str]) -> tuple[list[str], int]:
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or not lines[start].strip().startswith("```"):
        return [], 0
    values: list[str] = []
    offset = start + 1
    while offset < len(lines):
        stripped = lines[offset].strip()
        if stripped.startswith("```"):
            return [_clean_item(item) for item in values if _clean_item(item)], offset + 1
        values.append(stripped)
        offset += 1
    return [], 0


# LLM: _following_bullet_items accepts compact lists after an empty required_content field.
# 函数用途: 支持 `required_content_lines:` 下一行起用 `- 文本` 写多条字面内容。
def _following_bullet_items(lines: list[str]) -> tuple[int, list[str]]:
    values: list[str] = []
    consumed = 0
    for line in lines:
        status, value = _bullet_continuation_value(line, has_values=bool(values))
        if status == "skip":
            consumed += 1
            continue
        if status == "stop":
            break
        if value:
            values.append(value)
        consumed += 1
    return consumed, values


# LLM: _bullet_continuation_value classifies one line after an empty content field.
# 函数用途: 区分继续读取、跳过前置空行和停止读取，避免主循环嵌套增长。
def _bullet_continuation_value(line: str, *, has_values: bool) -> tuple[str, str]:
    if _ANY_STRUCTURED_FIELD_RE.match(line):
        return "stop", ""
    bullet = _BULLET_RE.match(line)
    if bullet:
        return "value", _clean_item(bullet.group("value"))
    if not line.strip() and not has_values:
        return "skip", ""
    return "stop", ""


# LLM: _inline_items treats pipe as the only multi-item separator so CSV commas stay intact.
# 函数用途: 拆 `required_content_lines: a | b`，不按英文逗号切 CSV 内容。
def _inline_items(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    if "|" in text:
        return [_clean_item(item) for item in text.split("|") if _clean_item(item)]
    return [_clean_item(text)] if _clean_item(text) else []


# LLM: _merge_file_line_mapping keeps the public by-file extractor shallow for code-size guards.
# 函数用途: 合并一段文本解析出的文件内容映射，空内容跳过，同一文件保持去重和顺序。
def _merge_file_line_mapping(merged: dict[str, list[str]], mapping: dict[str, list[str]]) -> None:
    for file_key, lines in mapping.items():
        if lines:
            merged[file_key] = _dedupe([*merged.get(file_key, []), *lines])[:100]


# LLM: _extend_file_lines deduplicates per-file values while preserving prompt order.
# 函数用途: 合并同一文件多段内容合同，避免重复生成同一条 content_check。
def _extend_file_lines(values: dict[str, list[str]], file_key: str, lines: list[str]) -> None:
    if not file_key:
        return
    values[file_key] = _dedupe([*values.get(file_key, []), *lines])


# LLM: _clean_file_key keeps user-provided file names literal but path-normalized.
# 函数用途: 规范 `./report.md` 和反斜杠写法；不展开真实文件系统路径。
def _clean_file_key(value: object) -> str:
    return str(value or "").strip().strip("'\"").replace("\\", "/").lstrip("./")


# LLM: _clean_item strips list/fence framing without altering inner CSV/text content.
# 函数用途: 清理 required_content_lines 的单条字面内容。
def _clean_item(value: object) -> str:
    return str(value or "").strip().strip("`").strip()


# LLM: _string_list normalizes structured scalar/list values without splitting prose.
# 函数用途: 将 attributes 里的字符串或列表转成字符串列表；不会按逗号、空格或自然语言拆分。
def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list | tuple):
        return [_clean_item(item) for item in value if _clean_item(item)]
    text = _clean_item(value)
    return [text] if text else []


# LLM: _dedupe preserves first-seen order for generated content checks.
# 函数用途: 去重内容合同，保持用户/任务字段中的顺序。
def _dedupe(values: list[str]) -> list[str]:
    items: list[str] = []
    for value in values:
        if value not in items:
            items.append(value)
    return items
