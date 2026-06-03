
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..settings.defaults import default_agent_config
from .real_run_review_rules import TAG_RULES

_MARKER_RE = re.compile(r"\[([A-Z][A-Z0-9_]+)\]")
_REPORT_NAME_PARTS = ("report", "acceptance", "validation", "execution")
_LOG_NAMES = {"stdout.txt", "stderr.txt", "events.jsonl"}


@dataclass(frozen=True)
class CollectedFacts:
    codes: tuple[str, ...]
    refs: tuple[str, ...]
    ok_values: tuple[bool, ...]


@dataclass(frozen=True)
class ReviewScanLimits:
    max_report_bytes: int
    max_log_bytes: int


def review_scan_limits(config: object | None) -> ReviewScanLimits:
    if config is None:
        config = default_agent_config()
    return ReviewScanLimits(
        max_report_bytes=_config_int(config, "real_run_review_max_report_bytes"),
        max_log_bytes=_config_int(config, "real_run_review_max_log_bytes"),
    )


def json_facts(root: Path, *, max_report_bytes: int) -> CollectedFacts:
    codes: list[str] = []
    refs: list[str] = []
    ok_values: list[bool] = []
    for path in _candidate_json_files(root, max_report_bytes=max_report_bytes):
        payload = _read_json(path)
        if payload is None:
            continue
        path_codes = _codes_from_payload(payload)
        path_ok_values = _ok_values(payload)
        if path_codes or path_ok_values:
            refs.append(_rel(path, root))
        codes.extend(path_codes)
        ok_values.extend(path_ok_values)
    return CollectedFacts(
        codes=_ordered_unique(codes),
        refs=tuple(refs),
        ok_values=tuple(ok_values),
    )


def marker_facts(root: Path, *, max_log_bytes: int) -> CollectedFacts:
    codes: list[str] = []
    refs: list[str] = []
    for path in _candidate_log_files(root):
        if not _size_allowed(path, max_log_bytes):
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        path_codes = [code for code in _MARKER_RE.findall(content) if _is_failure_marker(code)]
        if path_codes:
            refs.append(_rel(path, root))
            codes.extend(path_codes)
    return CollectedFacts(codes=_ordered_unique(codes), refs=tuple(refs), ok_values=())


def _candidate_json_files(root: Path, *, max_report_bytes: int) -> tuple[Path, ...]:
    paths = [
        path
        for path in root.rglob("*.json")
        if any(part in path.name for part in _REPORT_NAME_PARTS) and _size_allowed(path, max_report_bytes)
    ]
    return tuple(sorted(paths))


def _candidate_log_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in root.rglob("*") if path.is_file() and path.name in _LOG_NAMES))


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


def _codes_from_payload(payload: Any) -> tuple[str, ...]:
    codes: list[str] = []
    _collect_codes(payload, codes)
    return tuple(codes)


def _collect_codes(value: Any, codes: list[str]) -> None:
    if isinstance(value, list):
        _collect_codes_from_list(value, codes)
        return
    if not isinstance(value, dict):
        return
    _collect_code_fields(value, codes)
    _collect_codes_from_list(list(value.values()), codes)


def _collect_code_fields(value: dict[str, Any], codes: list[str]) -> None:
    for key in ("code", "error_code"):
        _append_code_value(value.get(key), codes)
    for key in ("error_codes", "issues", "warning_codes", "blocker_codes"):
        _append_code_value(value.get(key), codes)


def _collect_codes_from_list(values: list[Any], codes: list[str]) -> None:
    for child in values:
        _collect_codes(child, codes)


def _append_code_value(value: Any, codes: list[str]) -> None:
    if isinstance(value, str) and _looks_like_code(value):
        codes.append(value)
    if isinstance(value, list):
        for item in value:
            _append_code_value(item, codes)


def _looks_like_code(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", value.strip()))


def _is_failure_marker(code: str) -> bool:
    return any(code.startswith(prefix) for _, prefixes in TAG_RULES for prefix in prefixes)


def _ok_values(payload: Any) -> tuple[bool, ...]:
    values: list[bool] = []
    _collect_ok_values(payload, values)
    return tuple(values)


def _collect_ok_values(value: Any, values: list[bool]) -> None:
    if isinstance(value, list):
        _collect_ok_values_from_list(value, values)
        return
    if not isinstance(value, dict):
        return
    if isinstance(value.get("ok"), bool):
        values.append(value["ok"])
    _collect_ok_values_from_list(list(value.values()), values)


def _collect_ok_values_from_list(items: list[Any], values: list[bool]) -> None:
    for child in items:
        _collect_ok_values(child, values)


def _size_allowed(path: Path, limit: int) -> bool:
    try:
        return path.stat().st_size <= limit
    except OSError:
        return False


def _ordered_unique(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


def _config_int(config: object, key: str) -> int:
    try:
        return max(0, int(getattr(config, key)))
    except (TypeError, ValueError):
        return 0


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


__all__ = ["CollectedFacts", "ReviewScanLimits", "json_facts", "marker_facts", "review_scan_limits"]
