# LLM: Required-file extraction separates must-have filenames from forbidden rename examples.
# 模块用途: 从任务文本中抽取真正需要交付的文件名，避免把“禁止改成 x.html”里的反例误传给下层。

from __future__ import annotations

import re

_FILE_RE_TEMPLATE = (
    r"(?<![A-Za-z0-9_./-])([A-Za-z0-9][A-Za-z0-9_.-]*\.(?:{exts}))(?![A-Za-z0-9_-]|\.[A-Za-z0-9])"
)
_NEGATIVE_HINTS = (
    "禁止",
    "不允许",
    "不要",
    "不能",
    "不得",
    "不写",
    "不创建",
    "不生成",
    "不产出",
    "不包含",
    "勿",
    "无",
    "do not",
    "don't",
    "must not",
    "not ",
    "no ",
    "without",
)
# LLM: Negative examples may be introduced by “如/例如/比如”, not only by rename/create verbs.
# 函数用途: 识别禁止说明里的示例前缀，避免反例文件名进入 required_files。
_NEGATIVE_TARGET_RE = re.compile(
    r"(?:改成|改为|改名成|改名为|重命名为|命名为|叫做|叫|创建|生成|包含|产出|写入|写|如|例如|比如|forbidden_files|内部文件污染|文件污染|污染|to|as|create|generate|include|write)\s*[（(]?\s*$",
    re.IGNORECASE,
)
_BARE_NEGATIVE_TARGET_RE = re.compile(
    r"(?:^|[\s：:，,、])(?:禁止(?:改名|文件名|文件|创建|生成|产出|写入|写)?|不允许|不要|不能|不得|勿|do not|don't|must not)\s*[：:]?\s*$",
    re.IGNORECASE,
)
_NEGATIVE_LABEL_RE = re.compile(
    r"(?:禁止(?:创建|生成|产出|写入|写)?\s*[：:]|禁止(?:创建)?(?:文件名|内部文件|文件/反例名|文件|反例名)"
    r"[^。；;\n]{0,96}[：:（(】\]]|forbidden(?:_files)?[^。；;\n]{0,96}[：:（(】\]])\s*$",
    re.IGNORECASE,
)
_NEGATIVE_HEADER_RE = re.compile(
    r"(?:#+\s*禁止(?:创建)?(?:文件名|内部文件|文件/反例名|文件|反例名)\s*$|"
    r"禁止(?:创建|生成|产出|写入|写)?\s*[：:]|禁止(?:创建)?(?:文件名|内部文件|文件/反例名|文件|反例名)"
    r"[^。；;\n]{0,96}[：:（(】\]]|forbidden(?:_files)?[^。；;\n]{0,96}[：:（(】\]])\s*$",
    re.IGNORECASE,
)
_NEGATIVE_CHAIN_CONNECTOR_RE = re.compile(r"^(?:[\s,，、/]*|[\s,，、/]*(?:或|或者|or)[\s,，、/]*)$", re.IGNORECASE)
_BULLET_PREFIX_RE = re.compile(r"^[-*]\s*")
_LOCATION_TARGET_RE = re.compile(r"(?:放进|放入|放到|放在|置于|移入|inside|under|into)", re.IGNORECASE)
_FILE_LIKE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.[A-Za-z0-9]{1,6}")
_INTERNAL_REF_FILES = frozenset({
    "task.json",
    "execution_context.json",
    "runner_result.md",
    "runner_result.json",
    "latest_continue_packet.json",
    "checkpoint.json",
    "summary.md",
    "context_bundle.json",
})
_INTERNAL_REF_HINTS = (
    "真实",
    "里的",
    "里",
    "状态",
    "refs",
    "引用",
    "读取",
    "报告",
    "恢复",
    "接着",
    "续跑",
    "continue",
    "packet",
    "checkpoint",
    "summary",
    "run",
    "root",
    "child",
)
_POSITIVE_DELIVERABLE_HINTS = (
    "交付",
    "产出",
    "创建",
    "生成",
    "写入",
    "写 ",
    "包含",
    "required",
    "deliver",
    "create",
    "generate",
    "write",
)


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
        if not direct_negative and not chained_negative and not _is_internal_context_reference(segment, match):
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
    normalized = _normalize_file_list_separators(str(text or ""))
    segments: list[str] = []
    active_negative_header = ""
    for raw in re.split(r"[\n。；;]+", normalized):
        item = raw.strip()
        if not item:
            active_negative_header = ""
            continue
        if _NEGATIVE_HEADER_RE.search(item):
            active_negative_header = item
            segments.append(item)
            continue
        if active_negative_header and _BULLET_PREFIX_RE.match(item):
            segments.append(f"{active_negative_header} {_BULLET_PREFIX_RE.sub('', item)}")
            continue
        if active_negative_header and _FILE_LIKE_RE.search(item):
            segments.append(f"{active_negative_header} {item}")
            active_negative_header = ""
            continue
        active_negative_header = ""
        segments.append(item)
    return segments


