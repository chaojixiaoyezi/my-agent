
from __future__ import annotations

from ..common.json_io import jsonl_lines

"""read-only control-plane queries for runtime memory.

Human version:
The control plane is the small "where is the thing?" API. It does not load full
tool outputs or rewrite memory files. It only scans lightweight ledgers and
returns scoped references that compact/resume/debug flows can follow.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.tool_index_stream import (
    _PRESCREEN_ESCAPE_PATTERN as _PRESCREEN_ESCAPE_PATTERN,
)
from ..common.tool_index_stream import (
    _skip_record_bytes as _skip_record_bytes,
)
from ..common.tool_index_stream import (
    iter_prescreened_tool_index_lines as iter_prescreened_tool_index_lines,
)
from ..common.tool_index_stream import (
    tool_index_prescreen_pattern as _tool_ref_prescreen_pattern,
)
from ..common.tool_output_paths import tool_output_index_paths_for_lookup
from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_schema_payload,
)

CONTROL_PLANE_QUERY_SCHEMA = RuntimeMemorySchemaOptions("control_plane_query")
CONTROL_PLANE_TASK_RUN_REF_SCHEMA = RuntimeMemorySchemaOptions("control_plane_task_run_ref")
CONTROL_PLANE_DECODE_ERROR_SCHEMA = RuntimeMemorySchemaOptions("control_plane_decode_error")


@dataclass(frozen=True)
class MemoryControlPlaneQueryOptions:
    """Bundle filters for a read-only memory control-plane query."""

    date: str = ""
    task_id: str = ""
    run_id: str = ""
    event_type: str = ""
    include_missing_refs: bool = True
    limit: int = 50


@dataclass(frozen=True)
class _ControlPlaneResultParts:
    workspace: Path
    options: MemoryControlPlaneQueryOptions
    daily_events: list[dict[str, Any]]
    task_run_refs: list[dict[str, Any]]
    compact_applies: list[dict[str, Any]]
    tool_outputs: list[dict[str, Any]]


def query_memory_control_plane(root: str | Path, options: MemoryControlPlaneQueryOptions) -> dict[str, Any]:
    workspace = Path(root)
    daily_events = _limited(_matching_records(_daily_events(workspace, options), options), options.limit)
    compact_applies = _limited(_matching_records(_compact_applies(workspace), options), options.limit)
    tool_outputs = _limited(_tool_outputs(workspace, options), options.limit)
    task_run_refs = _limited(_task_run_refs(daily_events, options), options.limit)
    return _result_payload(
        _ControlPlaneResultParts(workspace, options, daily_events, task_run_refs, compact_applies, tool_outputs)
    )


def _result_payload(parts: _ControlPlaneResultParts) -> dict[str, Any]:
    return {
        "version": CONTROL_PLANE_QUERY_SCHEMA.version,
        "schema": runtime_memory_schema_payload(CONTROL_PLANE_QUERY_SCHEMA),
        "ok": True,
        "workspace_root": str(parts.workspace),
        "scope": _scope_payload(parts.options),
        "daily_events": parts.daily_events,
        "task_run_refs": parts.task_run_refs,
        "compact_applies": parts.compact_applies,
        "tool_outputs": parts.tool_outputs,
        "counts": {
            "daily_events": len(parts.daily_events),
            "task_run_refs": len(parts.task_run_refs),
            "compact_applies": len(parts.compact_applies),
            "tool_outputs": len(parts.tool_outputs),
        },
    }


def _daily_events(workspace: Path, options: MemoryControlPlaneQueryOptions) -> list[dict[str, Any]]:
    paths = [workspace / "daily" / options.date / "events.jsonl"] if options.date else _daily_event_paths(workspace)
    return _read_many_jsonl(paths)


def _daily_event_paths(workspace: Path) -> list[Path]:
    return sorted((workspace / "daily").glob("*/events.jsonl"))


def _compact_applies(workspace: Path) -> list[dict[str, Any]]:
    return _read_jsonl(workspace / "memory_archive" / "compact_applies" / "ledger.jsonl")


# LLM: 通用控制面的嵌套scope、坏行投影及顺序不变；无安全run/task条件时完整解析，不提前截断读取。
# 函数用途: 共用唯一索引读取器返回已匹配的控制面工具记录，不累积不相关历史。
def _tool_outputs(workspace: Path, options: MemoryControlPlaneQueryOptions | None = None) -> list[dict[str, Any]]:
    options = options or MemoryControlPlaneQueryOptions(limit=0)
    prescreen = _tool_ref_prescreen_pattern((options.run_id or options.task_id,))
    rows: list[dict[str, Any]] = []
    for path in tool_output_index_paths_for_lookup(workspace):
        rows.extend(record for number, text in iter_prescreened_tool_index_lines(path, prescreen, universal_newlines=True)
                    if _matches_scope(record := _decode_line(path, number, text), options))
    return rows


def _task_run_refs(
    daily_events: list[dict[str, Any]], options: MemoryControlPlaneQueryOptions
) -> list[dict[str, Any]]:
    refs_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for event in daily_events:
        ref = _task_run_ref(event, options.include_missing_refs)
        if ref:
            refs_by_key[(ref["task_id"], ref["run_id"])] = ref
    return list(refs_by_key.values())


def _task_run_ref(event: dict[str, Any], include_missing_refs: bool) -> dict[str, Any]:
    refs = event.get("refs") if isinstance(event.get("refs"), dict) else {}
    missing_refs = _missing_refs(refs)
    if missing_refs and not include_missing_refs:
        return {}
    return {
        "version": CONTROL_PLANE_TASK_RUN_REF_SCHEMA.version,
        "schema": runtime_memory_schema_payload(CONTROL_PLANE_TASK_RUN_REF_SCHEMA),
        "task_id": str(event.get("task_id") or ""),
        "run_id": str(event.get("run_id") or ""),
        "status": str(event.get("status") or ""),
        "progress": event.get("progress", 0.0),
        "summary": str(event.get("summary") or ""),
        "refs": dict(refs),
        "missing_refs": missing_refs,
    }


def _matching_records(
    records: list[dict[str, Any]], options: MemoryControlPlaneQueryOptions
) -> list[dict[str, Any]]:
    return [record for record in records if _matches_scope(record, options)]


def _matches_scope(record: dict[str, Any], options: MemoryControlPlaneQueryOptions) -> bool:
    return (
        _matches_value(_record_value(record, "task_id"), options.task_id)
        and _matches_value(_record_value(record, "run_id"), options.run_id)
        and _matches_value(str(record.get("event_type") or record.get("kind") or ""), options.event_type)
    )


def _record_value(record: dict[str, Any], key: str) -> str:
    scope = record.get("scope") if isinstance(record.get("scope"), dict) else {}
    return str(record.get(key) or scope.get(key) or "")


def _matches_value(actual: str, expected: str) -> bool:
    return not expected or actual == expected


def _scope_payload(options: MemoryControlPlaneQueryOptions) -> dict[str, Any]:
    return {
        "date": options.date,
        "task_id": options.task_id,
        "run_id": options.run_id,
        "event_type": options.event_type,
        "include_missing_refs": options.include_missing_refs,
        "limit": options.limit,
    }


def _missing_refs(refs: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for key, value in refs.items():
        text = str(value or "")
        if text and _looks_like_path(text) and not Path(text).exists():
            missing.append(str(key))
    return missing


def _looks_like_path(value: str) -> bool:
    return "/" in value or "\\" in value or value.endswith((".json", ".jsonl", ".md"))


def _read_many_jsonl(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        rows.extend(_read_jsonl(path))
    return rows


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    # 顺序必须是"先按 LF 切记录、再 enumerate"：把 enumerate 交给 jsonl_lines 会丢掉行号语义并直接报错。
    # JSONL 记录边界只能是物理 LF：splitlines() 会在 U+0085/U+2028/U+2029 等合法正文字符处切开记录。
    for line_number, line in enumerate(jsonl_lines(path.read_text(encoding="utf-8")), start=1):
        if line.strip():
            rows.append(_decode_line(path, line_number, line))
    return rows


def _decode_line(path: Path, line_number: int, line: str) -> dict[str, Any]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        return _decode_error(path, line_number, exc)
    return payload if isinstance(payload, dict) else _decode_error(path, line_number, None)


def _decode_error(path: Path, line_number: int, exc: json.JSONDecodeError | None) -> dict[str, Any]:
    return {
        "version": CONTROL_PLANE_DECODE_ERROR_SCHEMA.version,
        "schema": runtime_memory_schema_payload(CONTROL_PLANE_DECODE_ERROR_SCHEMA),
        "kind": "control_plane_decode_error",
        "path": str(path),
        "line_number": line_number,
        "error": str(exc) if exc else "JSONL row is not an object",
    }


def _limited(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return records[:limit] if limit > 0 else records


# LLM: curator 批次级读取——一次 glob、一次顺序扫描全部工具输出索引，为每个 run 收集有界匹配行；
#   与 query_memory_control_plane 的 tool_outputs 语义逐条等价：同一文件顺序、同一行序、同一
#   _record_value/_matches_scope 匹配、同一"前 N 条"截断。区别只在"每个 run 各扫一遍"变成"全批扫一遍"。
#   读取失败（OSError/UnicodeDecodeError）与旧路径一样向上抛出：旧路径每个 run 的独立查询都会在同一个
#   坏文件处失败，所以这里抛出即代表"每个 run 各得一条同类型读取错误"。
#   错误合同（mc4-1302，3a 已接受的有意变化）：本读取器只依赖工具输出索引，不再整读
#   daily/*/events.jsonl 与 memory_archive/compact_applies/ledger.jsonl；这两个无关账本损坏（非法
#   UTF-8）时旧路径会给每个 run 报 UnicodeDecodeError、curator 可能因 load_errors 拒绝整批输入，
#   新路径照常返回工具引用。这是行为改进，不是等价修改。
# 函数用途: 按 run_id 集合批量读取工具输出索引里的匹配行（只读元数据，不读 artifact 正文）。
def query_tool_output_refs_for_runs(
    root: str | Path,
    *,
    run_ids: tuple[str, ...],
    limit_per_run: int,
) -> dict[str, Any]:
    workspace = Path(root)
    collected: dict[str, list[dict[str, Any]]] = {run_id: [] for run_id in run_ids}
    options_by_run = {
        run_id: MemoryControlPlaneQueryOptions(run_id=run_id, limit=limit_per_run)
        for run_id in run_ids
    }
    prescreen = _tool_ref_prescreen_pattern(run_ids)
    for path in tool_output_index_paths_for_lookup(workspace):
        _collect_tool_output_rows(path, collected, options_by_run, prescreen)
    return {"ok": True, "tool_outputs_by_run": collected}


# LLM: Curator 的桶与截断规则不变，逐行读取、UTF-8 和保守预筛交给 Compact 也使用的唯一读取器。
# 函数用途: 把一个工具输出索引文件的匹配行收进各 run 的桶里。
def _collect_tool_output_rows(
    path: Path,
    collected: dict[str, list[dict[str, Any]]],
    options_by_run: dict[str, MemoryControlPlaneQueryOptions],
    prescreen: re.Pattern[bytes] | None,
) -> None:
    for line_number, text in iter_prescreened_tool_index_lines(path, prescreen):
        record = _decode_line(path, line_number, text)
        _collect_matching_record(record, collected, options_by_run)


# LLM: 归属判断复用控制面同一套语义：先按 _record_value 找候选 run（顶层或 scope），再走 _matches_scope
#   完整确认（批量契约里 task_id/event_type 为空，不额外筛）；桶满后不再收集——等价于旧路径
#   "先全量过滤、再取前 N 条"的截断结果（超限行既不处理也不报错）。
# 函数用途: 把一条已解析记录收进匹配 run 的桶（若未满）。
def _collect_matching_record(
    record: dict[str, Any],
    collected: dict[str, list[dict[str, Any]]],
    options_by_run: dict[str, MemoryControlPlaneQueryOptions],
) -> None:
    record_run = _record_value(record, "run_id")
    options = options_by_run.get(record_run)
    if options is None:
        return
    bucket = collected[record_run]
    if options.limit > 0 and len(bucket) >= options.limit:
        return
    if not _matches_scope(record, options):
        return
    bucket.append(record)


__all__ = [
    "MemoryControlPlaneQueryOptions",
    "iter_prescreened_tool_index_lines",
    "query_memory_control_plane",
    "query_tool_output_refs_for_runs",
]
