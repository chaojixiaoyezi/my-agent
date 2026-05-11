# LLM: Required-file extraction separates must-have filenames from forbidden rename examples.
# 模块用途: 从任务文本中抽取真正需要交付的文件名，避免把“禁止改成 x.html”里的反例误传给下层。

from __future__ import annotations

import re

_FILE_RE_TEMPLATE = r"(?<![\w./-])([A-Za-z0-9][A-Za-z0-9_.-]*\.(?:{exts}))(?![\w.-])"
_NEGATIVE_HINTS = ("禁止", "不允许", "不要", "不能", "不得", "勿", "do not", "don't", "must not", "not ")
_NEGATIVE_TARGET_RE = re.compile(
    r"(?:改成|改为|改名成|改名为|重命名为|命名为|叫做|叫|创建|生成|包含|产出|写入|to|as|create|generate|include|write)\s*$",
    re.IGNORECASE,
)
_NEGATIVE_CHAIN_CONNECTOR_RE = re.compile(r"^(?:[\s,，、/]*|[\s,，、/]*(?:或|或者|or)[\s,，、/]*)$", re.IGNORECASE)


# LLM: required_file_terms_from_text returns positive deliverable filenames only.
# 函数用途: 按片段提取文件名，并跳过禁止/反例语境中的目标文件名，保持首次出现顺序。
def required_file_terms_from_text(text: str, *, extensions: str) -> list[str]:
    pattern = re.compile(_FILE_RE_TEMPLATE.format(exts=extensions), re.IGNORECASE)
    terms: list[str] = []
    for segment in _segments(text):
        _append_terms(terms, _positive_terms(segment, pattern))
    return terms


# LLM: forbidden_file_terms_from_text returns negative-example filenames as a separate machine contract.
# 函数用途: 提取“禁止改成/不要创建”里的反例文件名，供下层明确知道哪些名字不能当产物。
def forbidden_file_terms_from_text(text: str, *, extensions: str) -> list[str]:
    pattern = re.compile(_FILE_RE_TEMPLATE.format(exts=extensions), re.IGNORECASE)
    terms: list[str] = []
    for segment in _segments(text):
        _append_terms(terms, _forbidden_terms(segment, pattern))
    return terms


# LLM: _append_terms preserves first-seen order across text segments.
# 函数用途: 将片段内的正向文件名合并进结果列表，避免重复项扰乱交接合同。
def _append_terms(terms: list[str], values: list[str]) -> None:
    for value in values:
        if value not in terms:
            terms.append(value)


# LLM: _positive_terms filters one local segment without growing the public extractor.
# 函数用途: 返回片段中不属于禁止/反例语境的文件名。
def _positive_terms(segment: str, pattern: re.Pattern[str]) -> list[str]:
    values: list[str] = []
    last_end = 0
    negative_chain_active = False
    for match in pattern.finditer(segment):
        connector = segment[last_end : match.start()]
        direct_negative = _is_negative_target(segment, match.start())
        chained_negative = negative_chain_active and _is_negative_chain_connector(connector)
        if not direct_negative and not chained_negative:
            values.append(match.group(1))
        negative_chain_active = direct_negative or chained_negative
        last_end = match.end()
    return values


# LLM: _forbidden_terms extracts only filenames that are forbidden targets or chained alternatives.
# 函数用途: 返回片段中作为禁止目标出现的文件名，不把“不要把 A 改成 B”里的 A 当成禁止项。
def _forbidden_terms(segment: str, pattern: re.Pattern[str]) -> list[str]:
    values: list[str] = []
    last_end = 0
    negative_chain_active = False
    for match in pattern.finditer(segment):
        connector = segment[last_end : match.start()]
        direct_negative = _is_negative_target(segment, match.start())
        chained_negative = negative_chain_active and _is_negative_chain_connector(connector)
        if direct_negative or chained_negative:
            values.append(match.group(1))
        negative_chain_active = direct_negative or chained_negative
        last_end = match.end()
    return values


# LLM: _segments limits semantic checks to local task lines and clauses.
# 函数用途: 把长任务拆成短片段，防止一个否定词影响后面无关的必需文件名。
def _segments(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[\n。；;]+", str(text or "")) if item.strip()]


# LLM: _is_negative_target detects filenames used as forbidden alternatives.
# 函数用途: 判断当前文件名前是否存在“禁止改成/不要创建”等语境，只过滤被禁止的目标名。
def _is_negative_target(segment: str, start: int) -> bool:
    before = segment[:start].lower()
    if not any(hint in before for hint in _NEGATIVE_HINTS):
        return False
    return bool(_NEGATIVE_TARGET_RE.search(before[-48:]))


# LLM: _is_negative_chain_connector extends one forbidden target across sibling alternatives.
# 函数用途: 识别“不要创建 a.html、b.html”或“不要改成 a.html 或 b.html”的并列反例。
def _is_negative_chain_connector(text: str) -> bool:
    return bool(_NEGATIVE_CHAIN_CONNECTOR_RE.match(text or ""))
