# LLM: Structured file-contract extraction keeps protocol fields separate from task-contract inference.
# 模块用途: 从 required_files / forbidden_files 机器字段中读取产物文件合同；额外的任务合同函数只给 handoff/验收层使用。

from __future__ import annotations

import re

_FIELD_RE = re.compile(r"^\s*(?:[-*]\s*)?(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(?P<tail>.*)$")
_BULLET_RE = re.compile(r"^\s*[-*]\s*(?P<value>.*)$")
_FILE_RE_TEMPLATE = (
    r"(?<![A-Za-z0-9_./-])"
    r"((?:~?/)?(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:{exts}))"
    r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9])"
)
_REQUIRED_FIELDS = frozenset({"required_files", "required_file_refs"})
_FORBIDDEN_FIELDS = frozenset({"forbidden_files"})
_LABELED_REQUIRED_FIELDS = frozenset({"父级必需文件/产物名", "必需文件/产物名", "父级必需文件", "父级产物名"})
_LABELED_FORBIDDEN_FIELDS = frozenset({"父级禁止文件/反例名", "禁止文件/反例名", "父级禁止文件", "禁止文件名"})
_NEGATIVE_MARKERS = (
    "禁止",
    "不允许",
    "不得",
    "不要",
    "不能",
    "forbidden",
    "do not",
    "must not",
)
_POSITIVE_MARKERS = (
    "必须包含",
    "必须包括",
    "必须交付",
    "必须输出",
    "必须写",
    "核心产物",
    "交付文件",
    "交付物",
    "产物写至",
    "输出到",
    "输出为",
    "写入",
    "写到",
    "创建",
    "生成",
    "保存到",
    "required files",
    "deliver to",
    "write to",
    "output to",
)
_RENAME_MARKERS = ("改名成", "改名为", "改成", "rename to", "renamed to")


# LLM: required_file_terms_from_text returns filenames from structured required_files fields only.
# 函数用途: 读取 `required_files: index.html, docs/report.md` 这类机器字段；不解析“必须生成”等自然语言。
def required_file_terms_from_text(text: str, *, extensions: str) -> list[str]:
    return _terms_from_structured_fields(text, extensions=extensions, field_names=_REQUIRED_FIELDS)


# LLM: forbidden_file_terms_from_text returns filenames from structured forbidden_files fields only.
# 函数用途: 读取 `forbidden_files: product.html, output.json` 这类机器字段；不解析“不要创建”等自然语言。
def forbidden_file_terms_from_text(text: str, *, extensions: str) -> list[str]:
    return _terms_from_structured_fields(text, extensions=extensions, field_names=_FORBIDDEN_FIELDS)


# LLM: labeled_required_file_terms_from_text reads system-generated Chinese contract labels only.
# 函数用途: 读取 `父级必需文件/产物名：index.html` 这类内部交接标签；普通“必须生成”句子仍不解析。
def labeled_required_file_terms_from_text(text: str, *, extensions: str) -> list[str]:
    return _terms_from_labeled_fields(text, extensions=extensions, labels=_LABELED_REQUIRED_FIELDS)


# LLM: task_contract_required_file_terms_from_text adds conservative deliverable extraction for handoff contracts.
# 函数用途: 给 context bundle / hierarchy 使用，合并机器字段、内部标签和明确“输出到/必须包含”等产物句式。
def task_contract_required_file_terms_from_text(text: str, *, extensions: str) -> list[str]:
    pattern = re.compile(_FILE_RE_TEMPLATE.format(exts=extensions), re.IGNORECASE)
    values = [
        *required_file_terms_from_text(text, extensions=extensions),
        *labeled_required_file_terms_from_text(text, extensions=extensions),
    ]
    for segment in _contract_segments(text):
        _append_terms(values, _positive_contract_terms(segment, pattern))
    return values


# LLM: task_contract_forbidden_file_terms_from_text extracts negative filename examples without polluting required files.
# 函数用途: 给 context bundle / hierarchy 使用，读取内部禁止标签和“不得创建/改名成”这类明确反例。
def task_contract_forbidden_file_terms_from_text(text: str, *, extensions: str) -> list[str]:
    pattern = re.compile(_FILE_RE_TEMPLATE.format(exts=extensions), re.IGNORECASE)
    values = [
        *forbidden_file_terms_from_text(text, extensions=extensions),
        *_terms_from_labeled_fields(text, extensions=extensions, labels=_LABELED_FORBIDDEN_FIELDS),
    ]
    for segment in _contract_segments(text):
        _append_terms(values, _negative_contract_terms(segment, pattern))
    return values


# LLM: _terms_from_structured_fields is the shared protocol parser for required and forbidden file lists.
# 函数用途: 扫描结构化字段、inline 列表和后续 bullet/裸文件列表，保持顺序并去重。
def _terms_from_structured_fields(text: str, *, extensions: str, field_names: frozenset[str]) -> list[str]:
    pattern = re.compile(_FILE_RE_TEMPLATE.format(exts=extensions), re.IGNORECASE)
    values: list[str] = []
    active = False
    for raw in str(text or "").splitlines():
        active, terms = _structured_file_terms_line(raw, active=active, field_names=field_names, pattern=pattern)
        _append_terms(values, terms)
    return values


