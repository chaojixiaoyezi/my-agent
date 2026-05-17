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
_REQUIRED_CONTENT_FILE_RE = re.compile(
    r"\brequired_content_(?:lines|texts?)\[([^\]]+)\]\s*[:=]\s*(.*)",
    re.IGNORECASE,
)
_FILE_TERM_RE = re.compile(r"([A-Za-z0-9_.\-/]+?\.(?:csv|txt|md|markdown|json|ya?ml|py|js|ts|html?|css))")
_NATURAL_COUNT_RE = re.compile(
    r"(?:下面|以下|后面|接下来)\s*([0-9一二两三四五六七八九十]+)\s*行.*(?:一字不差|逐字|原样|出现在|包含)"
)
_ENGLISH_COUNT_RE = re.compile(r"(?:next|following)\s+(\d+)\s+lines?.*(?:exactly|verbatim|contain)", re.IGNORECASE)
_FENCED_CONTENT_ANCHORS = (
    "必须包含以下内容",
    "需要包含以下内容",
    "以下内容",
    "下面内容",
    "一字不差",
    "原样包含",
    "must contain the following",
    "include the following",
)


# LLM: required_content_lines_from_texts accepts structured fields plus narrow exact-content natural blocks.
# 函数用途: 从 `required_content_lines: a | b`、项目符号、代码块或“下面 N 行一字不差”提取必须出现的字面行。
def required_content_lines_from_texts(texts: list[object]) -> list[str]:
    """Return explicit required content lines from structured task text."""

    values: list[str] = []
    for value in texts:
        values.extend(_required_lines_from_text(str(value or "")))
    return _dedupe(values)[:100]


# LLM: required_content_lines_by_file_from_texts extracts explicit file-scoped content contracts.
# 函数用途: 从 `required_content_lines[file]` 或 “file 必须包含以下内容”提取多文件内容验收映射。
def required_content_lines_by_file_from_texts(texts: list[object]) -> dict[str, list[str]]:
    """Return explicit per-file content-line contracts from task text."""

    merged: dict[str, list[str]] = {}
    for value in texts:
        _merge_file_line_mapping(merged, _required_lines_by_file_from_text(str(value or "")))
    return dict(list(merged.items())[:50])


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


# LLM: required_content_lines_by_file_for_task centralizes per-file content checks for multi-artifact tasks.
# 函数用途: 从 task 的目标、说明和验收条件里提取普通文件到 required lines 的映射。
def required_content_lines_by_file_for_task(task: Any) -> dict[str, list[str]]:
    """Return explicit per-file content contracts from a subagent task."""

    return required_content_lines_by_file_from_texts([
        getattr(task, "goal", ""),
        getattr(task, "thought", ""),
        getattr(task, "description", ""),
        *(getattr(task, "acceptance_checks", []) or []),
    ])


# LLM: _required_lines_from_text supports explicit field markers and narrow natural exact-content blocks.
# 函数用途: 扫描 required_content_lines、明确“下面 N 行一字不差”和代码块内容；普通说明不猜。
def _required_lines_from_text(text: str) -> list[str]:
    lines = text.splitlines()
    values: list[str] = []
    index = 0
    while index < len(lines):
        extracted, consumed = _unscoped_content_at(lines, index)
        values.extend(extracted)
        index += consumed
    return values


# LLM: _required_lines_by_file_from_text scans exact content contracts that name their target file.
# 函数用途: 解析多文件任务的内容验收映射；只接受显式文件名加结构化内容，不按普通描述猜。
def _required_lines_by_file_from_text(text: str) -> dict[str, list[str]]:
    lines = text.splitlines()
    values: dict[str, list[str]] = {}
    index = 0
    while index < len(lines):
        file_key, extracted, consumed = _file_scoped_content_at(lines, index)
        _extend_file_lines(values, file_key, extracted)
        index += consumed
    return values


# LLM: _merge_file_line_mapping keeps the public by-file extractor shallow for code-size guards.
# 函数用途: 合并一段文本解析出的文件内容映射，空内容跳过，同一文件保持去重和顺序。
def _merge_file_line_mapping(merged: dict[str, list[str]], mapping: dict[str, list[str]]) -> None:
    for file_key, lines in mapping.items():
        if lines:
            merged[file_key] = _dedupe([*merged.get(file_key, []), *lines])[:100]


# LLM: _unscoped_content_at returns the extracted content and how many source lines were consumed.
# 函数用途: 解析单文件/全局内容合同的一行入口，让主扫描循环不嵌套多层判断。
def _unscoped_content_at(lines: list[str], index: int) -> tuple[list[str], int]:
    line = lines[index].strip()
    match = _REQUIRED_CONTENT_RE.search(line)
    if match:
        return _content_from_required_match(match, lines[index + 1 :])
    return _anchored_content_after(lines, index)


# LLM: _content_from_required_match handles inline and following-bullet structured content fields.
# 函数用途: 解析 `required_content_lines:` 行；有尾部就直接拆，没有尾部就读取后续项目符号。
def _content_from_required_match(match: re.Match[str], following_lines: list[str]) -> tuple[list[str], int]:
    tail = match.group(1).strip()
    if tail:
        return _inline_items(tail), 1
    consumed, bullet_values = _following_bullet_items(following_lines)
    return bullet_values, consumed + 1


