"""受控查询工具。

提供时间窗口和结果数量限制的日志查询功能，防止子代理无限制查询。
第一版实现 file_tail 模板，后续可扩展 parquet_scan、sql_query 等。
"""
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

from .models import QueryResult, utc_now_iso


@dataclass
class BoundedQueryError:
    """受控查询的错误。"""

    error_type: str
    message: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class BoundedQueryConfig:
    """受控查询的配置。"""

    default_max_results: int = 100
    default_max_file_size_mb: float = 10.0
    default_max_lines: int = 10000
    default_tail_lines: int = 1000
    enforce_time_window: bool = True
    allow_absolute_paths: bool = False
    allowed_base_paths: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.allowed_base_paths:
            self.allowed_base_paths = [
                "agent_py_agent/data/log_fixtures",
                "validation/security_fixtures",
            ]


# 全局默认配置
default_config = BoundedQueryConfig()


def bounded_query(
    query_template: str,
    file_path: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    max_results: int | None = None,
    config: BoundedQueryConfig | None = None,
    **params: Any,
) -> QueryResult:
    """执行受控查询。

    Args:
        query_template: 查询模板名称，如 "file_tail"。
        file_path: 日志文件路径（file_tail 模板需要）。
        start_time: 开始时间（ISO 8601）。
        end_time: 结束时间（ISO 8601）。
        max_results: 最大结果数，默认使用配置默认值。
        config: 可选的查询配置。
        **params: 其他查询参数。

    Returns:
        QueryResult 包含查询结果、数量统计和截断标志。

    Raises:
        ValueError: 参数无效时。
    """
    if config is None:
        config = default_config

    if max_results is None:
        max_results = config.default_max_results

    if max_results <= 0:
        max_results = config.default_max_results

    # 验证时间窗口
    time_window = {}
    if start_time:
        time_window["start"] = start_time
    if end_time:
        time_window["end"] = end_time

    if config.enforce_time_window:
        time_window_valid, time_error = _validate_time_window(time_window)
        if not time_window_valid:
            return QueryResult(
                query_template=query_template,
                query_params=params,
                time_window=time_window,
                results=[],
                result_count=0,
                truncated=False,
                max_limit=max_results,
                metadata={"error": time_error.to_dict()},
            )

    # 根据模板执行查询
    if query_template == "file_tail":
        if not file_path:
            return QueryResult(
                query_template=query_template,
                query_params=params,
                time_window=time_window,
                results=[],
                result_count=0,
                truncated=False,
                max_limit=max_results,
                metadata={
                    "error": {
                        "error_type": "missing_parameter",
                        "message": "file_tail 模板需要 file_path 参数",
                        "details": {},
                    }
                },
            )
        return _execute_file_tail(
            file_path=file_path,
            time_window=time_window,
            max_results=max_results,
            config=config,
        )

    # 不支持的模板
    return QueryResult(
        query_template=query_template,
        query_params=params,
        time_window=time_window,
        results=[],
        result_count=0,
        truncated=False,
        max_limit=max_results,
        metadata={
            "error": {
                "error_type": "unsupported_template",
                "message": f"不支持的查询模板: {query_template}",
                "details": {"supported_templates": ["file_tail"]},
            }
        },
    )


def _validate_time_window(time_window: dict[str, str]) -> tuple[bool, BoundedQueryError | None]:
    """验证时间窗口是否有效。"""
    start_str = time_window.get("start", "")
    end_str = time_window.get("end", "")

    if not start_str and not end_str:
        return True, None

    try:
        start = datetime.fromisoformat(start_str.replace("Z", "+00:00")) if start_str else None
        end = datetime.fromisoformat(end_str.replace("Z", "+00:00")) if end_str else None
    except ValueError as e:
        return False, BoundedQueryError(
            error_type="invalid_time_format",
            message="时间格式无效，请使用 ISO 8601 格式",
            details={"error": str(e)},
        )

    if start and end and start > end:
        return False, BoundedQueryError(
            error_type="invalid_time_window",
            message="开始时间不能晚于结束时间",
            details={"start": start_str, "end": end_str},
        )

    # 检查时间窗口是否太大（比如超过 30 天）
    if start and end:
        delta = (end - start).days
        if delta > 30:
            return False, BoundedQueryError(
                error_type="time_window_too_large",
                message=f"时间窗口过大（{delta} 天），请缩小范围",
                details={"days": delta},
            )

    return True, None


def _check_file_path_allowed(file_path: str, config: BoundedQueryConfig) -> tuple[bool, str | None]:
    """检查文件路径是否允许访问。"""
    path = Path(file_path).resolve()

    if not path.exists():
        return False, f"文件不存在: {file_path}"

    if not path.is_file():
        return False, f"路径不是文件: {file_path}"

    # 检查文件大小
    file_size_mb = path.stat().st_size / (1024 * 1024)
    if file_size_mb > config.default_max_file_size_mb:
        return False, f"文件过大（{file_size_mb:.2f} MB），超过限制 {config.default_max_file_size_mb} MB"

    # 检查是否允许访问该路径
    if config.allow_absolute_paths:
        return True, None

    for base in config.allowed_base_paths:
        base_path = Path(base).resolve()
        try:
            path.relative_to(base_path)
            return True, None
        except ValueError:
            continue

    return False, f"不允许访问路径: {file_path}"


