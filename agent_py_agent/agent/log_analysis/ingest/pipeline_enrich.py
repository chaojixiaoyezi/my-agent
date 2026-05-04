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
from .pipeline import IngestResult, file_digest
from .pipeline_helpers import (
    WriteManifestParams,
    _EnrichCounts,
    _storage_summary,
    make_batch_id,
    normalize_file_format,
)
from .pipeline_helpers import (
    write_manifest as _write_manifest,
)


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


def _finalize_ingest_result(params: _FinalizeIngestParams) -> IngestResult:
    """Write manifest, finish dedup batch, commit checkpoint, return IngestResult."""
    manifest_params = WriteManifestParams(
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
    manifest_path = _write_manifest(manifest_params)
    params.pipeline.dedup.finish_batch(
        batch_id=params.batch_id,
        status="stored",
        event_count=len(params.event_ids),
        duplicate_count=params.counts.duplicate_count,
        dead_letter_count=params.dead_letters.count,
        manifest_path=str(manifest_path),
    )
    checkpoint = params.pipeline.checkpoints.commit(
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


def enrich_ingest_file(
    pipeline,  # IngestPipeline — lazy to avoid circular import
    path: str | Path,
    *,
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
    source_path = Path(path)
    if not source_path.exists():
        raise FileNotFoundError(source_path)

    fmt = normalize_file_format(file_format or source_path.suffix.lstrip("."))
    batch_source_id = source_id or source_path.stem
    size_bytes, content_hash = file_digest(source_path)
    batch_id = make_batch_id(
        source_id=batch_source_id,
        source_path=str(source_path.resolve()),
        content_hash=content_hash,
    )
    parser = pipeline.registry.choose(parser_id=parser_id, file_format=fmt)
    dead_letters = DeadLetterWriter(pipeline.root, source_id=batch_source_id, batch_id=batch_id)
    started_at = utc_now()
    cursor_before = pipeline.checkpoints.load(batch_source_id).get("cursor", {})

    pipeline.dedup.begin_batch(
        batch_id=batch_id,
        source_id=batch_source_id,
        content_hash=content_hash,
        source_path=str(source_path),
    )

    counts, event_ids, storage_infos = _process_records(
        pipeline,
        source_path=source_path,
        file_format=fmt,
        parser=parser,
        batch_id=batch_id,
        source_id=batch_source_id,
        source_product=source_product,
        dead_letters=dead_letters,
    )

    storage_summary = _storage_summary(
        storage_infos, fallback_path=pipeline.fallback_sink.events_path
    )
    return _finalize_ingest_result(
        _FinalizeIngestParams(
            pipeline=pipeline,
            batch_id=batch_id,
            batch_source_id=batch_source_id,
            source_path=source_path,
            fmt=fmt,
            parser=parser,
            started_at=started_at,
            content_hash=content_hash,
            size_bytes=size_bytes,
            counts=counts,
            event_ids=event_ids,
            dead_letters=dead_letters,
            storage_summary=storage_summary,
            cursor_before=cursor_before,
        )
    )


def _process_records(
    pipeline,
    source_path: Path,
    *,
    file_format: str,
    parser: LogParser,
    batch_id: str,
    source_id: str,
    source_product: str | None,
    dead_letters: DeadLetterWriter,
) -> tuple[_EnrichCounts, list[str], list[dict[str, Any]]]:
    """Parse, dedup, and buffer events from a source file in batches."""
    parsed_count = 0
    duplicate_count = 0
    skipped_count = 0
    last_event_time: str | None = None
    first_event_time: str | None = None
    stored_event_ids: list[str] = []
    event_buffer: list[dict[str, Any]] = []
    storage_infos: list[dict[str, Any]] = []
    seen_in_batch: set[str] = set()

    for item in pipeline._iter_parsed_records(
        source_path,
        file_format=file_format,
        parser=parser,
        batch_id=batch_id,
        source_id=source_id,
        source_product=source_product,
        dead_letters=dead_letters,
    ):
        if item is None:
            skipped_count += 1
            continue
        parsed_count += 1
        event = item.event
        event_time = event.get("event_time")
        if isinstance(event_time, str):
            if first_event_time is None or event_time < first_event_time:
                first_event_time = event_time
            if last_event_time is None or event_time > last_event_time:
                last_event_time = event_time

        dedup_key = str(event["dedup_key"])
        if dedup_key in seen_in_batch or pipeline.dedup.is_duplicate(dedup_key):
            duplicate_count += 1
            pipeline.dedup.note_duplicate(dedup_key)
            continue
        seen_in_batch.add(dedup_key)
        event_buffer.append(event)
        if len(event_buffer) >= pipeline.write_batch_size:
            storage_info, event_ids = flush_events(pipeline, event_buffer, batch_id=batch_id)
            storage_infos.append(storage_info)
            stored_event_ids.extend(event_ids)
            event_buffer = []

    if event_buffer:
        storage_info, event_ids = flush_events(pipeline, event_buffer, batch_id=batch_id)
        storage_infos.append(storage_info)
        stored_event_ids.extend(event_ids)

    counts = _EnrichCounts(
        parsed_count=parsed_count,
        duplicate_count=duplicate_count,
        skipped_count=skipped_count,
        first_event_time=first_event_time,
        last_event_time=last_event_time,
    )
    return counts, stored_event_ids, storage_infos


def write_events(pipeline, events: list[dict[str, Any]]) -> dict[str, Any]:
    """Write a list of events to the configured store or fallback JSONL sink."""
    if not events:
        return {"count": 0, "path": str(pipeline.fallback_sink.events_path)}

    if pipeline.store is None:
        return pipeline.fallback_sink.write_events(events)

    for method_name in ("write_events", "append_events", "upsert_events"):
        method = getattr(pipeline.store, method_name, None)
        if callable(method):
            result = method(events)
            return _storage_result(result, count=len(events), store=pipeline.store)

    for method_name in ("write_event", "append_event", "upsert_event"):
        method = getattr(pipeline.store, method_name, None)
        if callable(method):
            for event in events:
                method(event)
            return {"count": len(events), "path": None}

    return pipeline.fallback_sink.write_events(events)


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


def write_manifest(
    *,
    params: WriteManifestParams,
) -> Path:
    """Write a manifest JSON file summarizing the ingest batch results."""
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


def _storage_result(result: Any, *, count: int, store: Any | None = None) -> dict[str, Any]:
    """Normalize a store write return value into a standard dict with count and path."""
    store_path = getattr(store, "events_path", None)
    if isinstance(result, dict):
        payload = dict(result)
        payload.setdefault("count", count)
        if payload.get("path") is None and store_path is not None:
            payload["path"] = str(store_path)
        return payload
    if isinstance(result, (str, Path)):
        return {"count": count, "path": str(result)}
    if isinstance(result, int):
        return {"count": result, "path": str(store_path) if store_path is not None else None}
    return {"count": count, "path": str(store_path) if store_path is not None else None}
