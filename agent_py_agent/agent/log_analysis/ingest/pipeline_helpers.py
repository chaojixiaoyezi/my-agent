# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

"""Shared helpers extracted from pipeline_enrich.py to keep other modules lean."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..parsers.base import LogParser, ParserError


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 normalize_file_format 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 normalize file format 涉及的字段，让后续匹配和存储使用同一形态。
def normalize_file_format(value: str) -> str:
    """Normalize a file format string to a canonical ingest format name."""
    fmt = value.lower().lstrip(".") or "jsonl"
    if fmt == "json":
        return "jsonl"
    if fmt in {"jsonl", "csv", "log"}:
        return fmt
    raise ParserError(f"unsupported file format: {value}")


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 make_batch_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 make batch id 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def make_batch_id(*, source_id: str, source_path: str, content_hash: str) -> str:
    """Generate a deterministic batch ID from source identity and content hash."""
    from ..parsers.common import sha256_json

    digest = sha256_json(
        {
            "source_id": source_id,
            "source_path": source_path,
            "content_hash": content_hash,
        }
    )
    return f"batch-{digest[:24]}"


from dataclasses import dataclass


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 WriteManifestParams 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 WriteManifestParams 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class WriteManifestParams:
    """Bundle of write_manifest parameters."""

    pipeline: Any
    batch_id: str
    source_id: str
    source_path: Path
    file_format: str
    parser: LogParser
    started_at: str
    content_hash: str
    size_bytes: int
    first_event_time: str | None
    last_event_time: str | None
    parsed_count: int
    stored_count: int
    duplicate_count: int
    skipped_count: int
    dead_letter_count: int
    dead_letter_refs: list[dict[str, Any]]
    cursor_before: Mapping[str, Any]
    storage_info: Mapping[str, Any]


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 write_manifest 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write manifest 相关记录，集中处理目标路径、格式化和状态更新。
def write_manifest(params: WriteManifestParams) -> Path:
    """Write a manifest JSON file summarizing the ingest batch results."""
    from ..parsers.common import utc_now
    from .checkpoint import safe_source_id, write_json_atomic

    safe_source = safe_source_id(params.source_id)
    manifest_path = params.pipeline.root / "manifests" / safe_source / f"{params.batch_id}.json"
    cursor_after = {
        "path": str(params.source_path),
        "format": params.file_format,
        "size_bytes": params.size_bytes,
        "content_hash": params.content_hash,
        "batch_id": params.batch_id,
    }
    manifest = {
        "batch_id": params.batch_id,
        "source_id": params.source_id,
        "source_kind": "file",
        "source_path": str(params.source_path),
        "format": params.file_format,
        "parser_id": params.parser.parser_id,
        "parser_schema": params.parser.schema,
        "received_at": params.started_at,
        "completed_at": utc_now(),
        "time_range": [params.first_event_time, params.last_event_time],
        "raw_refs": [str(params.source_path)],
        "size_bytes": params.size_bytes,
        "content_hash": params.content_hash,
        "cursor_before": dict(params.cursor_before),
        "cursor_after": cursor_after,
        "dedup_policy": "source_event_fingerprint",
        "checkpoint_policy": "after_durable_write",
        "status": "stored",
        "counts": {
            "parsed": params.parsed_count,
            "stored": params.stored_count,
            "duplicates": params.duplicate_count,
            "skipped": params.skipped_count,
            "dead_letter": params.dead_letter_count,
        },
        "storage": dict(params.storage_info),
        "dead_letter_refs": params.dead_letter_refs,
    }
    write_json_atomic(manifest_path, manifest)
    return manifest_path


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _storage_result 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 storage result 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
def _storage_result(result: Any, *, count: int, store: Any | None = None) -> dict[str, Any]:
    """Normalize a store write return value into a standard dict with count and path."""
    store_path = getattr(store, "events_path", None)
    if isinstance(result, Mapping):
        payload = dict(result)
        payload.setdefault("count", count)
        if payload.get("path") is None and store_path is not None:
            payload["path"] = str(store_path).replace("\\", "/")
        return payload
    if isinstance(result, (str, Path)):
        return {"count": count, "path": str(result).replace("\\", "/")}
    if isinstance(result, int):
        return {"count": result, "path": str(store_path).replace("\\", "/") if store_path is not None else None}
    return {"count": count, "path": str(store_path).replace("\\", "/") if store_path is not None else None}


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _storage_summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 storage summary 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
def _storage_summary(infos: list[dict[str, Any]], *, fallback_path: Path) -> dict[str, Any]:
    """Aggregate multiple storage result dicts into a single summary."""
    count = sum(int(info.get("count") or 0) for info in infos)
    paths = sorted({str(info.get("path")) for info in infos if info.get("path")})
    if not paths:
        paths = [str(fallback_path)]
    summary: dict[str, Any] = {"count": count, "path": paths[0]}
    if len(paths) > 1:
        summary["paths"] = paths
    return summary


from dataclasses import dataclass


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _EnrichCounts 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _EnrichCounts 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _EnrichCounts:
    """Internal counter bundle returned by _process_records."""

    parsed_count: int
    duplicate_count: int
    skipped_count: int
    first_event_time: str | None
    last_event_time: str | None
