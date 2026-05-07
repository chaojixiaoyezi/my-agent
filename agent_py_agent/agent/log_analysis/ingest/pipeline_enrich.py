from __future__ import annotations

"""LLM: later-stage ingest pipeline functions — normalization, enrichment, dedup, and output writing.

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
from .pipeline_helpers import (
    WriteManifestParams,
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


@dataclass
class _FinalizeIngestParams:
    """Bundle of all _finalize_ingest_result parameters."""

    pipeline: Any
    batch_id: str
    batch_source_id: str
    source_path: Path
    fmt: str
    parser: LogParser
    started_at: str
    content_hash: str
    size_bytes: int
    counts: _EnrichCounts
    event_ids: list[str]
    dead_letters: DeadLetterWriter
    storage_summary: dict[str, Any]
    cursor_before: dict


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


@dataclass(frozen=True)
class _PrepareIngestRequest:
    path: str | Path
    source_id: str | None
    parser_id: str
    file_format: str | None


@dataclass(frozen=True)
class _PreparedFinalize:
    pipeline: Any
    prepared: _PreparedIngest
    counts: _EnrichCounts
    event_ids: list[str]
    storage_summary: dict[str, Any]


def _finalize_ingest_result(params: _FinalizeIngestParams) -> IngestResult:
    """Write manifest, finish dedup batch, commit checkpoint, return IngestResult."""
    manifest_path = _write_manifest(_manifest_params(params))
    _finish_dedup_batch(params, manifest_path)
    checkpoint = _commit_checkpoint(params)

    return IngestResult(
        batch_id=params.batch_id,
        source_id=params.batch_source_id,
        source_path=str(params.source_path),
        file_format=params.fmt,
        status="stored",
        parsed_count=params.counts.parsed_count,
        stored_count=len(params.event_ids),
        duplicate_count=params.counts.duplicate_count,
        dead_letter_count=params.dead_letters.count,
        skipped_count=params.counts.skipped_count,
        content_hash=params.content_hash,
        manifest_path=str(manifest_path),
        checkpoint_path=str(params.pipeline.checkpoints.path_for(checkpoint.source_id)),
        events_path=params.storage_summary.get("path"),
        dead_letter_refs=params.dead_letters.refs(),
        stored_event_ids=params.event_ids,
    )


def _manifest_params(params: _FinalizeIngestParams) -> WriteManifestParams:
    return WriteManifestParams(
        pipeline=params.pipeline,
        batch_id=params.batch_id,
        source_id=params.batch_source_id,
        source_path=params.source_path,
        file_format=params.fmt,
        parser=params.parser,
        started_at=params.started_at,
        content_hash=params.content_hash,
        size_bytes=params.size_bytes,
        first_event_time=params.counts.first_event_time,
        last_event_time=params.counts.last_event_time,
        parsed_count=params.counts.parsed_count,
        stored_count=len(params.event_ids),
        duplicate_count=params.counts.duplicate_count,
        skipped_count=params.counts.skipped_count,
        dead_letter_count=params.dead_letters.count,
        dead_letter_refs=params.dead_letters.refs(),
        cursor_before=params.cursor_before,
        storage_info=params.storage_summary,
    )


def _finish_dedup_batch(params: _FinalizeIngestParams, manifest_path: Path) -> None:
    params.pipeline.dedup.finish_batch(
        batch_id=params.batch_id,
        status="stored",
        event_count=len(params.event_ids),
        duplicate_count=params.counts.duplicate_count,
        dead_letter_count=params.dead_letters.count,
        manifest_path=str(manifest_path),
    )


def _commit_checkpoint(params: _FinalizeIngestParams):
    return params.pipeline.checkpoints.commit(
        source_id=params.batch_source_id,
        cursor_kind="file_content_hash",
        cursor={
            "path": str(params.source_path),
            "format": params.fmt,
            "size_bytes": params.size_bytes,
            "content_hash": params.content_hash,
            "batch_id": params.batch_id,
        },
        last_committed_batch_id=params.batch_id,
        last_event_time=params.counts.last_event_time,
    )


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
    """LLM: Main orchestration for ingesting a single file — dedup, storage, manifest, checkpoint.

    新手说明:
    一个文件的完整导入流程：选解析器、读文件、去重、写存储、写 manifest、
    写 checkpoint。这是 pipeline.ingest_file 的实际实现，拆到这里避免
    pipeline.py 太长。
    """
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

    counts, event_ids, storage_infos = _process_records(
        pipeline,
        _ProcessRecordsParams(
            source_path=prepared.source_path,
            file_format=prepared.fmt,
            parser=prepared.parser,
            batch_id=prepared.batch_id,
            source_id=prepared.batch_source_id,
            source_product=ingest_options.source_product,
            dead_letters=prepared.dead_letters,
        ),
    )

    storage_summary = _storage_summary(storage_infos, fallback_path=pipeline.fallback_sink.events_path)
    return _finalize_prepared_ingest(_PreparedFinalize(pipeline, prepared, counts, event_ids, storage_summary))


def _finalize_prepared_ingest(finalize: _PreparedFinalize) -> IngestResult:
    prepared = finalize.prepared
    return _finalize_ingest_result(
        _FinalizeIngestParams(
            pipeline=finalize.pipeline,
            batch_id=prepared.batch_id,
            batch_source_id=prepared.batch_source_id,
            source_path=prepared.source_path,
            fmt=prepared.fmt,
            parser=prepared.parser,
            started_at=prepared.started_at,
            content_hash=prepared.content_hash,
            size_bytes=prepared.size_bytes,
            counts=finalize.counts,
            event_ids=finalize.event_ids,
            dead_letters=prepared.dead_letters,
            storage_summary=finalize.storage_summary,
            cursor_before=prepared.cursor_before,
        )
    )


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


def _write_batch_events(pipeline, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for method_name in ("write_events", "append_events", "upsert_events"):
        method = getattr(pipeline.store, method_name, None)
        if callable(method):
            result = method(events)
            return _storage_result(result, count=len(events), store=pipeline.store)
    return None


def _write_item_events(pipeline, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for method_name in ("write_event", "append_event", "upsert_event"):
        method = getattr(pipeline.store, method_name, None)
        if callable(method):
            return _write_events_one_by_one(method, events)
    return None


def _write_events_one_by_one(method: Any, events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in events:
        method(event)
    return {"count": len(events), "path": None}


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