# LLM: _file_scoped_content_at resolves one source line into a file key, content lines, and consumption.
# 函数用途: 解析 per-file 内容合同的一行入口；没有明确文件目标时只消费当前行。
def _file_scoped_content_at(lines: list[str], index: int) -> tuple[str, list[str], int]:
    line = lines[index].strip()
    match = _REQUIRED_CONTENT_FILE_RE.search(line)
    if match:
        return _file_content_from_required_match(match, lines[index + 1 :])
    file_key = _file_key_from_content_anchor(line)
    if not file_key:
        return "", [], 1
    extracted, consumed = _anchored_content_after(lines, index)
    return file_key, extracted, consumed


# LLM: _file_content_from_required_match handles structured per-file inline or bullet contracts.
# 函数用途: 解析 `required_content_lines[file]: ...`，返回文件名、内容行和消耗行数。
def _file_content_from_required_match(match: re.Match[str], following_lines: list[str]) -> tuple[str, list[str], int]:
    file_key = _clean_file_key(match.group(1))
    tail = match.group(2).strip()
    if tail:
        return file_key, _inline_items(tail), 1
    consumed, bullet_values = _following_bullet_items(following_lines)
    return file_key, bullet_values, consumed + 1


# LLM: _anchored_content_after handles explicit fenced or counted natural-language content blocks.
# 函数用途: 在“必须包含以下内容”或“下面 N 行一字不差”后提取内容；没有命中只消费当前行。
def _anchored_content_after(lines: list[str], index: int) -> tuple[list[str], int]:
    line = lines[index].strip()
    fenced, consumed = _following_fenced_content(lines[index:])
    if fenced:
        return fenced, consumed
    count = _natural_exact_line_count(line)
    if count:
        return _following_exact_lines(lines[index + 1 :], count), count + 1
    return [], 1


# LLM: _following_fenced_content captures code fences only when introduced by an explicit content anchor.
# 函数用途: 用户写“必须包含以下内容”并给代码块时，提取代码块每行作为内容验收合同。
def _following_fenced_content(lines: list[str]) -> tuple[list[str], int]:
    if not lines or not _content_block_anchor(lines[0]):
        return [], 0
    start = 1
    while start < len(lines) and not lines[start].strip().startswith("```"):
        if lines[start].strip():
            return [], 0
        start += 1
    if start >= len(lines):
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


# LLM: _content_block_anchor is intentionally narrow so prose paragraphs do not become content checks.
# 函数用途: 判断某行是否明确引出后续精确内容；没有这些词就不读取后续代码块。
def _content_block_anchor(line: str) -> bool:
    text = str(line or "").strip().lower()
    return any(anchor in text for anchor in _FENCED_CONTENT_ANCHORS)


# LLM: _file_key_from_content_anchor finds a target file only on lines that introduce exact content.
# 函数用途: 多文件内容合同必须同时有文件名和内容锚点；普通“生成 report.md”不会触发。
def _file_key_from_content_anchor(line: str) -> str:
    if not (_content_block_anchor(line) or _natural_exact_line_count(line)):
        return ""
    match = _FILE_TERM_RE.search(str(line or ""))
    return _clean_file_key(match.group(1)) if match else ""


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


# LLM: _natural_exact_line_count recognizes user-friendly exact-line instructions without internal field names.
# 函数用途: 解析“下面四行一字不差”或英文 “following 3 lines exactly”，返回应捕获的行数。
def _natural_exact_line_count(line: str) -> int:
    text = str(line or "").strip()
    match = _NATURAL_COUNT_RE.search(text)
    if match:
        return _positive_count(match.group(1))
    english = _ENGLISH_COUNT_RE.search(text)
    return _positive_count(english.group(1)) if english else 0


# LLM: _following_exact_lines captures exactly N non-empty lines after an explicit counted instruction.
# 函数用途: 按用户声明的行数提取后续内容，避免把后面的“请检查”等普通说明吃进去。
def _following_exact_lines(lines: list[str], count: int) -> list[str]:
    values: list[str] = []
    for line in lines:
        text = _clean_item(line)
        if not text and not values:
            continue
        if not text:
            break
        values.append(text)
        if len(values) >= count:
            break
    return values


# LLM: _positive_count clamps natural block extraction to a small exact-content range.
# 函数用途: 把阿拉伯数字或常见中文数字转成 1-20 的行数，防止一次吞掉大段 prompt。
def _positive_count(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = _chinese_number(value)
    return number if 0 < number <= 20 else 0


# LLM: _chinese_number handles the small line counts users naturally write in Chinese prompts.
# 函数用途: 支持 一/两/二 到 二十 的行数表达；超出范围返回 0。
def _chinese_number(value: str) -> int:
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    text = str(value or "").strip()
    if text in digits:
        return digits[text]
    if text == "十":
        return 10
    if text.startswith("十") and len(text) == 2:
        return 10 + digits.get(text[1], 0)
    if "十" in text:
        left, right = text.split("十", 1)
        return digits.get(left, 0) * 10 + (digits.get(right, 0) if right else 0)
    return 0


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
