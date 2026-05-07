from __future__ import annotations

"""LLM: ingest finalization helpers for manifests, dedup completion, and checkpoints."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..parsers.base import LogParser
from .dead_letter import DeadLetterWriter
from .pipeline import IngestResult
from .pipeline_helpers import WriteManifestParams, _EnrichCounts
from .pipeline_helpers import write_manifest as _write_manifest


@dataclass
class FinalizeIngestParams:
    """Bundle of all finalize_ingest_result parameters."""

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


@dataclass(frozen=True)
class PreparedFinalize:
    pipeline: Any
    prepared: Any
    counts: _EnrichCounts
    event_ids: list[str]
    storage_summary: dict[str, Any]


def finalize_prepared_ingest(request: PreparedFinalize) -> IngestResult:
    prepared = request.prepared
    return finalize_ingest_result(
        FinalizeIngestParams(
            pipeline=request.pipeline,
            batch_id=prepared.batch_id,
            batch_source_id=prepared.batch_source_id,
            source_path=prepared.source_path,
            fmt=prepared.fmt,
            parser=prepared.parser,
            started_at=prepared.started_at,
            content_hash=prepared.content_hash,
            size_bytes=prepared.size_bytes,
            counts=request.counts,
            event_ids=request.event_ids,
            dead_letters=prepared.dead_letters,
            storage_summary=request.storage_summary,
            cursor_before=prepared.cursor_before,
        )
    )


def finalize_ingest_result(params: FinalizeIngestParams) -> IngestResult:
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


def _manifest_params(params: FinalizeIngestParams) -> WriteManifestParams:
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


def _finish_dedup_batch(params: FinalizeIngestParams, manifest_path: Path) -> None:
    params.pipeline.dedup.finish_batch(
        batch_id=params.batch_id,
        status="stored",
        event_count=len(params.event_ids),
        duplicate_count=params.counts.duplicate_count,
        dead_letter_count=params.dead_letters.count,
        manifest_path=str(manifest_path),
    )


def _commit_checkpoint(params: FinalizeIngestParams):
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
