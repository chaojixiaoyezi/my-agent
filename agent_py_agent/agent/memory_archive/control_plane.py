# LLM: Memory archive control plane; keep this module read-only and bundle-driven.
# 模块用途: 汇总 daily ledger、task/run refs、compact apply 和 tool output index，提供最小统一查询入口。

from __future__ import annotations

"""read-only control-plane queries for runtime memory.

Human version:
The control plane is the small "where is the thing?" API. It does not load full
tool outputs or rewrite memory files. It only scans lightweight ledgers and
returns scoped references that compact/resume/debug flows can follow.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)

CONTROL_PLANE_QUERY_SCHEMA = RuntimeMemorySchemaOptions("control_plane_query")
CONTROL_PLANE_TASK_RUN_REF_SCHEMA = RuntimeMemorySchemaOptions("control_plane_task_run_ref")
CONTROL_PLANE_DECODE_ERROR_SCHEMA = RuntimeMemorySchemaOptions("control_plane_decode_error")


# LLM: MemoryControlPlaneQueryOptions is the public query bundle; add future filters here, not as kwargs.
# 类用途: 描述 runtime memory 控制面的查询范围，包括日期、task/run、事件类型、缺失引用策略和数量限制。
@dataclass(frozen=True)
class MemoryControlPlaneQueryOptions:
    """Bundle filters for a read-only memory control-plane query."""

    date: str = ""
    task_id: str = ""
    run_id: str = ""
    event_type: str = ""
    include_missing_refs: bool = True
    limit: int = 50


# LLM: _ControlPlaneResultParts keeps response assembly bundle-based and under code-size param limits.
# 类用途: 汇总 control-plane 查询已经读出的各类结果，交给 payload renderer 生成最终 JSON。
@dataclass(frozen=True)
class _ControlPlaneResultParts:
    workspace: Path
    options: MemoryControlPlaneQueryOptions
    daily_events: list[dict[str, Any]]
    task_run_refs: list[dict[str, Any]]
    compact_applies: list[dict[str, Any]]
    tool_outputs: list[dict[str, Any]]


# LLM: query_memory_control_plane is the single minimal read API for runtime memory indexes.
# 函数用途: 读取 daily ledger、compact apply ledger、tool output index，并返回同一 scope 下的 task/run 引用摘要。
def query_memory_control_plane(root: str | Path, options: MemoryControlPlaneQueryOptions) -> dict[str, Any]:
    workspace = Path(root)
    daily_events = _limited(_matching_records(_daily_events(workspace, options), options), options.limit)
    compact_applies = _limited(_matching_records(_compact_applies(workspace), options), options.limit)
    tool_outputs = _limited(_matching_records(_tool_outputs(workspace), options), options.limit)
    task_run_refs = _limited(_task_run_refs(daily_events, options), options.limit)
    return _result_payload(
        _ControlPlaneResultParts(workspace, options, daily_events, task_run_refs, compact_applies, tool_outputs)
    )


# LLM: _result_payload defines the control-plane response shape; keep it compact and schema-friendly.
# 函数用途: 组装统一查询结果，附带 scope、counts 和 reserved 字段，供 CLI、compact 和调试流程消费。
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
        "reserved": runtime_memory_reserved_fields(CONTROL_PLANE_QUERY_SCHEMA),
    }


# LLM: _daily_events scans only daily/YYYY-MM-DD/events.jsonl index files.
# 函数用途: 根据 date filter 找到 daily ledger 文件并读取 JSONL 事件。
def _daily_events(workspace: Path, options: MemoryControlPlaneQueryOptions) -> list[dict[str, Any]]:
    paths = [workspace / "daily" / options.date / "events.jsonl"] if options.date else _daily_event_paths(workspace)
    return _read_many_jsonl(paths)


# LLM: _daily_event_paths keeps daily ledger discovery deterministic for repeatable tests and CLI output.
# 函数用途: 返回 runtime memory 下所有 daily event JSONL 路径，按日期路径排序。
def _daily_event_paths(workspace: Path) -> list[Path]:
    return sorted((workspace / "daily").glob("*/events.jsonl"))


# LLM: _compact_applies reads the append-only compact apply ledger, never metadata bodies.
# 函数用途: 读取 memory_archive/compact_applies/ledger.jsonl 的 compact apply 摘要记录。
def _compact_applies(workspace: Path) -> list[dict[str, Any]]:
    return _read_jsonl(workspace / "memory_archive" / "compact_applies" / "ledger.jsonl")


# LLM: _tool_outputs reads the lightweight tool output index, not the externalized content files.
# 函数用途: 读取 memory_archive/artifacts/tool_outputs/index.jsonl 的工具输出 artifact 摘要。
def _tool_outputs(workspace: Path) -> list[dict[str, Any]]:
    return _read_jsonl(workspace / "memory_archive" / "artifacts" / "tool_outputs" / "index.jsonl")


# LLM: _task_run_refs extracts task/run navigation records from daily event refs.
# 函数用途: 从 daily event 中提取 task/run/workspace 引用，并按缺失引用策略过滤。
def _task_run_refs(
    daily_events: list[dict[str, Any]], options: MemoryControlPlaneQueryOptions
) -> list[dict[str, Any]]:
    refs_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for event in daily_events:
        ref = _task_run_ref(event, options.include_missing_refs)
        if ref:
            refs_by_key[(ref["task_id"], ref["run_id"])] = ref
    return list(refs_by_key.values())


# LLM: _task_run_ref normalizes one daily event into a compact navigation record.
# 函数用途: 返回一个 task/run 引用摘要，包含状态、摘要、refs 和缺失路径列表。
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
        "reserved": runtime_memory_reserved_fields(CONTROL_PLANE_TASK_RUN_REF_SCHEMA),
    }


# LLM: _matching_records applies business filters consistently across daily, compact and tool indexes.
# 函数用途: 按 task_id、run_id 和 event_type 对不同来源的索引记录做统一筛选。
def _matching_records(
    records: list[dict[str, Any]], options: MemoryControlPlaneQueryOptions
) -> list[dict[str, Any]]:
    return [record for record in records if _matches_scope(record, options)]


# LLM: _matches_scope understands both top-level ids and nested compact scope ids.
# 函数用途: 判断一条索引记录是否满足控制面查询范围。
def _matches_scope(record: dict[str, Any], options: MemoryControlPlaneQueryOptions) -> bool:
    return (
        _matches_value(_record_value(record, "task_id"), options.task_id)
        and _matches_value(_record_value(record, "run_id"), options.run_id)
        and _matches_value(str(record.get("event_type") or record.get("kind") or ""), options.event_type)
    )


# LLM: _record_value checks top-level fields first, then compact apply scope fields.
# 函数用途: 从不同 ledger 形态中取出 task_id 或 run_id，隐藏来源差异。
def _record_value(record: dict[str, Any], key: str) -> str:
    scope = record.get("scope") if isinstance(record.get("scope"), dict) else {}
    return str(record.get(key) or scope.get(key) or "")


# LLM: _matches_value treats an empty expected value as wildcard.
# 函数用途: 执行空值通配的字符串等值匹配。
def _matches_value(actual: str, expected: str) -> bool:
    return not expected or actual == expected


# LLM: _scope_payload mirrors the public query bundle without leaking implementation paths.
# 函数用途: 将查询参数转成 JSON 结果中的 scope 字段。
def _scope_payload(options: MemoryControlPlaneQueryOptions) -> dict[str, Any]:
    return {
        "date": options.date,
        "task_id": options.task_id,
        "run_id": options.run_id,
        "event_type": options.event_type,
        "include_missing_refs": options.include_missing_refs,
        "limit": options.limit,
        "reserved": {},
    }


# LLM: _missing_refs is intentionally best-effort; it flags file-like refs without failing the query.
# 函数用途: 检查 refs 中看起来像路径的字段，把当前不存在的引用名列出来。
def _missing_refs(refs: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for key, value in refs.items():
        text = str(value or "")
        if text and _looks_like_path(text) and not Path(text).exists():
            missing.append(str(key))
    return missing


# LLM: _looks_like_path avoids treating opaque ids as filesystem refs.
# 函数用途: 粗略判断字符串是否像文件路径，避免误报普通标识符。
def _looks_like_path(value: str) -> bool:
    return "/" in value or "\\" in value or value.endswith((".json", ".jsonl", ".md"))


# LLM: _read_many_jsonl keeps source order stable while tolerating missing ledgers.
# 函数用途: 顺序读取多个 JSONL 文件并合并记录。
def _read_many_jsonl(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        rows.extend(_read_jsonl(path))
    return rows


# LLM: _read_jsonl is a forgiving ledger reader; malformed lines become control-plane diagnostics.
# 函数用途: 读取 JSONL 文件，跳过空行，并把无法解析的行转成 archive_error 记录。
def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            rows.append(_decode_line(path, line_number, line))
    return rows


# LLM: _decode_line prevents one malformed ledger line from breaking the whole query.
# 函数用途: 解析一行 JSON，失败时返回带路径和行号的诊断记录。
def _decode_line(path: Path, line_number: int, line: str) -> dict[str, Any]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        return _decode_error(path, line_number, exc)
    return payload if isinstance(payload, dict) else _decode_error(path, line_number, None)


# LLM: _decode_error creates machine-readable diagnostics for corrupt index rows.
# 函数用途: 生成 JSONL 解析失败或非对象记录的错误摘要。
def _decode_error(path: Path, line_number: int, exc: json.JSONDecodeError | None) -> dict[str, Any]:
    return {
        "version": CONTROL_PLANE_DECODE_ERROR_SCHEMA.version,
        "schema": runtime_memory_schema_payload(CONTROL_PLANE_DECODE_ERROR_SCHEMA),
        "kind": "control_plane_decode_error",
        "path": str(path),
        "line_number": line_number,
        "error": str(exc) if exc else "JSONL row is not an object",
        "reserved": runtime_memory_reserved_fields(CONTROL_PLANE_DECODE_ERROR_SCHEMA),
    }


# LLM: _limited centralizes query limit semantics; limit <= 0 means no cap.
# 函数用途: 对查询结果应用数量上限，保持调用方输出可控。
def _limited(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return records[:limit] if limit > 0 else records


__all__ = ["MemoryControlPlaneQueryOptions", "query_memory_control_plane"]
