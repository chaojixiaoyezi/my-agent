
from __future__ import annotations

"""Execution helpers for bounded file-tail log queries."""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .bounded_query_models import BoundedQueryConfig, BoundedQueryError
from .models_work_orders import QueryResult


@dataclass(frozen=True)
class _QueryErrorInput:
    error_type: str
    message: str
    file_path: str
    time_window: dict[str, str]
    max_results: int
    details: dict[str, Any] = field(default_factory=dict)


def validate_time_window(time_window: dict[str, str]) -> tuple[bool, BoundedQueryError | None]:
    """验证时间窗口是否有效。"""
    start_str = time_window.get("start", "")
    end_str = time_window.get("end", "")
    if not start_str and not end_str:
        return True, None

    try:
        start = datetime.fromisoformat(start_str.replace("Z", "+00:00")) if start_str else None
        end = datetime.fromisoformat(end_str.replace("Z", "+00:00")) if end_str else None
    except ValueError as exc:
        return False, BoundedQueryError("invalid_time_format", "时间格式无效，请使用 ISO 8601 格式", {"error": str(exc)})

    if start and end and start > end:
        return False, BoundedQueryError("invalid_time_window", "开始时间不能晚于结束时间", {"start": start_str, "end": end_str})
    if start and end and (end - start).days > 30:
        days = (end - start).days
        return False, BoundedQueryError("time_window_too_large", f"时间窗口过大（{days} 天），请缩小范围", {"days": days})
    return True, None


def execute_file_tail(file_path: str, time_window: dict[str, str], max_results: int, config: BoundedQueryConfig) -> QueryResult:
    """执行 file_tail 查询。"""
    allowed, error_msg = check_file_path_allowed(file_path, config)
    if not allowed:
        return _query_error(_QueryErrorInput("access_denied", error_msg or "", file_path, time_window, max_results))

    try:
        total_lines, parsed_lines = read_tail_lines(file_path, config.default_tail_lines)
    except OSError as exc:
        return _query_error(_QueryErrorInput("file_read_error", f"无法读取文件: {exc}", file_path, time_window, max_results, {"error": str(exc)}))

    filtered_lines, filtered_count, skipped_count = filter_by_time_window(parsed_lines, time_window)
    truncated = len(filtered_lines) > max_results
    return QueryResult(
        query_template="file_tail",
        query_params={"file_path": file_path},
        time_window=time_window,
        results=filtered_lines[:max_results],
        result_count=min(len(filtered_lines), max_results),
        truncated=truncated,
        max_limit=max_results,
        metadata={
            "file_path": file_path,
            "total_lines": total_lines,
            "parsed_lines": len(parsed_lines),
            "filtered_count": filtered_count,
            "skipped_count": skipped_count,
            "truncated": truncated,
            "tail_lines": config.default_tail_lines,
        },
    )


def check_file_path_allowed(file_path: str, config: BoundedQueryConfig) -> tuple[bool, str | None]:
    """检查文件路径是否允许访问。"""
    path = Path(file_path).resolve()
    if not path.exists():
        return False, f"文件不存在: {file_path}"
    if not path.is_file():
        return False, f"路径不是文件: {file_path}"
    file_size_mb = path.stat().st_size / (1024 * 1024)
    if file_size_mb > config.default_max_file_size_mb:
        return False, f"文件过大（{file_size_mb:.2f} MB），超过限制 {config.default_max_file_size_mb} MB"
    if config.allow_absolute_paths or any(_within_base(path, base) for base in config.allowed_base_paths):
        return True, None
    return False, f"不允许访问路径: {file_path}"


def read_tail_lines(file_path: str, tail_lines: int) -> tuple[int, list[dict[str, Any]]]:
    """Read a bounded tail from a text log file and parse non-empty lines."""
    with open(file_path, encoding="utf-8", errors="replace") as handle:
        all_lines = handle.readlines()
    lines_to_parse = all_lines[-tail_lines:] if len(all_lines) > tail_lines else all_lines
    return len(all_lines), [parse_log_line(line.strip()) for line in lines_to_parse if line.strip()]


def parse_log_line(line: str) -> dict[str, Any]:
    """简单解析日志行，提取时间戳和内容。"""
    result = {"raw": line, "timestamp": "", "message": line}
    iso_match = re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", line)
    syslog_match = re.search(r"\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}", line)
    if iso_match:
        result["timestamp"] = iso_match.group()
    if syslog_match and not result["timestamp"]:
        result["timestamp"] = syslog_match.group()
    if result["timestamp"]:
        result["message"] = line.replace(result["timestamp"], "", 1).lstrip()
    return result


def filter_by_time_window(lines: Iterable[dict[str, Any]], time_window: dict[str, str]) -> tuple[list[dict[str, Any]], int, int]:
    """按时间窗口过滤日志行。"""
    items = list(lines)
    start, end = _parse_window_bounds(time_window)
    if start is None and end is None:
        return items, len(items), 0

    filtered: list[dict[str, Any]] = []
    skipped = 0
    for line_dict in items:
        timestamp = parse_line_timestamp(str(line_dict.get("timestamp", "")))
        if timestamp is None or (start and timestamp < start) or (end and timestamp > end):
            skipped += 1
            continue
        filtered.append(line_dict)
    return filtered, len(filtered), skipped


def parse_line_timestamp(timestamp_str: str) -> datetime | None:
    """Parse supported line timestamp formats."""
    if not timestamp_str:
        return None
    try:
        if "T" in timestamp_str or "+" in timestamp_str or "Z" in timestamp_str:
            return datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        if " " in timestamp_str and ":" in timestamp_str:
            return datetime.strptime(f"{date.today().year} {timestamp_str}", "%Y %b %d %H:%M:%S")
    except (ValueError, TypeError):
        return None
    return None


def _parse_window_bounds(time_window: dict[str, str]) -> tuple[datetime | None, datetime | None]:
    try:
        start = datetime.fromisoformat(time_window["start"].replace("Z", "+00:00")) if time_window.get("start") else None
        end = datetime.fromisoformat(time_window["end"].replace("Z", "+00:00")) if time_window.get("end") else None
    except ValueError:
        return None, None
    return start, end


def _within_base(path: Path, base: str) -> bool:
    try:
        path.relative_to(Path(base).resolve())
        return True
    except ValueError:
        return False


def _query_error(data: _QueryErrorInput) -> QueryResult:
    return QueryResult(
        query_template="file_tail",
        query_params={"file_path": data.file_path},
        time_window=data.time_window,
        results=[],
        result_count=0,
        truncated=False,
        max_limit=data.max_results,
        metadata={"error": {"error_type": data.error_type, "message": data.message, "details": {"file_path": data.file_path, **data.details}}},
    )


__all__ = [
    "check_file_path_allowed",
    "execute_file_tail",
    "filter_by_time_window",
    "parse_line_timestamp",
    "parse_log_line",
    "read_tail_lines",
    "validate_time_window",
]
