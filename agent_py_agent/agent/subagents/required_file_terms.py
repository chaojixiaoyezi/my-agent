
from __future__ import annotations

import re

from ..common.value_parsing import dedupe_strings

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


def required_file_terms_from_text(text: str, *, extensions: str) -> list[str]:
    return _terms_from_structured_fields(text, extensions=extensions, field_names=_REQUIRED_FIELDS)


def forbidden_file_terms_from_text(text: str, *, extensions: str) -> list[str]:
    return _terms_from_structured_fields(text, extensions=extensions, field_names=_FORBIDDEN_FIELDS)


def labeled_required_file_terms_from_text(text: str, *, extensions: str) -> list[str]:
    return _terms_from_labeled_fields(text, extensions=extensions, labels=_LABELED_REQUIRED_FIELDS)


def task_contract_required_file_terms_from_text(text: str, *, extensions: str) -> list[str]:
    return dedupe_strings([
        *required_file_terms_from_text(text, extensions=extensions),
        *labeled_required_file_terms_from_text(text, extensions=extensions),
    ])


def task_contract_forbidden_file_terms_from_text(text: str, *, extensions: str) -> list[str]:
    return dedupe_strings([
        *forbidden_file_terms_from_text(text, extensions=extensions),
        *_terms_from_labeled_fields(text, extensions=extensions, labels=_LABELED_FORBIDDEN_FIELDS),
    ])


def _terms_from_structured_fields(text: str, *, extensions: str, field_names: frozenset[str]) -> list[str]:
    pattern = re.compile(_FILE_RE_TEMPLATE.format(exts=extensions), re.IGNORECASE)
    values: list[str] = []
    active = False
    for raw in str(text or "").splitlines():
        active, terms = _structured_file_terms_line(raw, active=active, field_names=field_names, pattern=pattern)
        _append_terms(values, terms)
    return values


def _terms_from_labeled_fields(text: str, *, extensions: str, labels: frozenset[str]) -> list[str]:
    pattern = re.compile(_FILE_RE_TEMPLATE.format(exts=extensions), re.IGNORECASE)
    values: list[str] = []
    active = False
    for raw in str(text or "").splitlines():
        active, terms = _labeled_file_terms_line(raw, active=active, labels=labels, pattern=pattern)
        _append_terms(values, terms)
    return values


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


def _labeled_field_from_line(raw: str) -> tuple[str, str] | None:
    for segment in _labeled_line_segments(raw):
        if field := _labeled_field_match(segment):
            return field
    return None


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


def _field_match(line: str) -> tuple[str, str] | None:
    match = _FIELD_RE.match(line)
    if not match:
        return None
    return match.group("field").strip().lower(), match.group("tail").strip()


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


def _labeled_line_segments(raw: str) -> list[str]:
    return [item.strip() for item in re.split(r"[。；;]+", str(raw or "")) if item.strip()]


def _field_continuation_value(line: str) -> str | None:
    if not line:
        return None
    bullet = _BULLET_RE.match(line)
    if bullet:
        return bullet.group("value").strip()
    return line if _looks_like_file_list_line(line) else None


def _looks_like_file_list_line(line: str) -> bool:
    text = str(line or "").strip()
    if not text or "." not in text:
        return False
    cleaned = re.sub(r"[A-Za-z0-9_.\-/]+", "", text)
    return all(ch in " \t,，、;；|[]()（）'\"" for ch in cleaned)


def _file_terms_from_value(value: str, pattern: re.Pattern[str]) -> list[str]:
    normalized = _normalize_file_list_separators(value)
    return [_clean_term(match.group(1)) for match in pattern.finditer(normalized)]


def _normalize_file_list_separators(text: str) -> str:
    return re.sub(
        r"(\.[A-Za-z0-9]{1,8})/(?=[A-Za-z0-9][A-Za-z0-9_.-]*\.[A-Za-z0-9]{1,8}(?![A-Za-z0-9_.-]))",
        r"\1,",
        str(text or ""),
    )


def _clean_term(value: object) -> str:
    return clean_file_contract_term(value)


def clean_file_contract_term(value: object) -> str:
    text = str(value or "").strip().strip("`'\".,;:，。；：、").replace("\\", "/")
    return text[2:] if text.startswith("./") else text


def _append_terms(terms: list[str], values: list[str]) -> None:
    for value in values:
        if value and value not in terms:
            terms.append(value)
