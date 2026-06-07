
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


def required_file_terms_from_text(text: str, *, extensions: str) -> list[str]:
    return _terms_from_structured_fields(text, extensions=extensions, field_names=_REQUIRED_FIELDS)


def forbidden_file_terms_from_text(text: str, *, extensions: str) -> list[str]:
    return _terms_from_structured_fields(text, extensions=extensions, field_names=_FORBIDDEN_FIELDS)


def _terms_from_structured_fields(text: str, *, extensions: str, field_names: frozenset[str]) -> list[str]:
    pattern = re.compile(_FILE_RE_TEMPLATE.format(exts=extensions), re.IGNORECASE)
    values: list[str] = []
    active = False
    for raw in str(text or "").splitlines():
        active, terms = _structured_file_terms_line(raw, active=active, field_names=field_names, pattern=pattern)
        _append_terms(values, terms)
    return values


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
