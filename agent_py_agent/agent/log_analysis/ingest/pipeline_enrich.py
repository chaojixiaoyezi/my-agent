from __future__ import annotations

"""LLM: later-stage ingest pipeline functions — normalization, enrichment, dedup, and output writing.

给人看的解释：
这个文件放 pipeline 的后半段：格式规范化、去重、事件写入存储、manifest 生成。
从 pipeline.py 拆出来，让解析/迭代和写入/去重各归一处。
"""

from pathlib import Path
from typing import Any, Mapping

from ..parsers.base import LogParser, ParserError
from ..parsers.common import sha256_json, utc_now
from .checkpoint import safe_source_id, write_json_atomic
from .dead_letter import DeadLetterWriter
from .pipeline import IngestResult, file_digest


def normalize_file_format(value: str) -> str:
    """LLM: Normalize a file format string to a canonical ingest format name.

    新手说明:
    把用户传入的格式字符串统一成内部用的格式名。
    比如 "JSON" 变 "jsonl"，".csv" 变 "csv"。
    """
    fmt = value.lower().lstrip(".") or "jsonl"
    if fmt == "json":
        return "jsonl"
    if fmt in {"jsonl", "csv", "log"}:
        return fmt
    raise ParserError(f"unsupported file format: {value}")


def make_batch_id(*, source_id: str, source_path: str, content_hash: str) -> str:
    """LLM: Generate a deterministic batch ID from source identity and content hash.

    新手说明:
    根据来源 ID、文件路径、内容哈希生成一个唯一的 batch ID。
    同一个文件多次导入会产生相同的 batch ID。
    """
    digest = sha256_json(
        {
            "source_id": source_id,
            "source_path": source_path,
            "content_hash": content_hash,
        }
    )
    return f"batch-{digest[:24]}"


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
        file_format=fmt,
        parser=parser,
        batch_id=batch_id,
        source_id=batch_source_id,
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
            first_event_time = min(first_event_time, event_time) if first_event_time else event_time
            last_event_time = max(last_event_time, event_time) if last_event_time else event_time

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

    storage_info, event_ids = flush_events(pipeline, event_buffer, batch_id=batch_id)
    storage_infos.append(storage_info)
    stored_event_ids.extend(event_ids)
    storage_summary = _storage_summary(storage_infos, fallback_path=pipeline.fallback_sink.events_path)

    manifest_path = write_manifest(
        pipeline,
        batch_id=batch_id,
        source_id=batch_source_id,
        source_path=source_path,
        file_format=fmt,
        parser=parser,
        started_at=started_at,
        content_hash=content_hash,
        size_bytes=size_bytes,
        first_event_time=first_event_time,
        last_event_time=last_event_time,
        parsed_count=parsed_count,
        stored_count=len(stored_event_ids),
        duplicate_count=duplicate_count,
        skipped_count=skipped_count,
        dead_letter_count=dead_letters.count,
        dead_letter_refs=dead_letters.refs(),
        cursor_before=cursor_before,
        storage_info=storage_summary,
    )
    pipeline.dedup.finish_batch(
        batch_id=batch_id,
        status="stored",
        event_count=len(stored_event_ids),
        duplicate_count=duplicate_count,
        dead_letter_count=dead_letters.count,
        manifest_path=str(manifest_path),
    )
    checkpoint = pipeline.checkpoints.commit(
        source_id=batch_source_id,
        cursor_kind="file_content_hash",
        cursor={
            "path": str(source_path),
            "format": fmt,
            "size_bytes": size_bytes,
            "content_hash": content_hash,
            "batch_id": batch_id,
        },
        last_committed_batch_id=batch_id,
        last_event_time=last_event_time,
    )

    return IngestResult(
        batch_id=batch_id,
        source_id=batch_source_id,
        source_path=str(source_path),
        file_format=fmt,
        status="stored",
        parsed_count=parsed_count,
        stored_count=len(stored_event_ids),
        duplicate_count=duplicate_count,
        dead_letter_count=dead_letters.count,
        skipped_count=skipped_count,
        content_hash=content_hash,
        manifest_path=str(manifest_path),
        checkpoint_path=str(pipeline.checkpoints.path_for(checkpoint.source_id)),
        events_path=storage_summary.get("path"),
        dead_letter_refs=dead_letters.refs(),
        stored_event_ids=stored_event_ids,
    )


