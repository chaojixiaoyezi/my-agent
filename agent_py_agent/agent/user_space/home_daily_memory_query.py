from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime_errors import runtime_error_report
from .home_layout import MyAgentHomePaths, home_paths


# LLM: Daily 查询只暴露 v2 经历字段；不得保留 role/kind 旧镜像的运行时双读入口。
# 类用途: 描述按日期、actor、event_type 和正文关键词筛选 owner Daily v2 账本的只读条件。
@dataclass(frozen=True)
class DailyMemoryQuery:
    query: str = ""
    date_key: str | None = None
    actor: str = ""
    event_type: str = ""
    limit: int = 20


@dataclass(frozen=True)
class DailyMemoryRecordsReport:
    records: list[dict[str, Any]]
    load_errors: list[dict[str, object]]


def read_daily_memory_records(paths: MyAgentHomePaths | str | Path, request: DailyMemoryQuery) -> list[dict[str, Any]]:
    return read_daily_memory_records_report(paths, request).records


# LLM: 管理查询可以跨日读取，但每行必须先通过 DailyMemoryEvent v2 验证，legacy 行只报错不进入结果。
# 函数用途: 返回匹配的 Daily v2 经历摘要和逐行加载错误，不赋予其长期事实权威。
def read_daily_memory_records_report(paths: MyAgentHomePaths | str | Path, request: DailyMemoryQuery) -> DailyMemoryRecordsReport:
    home = _coerce_home_paths(paths)
    records: list[dict[str, Any]] = []
    load_errors: list[dict[str, object]] = []
    for path in daily_memory_files(home, request.date_key):
        report = _read_daily_file_report(path, request)
        records.extend(report.records)
        load_errors.extend(report.load_errors)
        if _limit_reached(records, request.limit):
            break
    return DailyMemoryRecordsReport(_apply_limit(records, request.limit), load_errors)


def daily_memory_files(paths: MyAgentHomePaths, date_key: str | None) -> list[Path]:
    return _jsonl_files_from_dirs(_daily_memory_dirs(paths), date_key)


def _coerce_home_paths(paths: MyAgentHomePaths | str | Path) -> MyAgentHomePaths:
    if isinstance(paths, MyAgentHomePaths):
        return paths
    return home_paths(paths)


def _daily_memory_dirs(paths: MyAgentHomePaths) -> tuple[Path, ...]:
    owner_daily = getattr(paths, "owner_memory_daily_dir", None)
    return (Path(owner_daily),) if owner_daily else ()


# LLM: A syntactically valid legacy or malformed schema row becomes a reported load error and is
# never projected as a v2 Daily event.
# 函数用途: 逐行严格读取一个 Daily v2 日分片并附加只读定位信息。
def _read_daily_file_report(path: Path, request: DailyMemoryQuery) -> DailyMemoryRecordsReport:
    records: list[dict[str, Any]] = []
    load_errors: list[dict[str, object]] = []
    date_key = path.stem
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return DailyMemoryRecordsReport([], [_daily_memory_load_error(path, exc, line_no=0)])
    for line_no, line in enumerate(lines, start=1):
        obj, load_error = _parse_json_line_report(line, path=path, line_no=line_no)
        if load_error is not None:
            load_errors.append(load_error)
        if not obj:
            continue
        normalized, schema_error = _normalize_daily_record_report(obj, path=path, line_no=line_no)
        if schema_error is not None:
            load_errors.append(schema_error)
            continue
        if not _daily_record_matches(normalized, request):
            continue
        normalized["date"] = date_key
        normalized["path"] = str(path)
        normalized["line_no"] = line_no
        records.append(normalized)
    return DailyMemoryRecordsReport(records, load_errors)


# LLM: actor/event_type 是 v2 的唯一过滤维度；旧 role/kind 不得被兼容解释。
# 函数用途: 判断一条已验证 Daily v2 记录是否满足管理员查询条件。
def _daily_record_matches(record: dict[str, Any], request: DailyMemoryQuery) -> bool:
    if request.actor and str(record.get("actor") or "") != request.actor:
        return False
    if request.event_type and str(record.get("event_type") or "") != request.event_type:
        return False
    return _text_contains(record, request.query)


# LLM: JSON 语法正确仍不等于合法 Daily；必须验证 schema_version、枚举和权威顺序链字段。
# 函数用途: 把一个原始 JSON 对象严格恢复并规范化为 Daily v2 记录。
def _normalize_daily_record_report(
    payload: dict[str, Any],
    *,
    path: Path,
    line_no: int,
) -> tuple[dict[str, Any], dict[str, object] | None]:
    try:
        # 循环导入根修: 本模块被 user_space/__init__ 顶层加载(经 home_runtime_query)，而
        #   memory_store.daily 又 import user_space.owner_quota → 包加载期回环。函数内 lazy
        #   import，运行期行为不变，只在真正读 Daily v2 时解析 memory_store。
        from ..memory_store.daily import DailyMemoryEvent

        event = DailyMemoryEvent.from_record(payload)
        return event.to_record(), None
    except (TypeError, ValueError) as exc:
        return {}, _daily_memory_load_error(path, exc, line_no=line_no)


def _jsonl_files_from_dirs(directories: tuple[Path, ...], date_key: str | None) -> list[Path]:
    files: list[Path] = []
    for directory in directories:
        if date_key:
            files.extend(_dated_jsonl_file(directory, date_key))
            continue
        files.extend(_all_jsonl_files(directory))
    return sorted(dict.fromkeys(files), reverse=True)


def _dated_jsonl_file(directory: Path, date_key: str) -> list[Path]:
    path = directory / f"{date_key}.jsonl"
    return [path] if path.exists() and path.is_file() else []


def _all_jsonl_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return [path for path in directory.glob("*.jsonl") if path.is_file()]


def _parse_json_line_report(line: str, *, path: Path, line_no: int) -> tuple[dict[str, Any], dict[str, object] | None]:
    if not line.strip():
        return {}, None
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        return {}, _daily_memory_load_error(path, exc, line_no=line_no)
    if isinstance(value, dict):
        return value, None
    return {}, _daily_memory_load_error(
        path,
        ValueError(f"JSONL row is {type(value).__name__}, expected object"),
        line_no=line_no,
    )


def _daily_memory_load_error(path: Path, exc: BaseException, *, line_no: int) -> dict[str, object]:
    report = runtime_error_report(exc, context="home_runtime_query.daily_memory")
    report["path"] = str(path)
    if line_no:
        report["line"] = line_no
    return report


def _text_contains(value: dict[str, Any], query: str) -> bool:
    text = str(query or "").strip().lower()
    if not text:
        return True
    return text in json.dumps(value, ensure_ascii=False, sort_keys=True).lower()


def _limit_reached(items: list[dict[str, Any]], limit: int) -> bool:
    return int(limit or 0) > 0 and len(items) >= int(limit)


def _apply_limit(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    value = int(limit or 0)
    return items[:value] if value > 0 else items


__all__ = [
    "DailyMemoryQuery",
    "DailyMemoryRecordsReport",
    "daily_memory_files",
    "read_daily_memory_records",
    "read_daily_memory_records_report",
]
