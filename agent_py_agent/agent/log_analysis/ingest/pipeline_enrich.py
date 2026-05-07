# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""later-stage ingest pipeline functions — normalization, enrichment, dedup, and output writing.

给人看的解释：
这个文件放 pipeline 的后半段：格式规范化、去重、事件写入存储、manifest 生成。
从 pipeline.py 拆出来，让解析/迭代和写入/去重各归一处。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..parsers.base import LogParser
from ..parsers.common import utc_now
from .checkpoint import safe_source_id, write_json_atomic
from .dead_letter import DeadLetterWriter
from .pipeline import IngestFileOptions, IngestResult, file_digest
from .pipeline_finalize import PreparedFinalize, finalize_prepared_ingest
from .pipeline_helpers import (
    _EnrichCounts,
    _storage_result,
    _storage_summary,
    make_batch_id,
    normalize_file_format,
)
from .pipeline_helpers import (
    write_manifest as _write_manifest,
)
from .pipeline_processing import _ProcessRecordsParams
from .pipeline_processing import process_records as _process_records

# Re-export for backward compatibility
write_manifest = _write_manifest


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _PreparedIngest 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _PreparedIngest 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class _PreparedIngest:
    source_path: Path
    fmt: str
    batch_source_id: str
    size_bytes: int
    content_hash: str
    batch_id: str
    parser: LogParser
    dead_letters: DeadLetterWriter
    started_at: str
    cursor_before: dict


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _PrepareIngestRequest 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _PrepareIngestRequest 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _PrepareIngestRequest:
    path: str | Path
    source_id: str | None
    parser_id: str
    file_format: str | None


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 enrich_ingest_file 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 enrich ingest file 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
def enrich_ingest_file(
    pipeline,  # IngestPipeline — lazy to avoid circular import
    path: str | Path,
    *,
    options: IngestFileOptions | None = None,
    source_id: str | None = None,
    source_product: str | None = None,
    parser_id: str = "security_alert_v1",
    file_format: str | None = None,
) -> IngestResult:
    """Main orchestration for one-file ingest: dedup, storage, manifest, checkpoint."""
    ingest_options = options or IngestFileOptions(
        source_id=source_id,
        source_product=source_product,
        parser_id=str(parser_id),
        file_format=file_format,
    )
    prepared = _prepare_ingest(
        pipeline,
        _PrepareIngestRequest(
            path=path,
            source_id=ingest_options.source_id,
            parser_id=ingest_options.parser_id,
            file_format=ingest_options.file_format,
        ),
    )

    pipeline.dedup.begin_batch(
        batch_id=prepared.batch_id,
        source_id=prepared.batch_source_id,
        content_hash=prepared.content_hash,
        source_path=str(prepared.source_path),
    )

    counts, event_ids, storage_infos = _process_prepared_ingest(pipeline, prepared, ingest_options.source_product)
    storage_summary = _storage_summary(storage_infos, fallback_path=pipeline.fallback_sink.events_path)
    return finalize_prepared_ingest(PreparedFinalize(pipeline, prepared, counts, event_ids, storage_summary))


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _process_prepared_ingest 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 推进 process prepared ingest 对应的调度、执行或处理步骤，并返回可追踪的状态结果。
def _process_prepared_ingest(pipeline, prepared: _PreparedIngest, source_product: str | None):
    return _process_records(
        pipeline,
        _ProcessRecordsParams(
            source_path=prepared.source_path,
            file_format=prepared.fmt,
            parser=prepared.parser,
            batch_id=prepared.batch_id,
            source_id=prepared.batch_source_id,
            source_product=source_product,
            dead_letters=prepared.dead_letters,
        ),
    )


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _prepare_ingest 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 prepare ingest 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
def _prepare_ingest(pipeline, request: _PrepareIngestRequest) -> _PreparedIngest:
    source_path = Path(request.path)
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    fmt = normalize_file_format(request.file_format or source_path.suffix.lstrip("."))
    batch_source_id = request.source_id or source_path.stem
    size_bytes, content_hash = file_digest(source_path)
    batch_id = make_batch_id(source_id=batch_source_id, source_path=str(source_path.resolve()), content_hash=content_hash)
    parser = pipeline.registry.choose(parser_id=request.parser_id, file_format=fmt)
    return _PreparedIngest(
        source_path=source_path,
        fmt=fmt,
        batch_source_id=batch_source_id,
        size_bytes=size_bytes,
        content_hash=content_hash,
        batch_id=batch_id,
        parser=parser,
        dead_letters=DeadLetterWriter(pipeline.root, source_id=batch_source_id, batch_id=batch_id),
        started_at=utc_now(),
        cursor_before=pipeline.checkpoints.load(batch_source_id).get("cursor", {}),
    )


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 write_events 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write events 相关记录，集中处理目标路径、格式化和状态更新。
def write_events(pipeline, events: list[dict[str, Any]]) -> dict[str, Any]:
    """Write a list of events to the configured store or fallback JSONL sink."""
    if not events:
        return {"count": 0, "path": str(pipeline.fallback_sink.events_path)}

    if pipeline.store is None:
        return pipeline.fallback_sink.write_events(events)

    batch_result = _write_batch_events(pipeline, events)
    if batch_result is not None:
        return batch_result
    item_result = _write_item_events(pipeline, events)
    return item_result if item_result is not None else pipeline.fallback_sink.write_events(events)


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _write_batch_events 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write batch events 相关记录，集中处理目标路径、格式化和状态更新。
def _write_batch_events(pipeline, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for method_name in ("write_events", "append_events", "upsert_events"):
        method = getattr(pipeline.store, method_name, None)
        if callable(method):
            result = method(events)
            return _storage_result(result, count=len(events), store=pipeline.store)
    return None


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _write_item_events 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write item events 相关记录，集中处理目标路径、格式化和状态更新。
def _write_item_events(pipeline, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for method_name in ("write_event", "append_event", "upsert_event"):
        method = getattr(pipeline.store, method_name, None)
        if callable(method):
            return _write_events_one_by_one(method, events)
    return None


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _write_events_one_by_one 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write events one by one 相关记录，集中处理目标路径、格式化和状态更新。
def _write_events_one_by_one(method: Any, events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in events:
        method(event)
    return {"count": len(events), "path": None}


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 flush_events 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 flush events 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
def flush_events(
    pipeline,
    events: list[dict[str, Any]],
    *,
    batch_id: str,
) -> tuple[dict[str, Any], list[str]]:
    """Flush buffered events to storage and register them in the dedup store."""
    if not events:
        return {"count": 0, "path": str(pipeline.fallback_sink.events_path)}, []
    storage_info = write_events(pipeline, events)
    stored_event_ids: list[str] = []
    for event in events:
        if pipeline.dedup.mark_event(
            dedup_key=str(event["dedup_key"]),
            event_id=str(event["event_id"]),
            source_id=str(event["source_id"]),
            batch_id=batch_id,
        ):
            stored_event_ids.append(str(event["event_id"]))
    return storage_info, stored_event_ids
