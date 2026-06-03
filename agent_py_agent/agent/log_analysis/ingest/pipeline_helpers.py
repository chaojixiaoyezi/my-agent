
"""Shared helpers extracted from pipeline_enrich.py to keep other modules lean."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..parsers.base import LogParser, ParserError


def normalize_file_format(value: str) -> str:
    """Normalize a file format string to a canonical ingest format name."""
    fmt = value.lower().lstrip(".") or "jsonl"
    if fmt == "json":
        return "jsonl"
    if fmt in {"jsonl", "csv", "log"}:
        return fmt
    raise ParserError(f"unsupported file format: {value}")


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


@dataclass(frozen=True)
class _EnrichCounts:
    """Internal counter bundle returned by _process_records."""

    parsed_count: int
    duplicate_count: int
    skipped_count: int
    first_event_time: str | None
    last_event_time: str | None