def write_events(pipeline, events: list[dict[str, Any]]) -> dict[str, Any]:
    """LLM: Write a list of events to the configured store or fallback JSONL sink.

    新手说明:
    把事件列表写入存储。如果有正式 store 就用 store，
    否则 fallback 到 JsonlEventSink 写 JSONL 文件。
    """
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
    """LLM: Flush buffered events to storage and register them in the dedup store.

    新手说明:
    把缓冲区里的事件写入存储，同时在去重表里标记这些事件已存储。
    返回 (存储信息, 已存储的事件 ID 列表)。
    """
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
    pipeline,
    *,
    batch_id: str,
    source_id: str,
    source_path: Path,
    file_format: str,
    parser: LogParser,
    started_at: str,
    content_hash: str,
    size_bytes: int,
    first_event_time: str | None,
    last_event_time: str | None,
    parsed_count: int,
    stored_count: int,
    duplicate_count: int,
    skipped_count: int,
    dead_letter_count: int,
    dead_letter_refs: list[dict[str, Any]],
    cursor_before: Mapping[str, Any],
    storage_info: Mapping[str, Any],
) -> Path:
    """LLM: Write a manifest JSON file summarizing the ingest batch results.

    新手说明:
    写一个 manifest 文件记录这批导入的详细信息：
    来源、解析器、时间范围、计数、游标等。
    """
    safe_source = safe_source_id(source_id)
    manifest_path = pipeline.root / "manifests" / safe_source / f"{batch_id}.json"
    cursor_after = {
        "path": str(source_path),
        "format": file_format,
        "size_bytes": size_bytes,
        "content_hash": content_hash,
        "batch_id": batch_id,
    }
    manifest = {
        "batch_id": batch_id,
        "source_id": source_id,
        "source_kind": "file",
        "source_path": str(source_path),
        "format": file_format,
        "parser_id": parser.parser_id,
        "parser_schema": parser.schema,
        "received_at": started_at,
        "completed_at": utc_now(),
        "time_range": [first_event_time, last_event_time],
        "raw_refs": [str(source_path)],
        "size_bytes": size_bytes,
        "content_hash": content_hash,
        "cursor_before": dict(cursor_before),
        "cursor_after": cursor_after,
        "dedup_policy": "source_event_fingerprint",
        "checkpoint_policy": "after_durable_write",
        "status": "stored",
        "counts": {
            "parsed": parsed_count,
            "stored": stored_count,
            "duplicates": duplicate_count,
            "skipped": skipped_count,
            "dead_letter": dead_letter_count,
        },
        "storage": dict(storage_info),
        "dead_letter_refs": dead_letter_refs,
    }
    write_json_atomic(manifest_path, manifest)
    return manifest_path


def _storage_result(result: Any, *, count: int, store: Any | None = None) -> dict[str, Any]:
    """LLM: Normalize a store write return value into a standard dict with count and path.

    新手说明:
    不同的 store 返回值格式不一样（dict/str/int），
    这个函数统一转成 {"count": ..., "path": ...} 的格式。
    """
    store_path = getattr(store, "events_path", None)
    if isinstance(result, Mapping):
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


def _storage_summary(infos: list[dict[str, Any]], *, fallback_path: Path) -> dict[str, Any]:
    """LLM: Aggregate multiple storage result dicts into a single summary.

    新手说明:
    多次写入可能产生多个存储结果，这个函数把它们合并成一个汇总：
    总计数 + 所有路径。
    """
    count = sum(int(info.get("count") or 0) for info in infos)
    paths = sorted({str(info.get("path")) for info in infos if info.get("path")})
    if not paths:
        paths = [str(fallback_path)]
    summary: dict[str, Any] = {"count": count, "path": paths[0]}
    if len(paths) > 1:
        summary["paths"] = paths
    return summary