# LLM: _terms_from_labeled_fields parses known internal Chinese labels with the same continuation rules.
# 函数用途: 支持父级继承块里的中文合同字段；未知标题仍不会被当成机器字段。
def _terms_from_labeled_fields(text: str, *, extensions: str, labels: frozenset[str]) -> list[str]:
    pattern = re.compile(_FILE_RE_TEMPLATE.format(exts=extensions), re.IGNORECASE)
    values: list[str] = []
    active = False
    for raw in str(text or "").splitlines():
        active, terms = _labeled_file_terms_line(raw, active=active, labels=labels, pattern=pattern)
        _append_terms(values, terms)
    return values


# LLM: _labeled_file_terms_line keeps Chinese label parsing flat for code-size and reviewability.
# 函数用途: 解析一行内部中文合同标签，返回下一行 active 状态和本行文件名列表。
def _labeled_file_terms_line(
    raw: str,
    *,
    active: bool,
    labels: frozenset[str],
    pattern: re.Pattern[str],
) -> tuple[bool, list[str]]:
    field = _labeled_field_from_line(raw)
    if field:
        is_active = field[0] in labels
        return is_active, _file_terms_from_value(field[1], pattern) if is_active else []
    if not active:
        return False, []
    continuation = _field_continuation_value(raw.strip())
    return (continuation is not None), _file_terms_from_value(continuation, pattern) if continuation is not None else []


# LLM: _labeled_field_from_line finds the first exact internal label in one physical line.
# 函数用途: 支持同一行里带句号前缀的内部标签；未知中文标题不会进入合同。
def _labeled_field_from_line(raw: str) -> tuple[str, str] | None:
    for segment in _labeled_line_segments(raw):
        if field := _labeled_field_match(segment):
            return field
    return None


# LLM: _structured_file_terms_line keeps required/forbidden file parsing shallow.
# 函数用途: 解析一行 structured file contract，返回下一行 active 状态和本行文件名列表。
def _structured_file_terms_line(
    raw: str,
    *,
    active: bool,
    field_names: frozenset[str],
    pattern: re.Pattern[str],
) -> tuple[bool, list[str]]:
    line = raw.strip()
    field = _field_match(line)
    if field:
        is_active = field[0] in field_names
        return is_active, _file_terms_from_value(field[1], pattern) if is_active else []
    if not active:
        return False, []
    continuation = _field_continuation_value(line)
    return (continuation is not None), _file_terms_from_value(continuation, pattern) if continuation is not None else []


# LLM: _field_match accepts machine field names, not translated labels.
# 函数用途: 解析 `required_files:` / `forbidden_files:` 行，返回标准化字段名和尾部内容。
def _field_match(line: str) -> tuple[str, str] | None:
    match = _FIELD_RE.match(line)
    if not match:
        return None
    return match.group("field").strip().lower(), match.group("tail").strip()


# LLM: _labeled_field_match recognizes exact internal labels without broad translation guessing.
# 函数用途: 解析 `父级必需文件/产物名：...` 这类系统标签，返回原标签和尾部内容。
def _labeled_field_match(raw: str) -> tuple[str, str] | None:
    line = str(raw or "").strip().lstrip("-* ").strip()
    for separator in (":", "：", "="):
        if separator not in line:
            continue
        label, tail = line.split(separator, 1)
        label = label.strip()
        if label in _LABELED_REQUIRED_FIELDS or label in _LABELED_FORBIDDEN_FIELDS:
            return label, tail.strip()
    return None


# LLM: _labeled_line_segments lets embedded system labels survive surrounding prose.
# 函数用途: 允许“...。父级必需文件/产物名：index.html”被读取，仍只匹配精确内部标签。
def _labeled_line_segments(raw: str) -> list[str]:
    return [item.strip() for item in re.split(r"[。；;]+", str(raw or "")) if item.strip()]