# LLM: _normalize_file_list_separators keeps slash-separated filename alternatives parseable.
# 函数用途: 把 `product.html/old.html` 这种文件名列表里的斜杠当分隔符，不影响普通路径分隔。
def _normalize_file_list_separators(text: str) -> str:
    return re.sub(
        r"(\.[A-Za-z0-9]{1,6})/(?=[A-Za-z0-9][A-Za-z0-9_.-]*\.[A-Za-z0-9]{1,6}(?![A-Za-z0-9_.-]))",
        r"\1、",
        text,
    )


# LLM: _is_negative_target detects filenames used as forbidden alternatives.
# 函数用途: 判断当前文件名前是否存在“禁止改成/不要创建”等语境，只过滤被禁止的目标名。
def _is_negative_target(segment: str, start: int) -> bool:
    before = segment[:start].lower()
    if not any(hint in before for hint in _NEGATIVE_HINTS):
        return False
    window = before[-64:]
    label_window = before[-128:]
    target_negative = bool(_NEGATIVE_TARGET_RE.search(window) or _NEGATIVE_LABEL_RE.search(label_window))
    bare_negative = bool(_BARE_NEGATIVE_TARGET_RE.search(window))
    if bare_negative and not target_negative and _is_location_rule_source(segment, start):
        return False
    return target_negative or bare_negative


# LLM: _is_location_rule_source keeps "do not place style.css under css/" from forbidding style.css itself.
# 函数用途: 识别“禁止 A.css 放进子目录”这种位置约束，避免把必须产物 A.css 误当禁止文件。
def _is_location_rule_source(segment: str, start: int) -> bool:
    after = segment[start:]
    return bool(_LOCATION_TARGET_RE.search(after))


# LLM: _is_internal_context_reference gives adjacent state-ref wording priority over distant create/write verbs.
# 函数用途: task.json/execution_context.json 在“里的状态/refs/阻塞汇报”等语境里只是内部引用，不是下级要创建的产物。
def _is_internal_context_reference(segment: str, match: re.Match[str]) -> bool:
    filename = match.group(1).lower()
    if filename not in _INTERNAL_REF_FILES:
        return False
    before = segment[max(0, match.start() - 48) : match.start()].lower()
    after = segment[match.end() : match.end() + 48].lower()
    if any(hint in after[:24] for hint in ("里的", "里", "refs", "阻塞", "汇报", "状态")):
        return True
    if any(hint in before for hint in _POSITIVE_DELIVERABLE_HINTS):
        return False
    return any(hint in before or hint in after for hint in _INTERNAL_REF_HINTS)


# LLM: _is_negative_chain_connector extends one forbidden target across sibling alternatives.
# 函数用途: 识别“不要创建 a.html、b.html”或“不要改成 a.html 或 b.html”的并列反例。
def _is_negative_chain_connector(text: str) -> bool:
    return bool(_NEGATIVE_CHAIN_CONNECTOR_RE.match(text or ""))