def _parse_log_line(line: str) -> dict[str, Any]:
    """简单解析日志行，提取时间戳和内容。"""
    result = {"raw": line, "timestamp": "", "message": line}

    # 尝试解析常见的时间戳格式
    import re

    # ISO 8601 格式
    iso_match = re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", line)
    if iso_match:
        result["timestamp"] = iso_match.group()

    # 简单 syslog 格式
    syslog_match = re.search(r"\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}", line)
    if syslog_match and not result["timestamp"]:
        result["timestamp"] = syslog_match.group()

    # 提取消息（去掉时间戳部分）
    if result["timestamp"]:
        result["message"] = line.replace(result["timestamp"], "", 1).lstrip()

    return result


def _filter_by_time_window(
    lines: Iterable[dict[str, Any]],
    time_window: dict[str, str],
) -> tuple[list[dict[str, Any]], int, int]:
    """按时间窗口过滤日志行。"""
    start_str = time_window.get("start", "")
    end_str = time_window.get("end", "")

    if not start_str and not end_str:
        return list(lines), len(list(lines)), 0

    try:
        start = datetime.fromisoformat(start_str.replace("Z", "+00:00")) if start_str else None
        end = datetime.fromisoformat(end_str.replace("Z", "+00:00")) if end_str else None
    except ValueError:
        return list(lines), len(list(lines)), 0

    filtered = []
    skipped = 0

    for line_dict in lines:
        timestamp_str = line_dict.get("timestamp", "")
        if not timestamp_str:
            # 无法解析时间戳的行，保守策略：跳过
            skipped += 1
            continue

        try:
            # 尝试解析时间戳
            ts = None
            if "T" in timestamp_str or "+" in timestamp_str or "Z" in timestamp_str:
                ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            elif " " in timestamp_str and ":" in timestamp_str:
                # 简单格式，假设是当年
                from datetime import date
                year = date.today().year
                ts = datetime.strptime(f"{year} {timestamp_str}", "%Y %b %d %H:%M:%S")

            if ts is None:
                skipped += 1
                continue

            if start and ts < start:
                skipped += 1
                continue
            if end and ts > end:
                skipped += 1
                continue

            filtered.append(line_dict)
        except (ValueError, TypeError):
            skipped += 1
            continue

    return filtered, len(filtered), skipped


def _execute_file_tail(
    file_path: str,
    time_window: dict[str, str],
    max_results: int,
    config: BoundedQueryConfig,
) -> QueryResult:
    """执行 file_tail 查询。"""
    # 检查文件路径
    allowed, error_msg = _check_file_path_allowed(file_path, config)
    if not allowed:
        return QueryResult(
            query_template="file_tail",
            query_params={"file_path": file_path},
            time_window=time_window,
            results=[],
            result_count=0,
            truncated=False,
            max_limit=max_results,
            metadata={
                "error": {
                    "error_type": "access_denied",
                    "message": error_msg or "",
                    "details": {"file_path": file_path},
                }
            },
        )

    # 读取文件尾部
    tail_lines = config.default_tail_lines
    total_lines = 0
    parsed_lines = []

    try:
        # 简单实现：读取所有行然后取尾部
        # 生产环境应该用更高效的方式（如直接 seek）
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            total_lines = len(all_lines)

            # 取最后 N 行
            lines_to_parse = all_lines[-tail_lines:] if len(all_lines) > tail_lines else all_lines

            for line in lines_to_parse:
                line = line.strip()
                if line:
                    parsed_lines.append(_parse_log_line(line))
    except (IOError, OSError) as e:
        return QueryResult(
            query_template="file_tail",
            query_params={"file_path": file_path},
            time_window=time_window,
            results=[],
            result_count=0,
            truncated=False,
            max_limit=max_results,
            metadata={
                "error": {
                    "error_type": "file_read_error",
                    "message": f"无法读取文件: {e}",
                    "details": {"file_path": file_path, "error": str(e)},
                }
            },
        )

    # 按时间窗口过滤
    filtered_lines, filtered_count, skipped_count = _filter_by_time_window(parsed_lines, time_window)

    # 限制结果数量
    truncated = len(filtered_lines) > max_results
    results = filtered_lines[:max_results]

    metadata = {
        "file_path": file_path,
        "total_lines": total_lines,
        "parsed_lines": len(parsed_lines),
        "filtered_count": filtered_count,
        "skipped_count": skipped_count,
        "truncated": truncated,
        "tail_lines": tail_lines,
    }

    return QueryResult(
        query_template="file_tail",
        query_params={"file_path": file_path},
        time_window=time_window,
        results=results,
        result_count=len(results),
        truncated=truncated,
        max_limit=max_results,
        metadata=metadata,
    )


__all__ = [
    "BoundedQueryConfig",
    "BoundedQueryError",
    "bounded_query",
    "default_config",
]