# LLM: _contract_segments keeps positive/negative extraction local to short task-contract clauses.
# 函数用途: 按常见中英文标点切分合同文本，避免一个读文件句子污染后面的输出句子。
def _contract_segments(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[\n。；;]+", str(text or "")) if item.strip()]


# LLM: _positive_contract_terms extracts files only after explicit deliverable markers.
# 函数用途: 从“必须包含 index.html / 输出到 final_report.md”这类短句提取产物名；读取句子不算。
def _positive_contract_terms(segment: str, pattern: re.Pattern[str]) -> list[str]:
    if _contains_marker(segment, _NEGATIVE_MARKERS):
        segment = _before_first_marker(segment, _NEGATIVE_MARKERS)
    values: list[str] = []
    for marker in _POSITIVE_MARKERS:
        tail = _tail_after_marker(segment, marker)
        if tail:
            _append_terms(values, _file_terms_from_value(_positive_tail(marker, tail), pattern))
    return values


# LLM: _negative_contract_terms extracts forbidden refs and rename targets from explicit negative clauses.
# 函数用途: 从“不允许把 A 改名成 B”只提取 B；从“禁止创建 output.json”提取 output.json。
def _negative_contract_terms(segment: str, pattern: re.Pattern[str]) -> list[str]:
    if not _contains_marker(segment, _NEGATIVE_MARKERS):
        return []
    tail = _tail_after_any_marker(segment, _RENAME_MARKERS) or _tail_after_any_marker(segment, _NEGATIVE_MARKERS)
    return _file_terms_from_value(tail, pattern) if tail else []


# LLM: _contains_marker does case-insensitive marker checks for mixed Chinese/English contracts.
# 函数用途: 判断片段是否包含任一合同触发词。
def _contains_marker(text: str, markers: tuple[str, ...]) -> bool:
    lowered = str(text or "").lower()
    return any(marker.lower() in lowered for marker in markers)


# LLM: _before_first_marker trims a segment before the earliest negative clause.
# 函数用途: 防止“必须包含 A。不允许 B”类混合片段把禁止文件也归入 required。
def _before_first_marker(text: str, markers: tuple[str, ...]) -> str:
    lowered = str(text or "").lower()
    indexes = [idx for marker in markers if (idx := lowered.find(marker.lower())) >= 0]
    return str(text or "")[: min(indexes)] if indexes else str(text or "")


# LLM: _tail_after_any_marker returns the text after the earliest matching marker.
# 函数用途: 给负向/改名合同取尾部文件列表。
def _tail_after_any_marker(text: str, markers: tuple[str, ...]) -> str:
    lowered = str(text or "").lower()
    candidates = [
        (idx, len(marker))
        for marker in markers
        if (idx := lowered.find(marker.lower())) >= 0
    ]
    if not candidates:
        return ""
    idx, length = min(candidates, key=lambda item: item[0])
    return str(text or "")[idx + length :]


# LLM: _tail_after_marker returns text after one marker, preserving original characters for regex extraction.
# 函数用途: 支持中英文 marker 的大小写无关匹配。
def _tail_after_marker(text: str, marker: str) -> str:
    lowered = str(text or "").lower()
    idx = lowered.find(marker.lower())
    return str(text or "")[idx + len(marker) :] if idx >= 0 else ""


# LLM: _positive_tail bounds action-style file targets before explanatory clauses.
# 函数用途: `写入 <run_id>.md，报告 README.md 摘要` 只看逗号前的写入目标，不把输入主题当产物。
def _positive_tail(marker: str, tail: str) -> str:
    action_markers = {"输出到", "输出为", "写入", "写到", "保存到", "deliver to", "write to", "output to"}
    if marker not in action_markers:
        return tail
    return re.split(r"[,，]", str(tail or ""), maxsplit=1)[0]


# LLM: _field_continuation_value keeps multiline structured lists small and deterministic.
# 函数用途: 支持字段下一行的 bullet 文件列表，或只包含文件名/分隔符的裸列表行；遇到普通说明就停止。
def _field_continuation_value(line: str) -> str | None:
    if not line:
        return None
    bullet = _BULLET_RE.match(line)
    if bullet:
        return bullet.group("value").strip()
    return line if _looks_like_file_list_line(line) else None


# LLM: _looks_like_file_list_line prevents prose after a field from being swallowed as contract data.
# 函数用途: 只有纯文件名列表才作为字段续行；包含普通词句的行会结束当前字段。
def _looks_like_file_list_line(line: str) -> bool:
    text = str(line or "").strip()
    if not text or "." not in text:
        return False
    cleaned = re.sub(r"[A-Za-z0-9_.\-/]+", "", text)
    return all(ch in " \t,，、;；|[]()（）'\"" for ch in cleaned)


# LLM: _file_terms_from_value extracts literal relative file refs from one structured value.
# 函数用途: 从 inline 字段、bullet 或纯列表行中提取文件名，支持文件名之间用 `/` 写成短列表。
def _file_terms_from_value(value: str, pattern: re.Pattern[str]) -> list[str]:
    normalized = _normalize_file_list_separators(value)
    return [_clean_term(match.group(1)) for match in pattern.finditer(normalized)]


# LLM: _normalize_file_list_separators treats file/file alternatives as list separators, not directories.
# 函数用途: `style.css/app.js` 解析成两个文件；`docs/report.md` 保持为相对路径。
def _normalize_file_list_separators(text: str) -> str:
    return re.sub(
        r"(\.[A-Za-z0-9]{1,8})/(?=[A-Za-z0-9][A-Za-z0-9_.-]*\.[A-Za-z0-9]{1,8}(?![A-Za-z0-9_.-]))",
        r"\1,",
        str(text or ""),
    )


# LLM: _clean_term normalizes path separators without resolving the filesystem.
# 函数用途: 去掉字段值两侧标点和 `./`，保留相对目录结构。
def _clean_term(value: object) -> str:
    text = str(value or "").strip().strip("`'\".,;:，。；：、").replace("\\", "/")
    return text[2:] if text.startswith("./") else text


# LLM: _append_terms preserves first-seen order across structured fields.
# 函数用途: 合并字段中的文件名，跳过空值和重复项。
def _append_terms(terms: list[str], values: list[str]) -> None:
    for value in values:
        if value and value not in terms:
            terms.append(value)
