from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...file_io import append_jsonl
from ..parsers.base import LogParser, ParserError
from ..parsers.common import DEFAULT_PAYLOAD_MAX_CHARS, sha256_json, utc_now
from ..parsers.registry import ParserRegistry, default_registry
from .checkpoint import CheckpointStore, safe_source_id, write_json_atomic
from .dead_letter import DeadLetterWriter
from .dedup import DedupStore

if TYPE_CHECKING:
    from .pipeline_stages import EventWriter, ManifestWriter, RecordIteratorFactory


@dataclass(frozen=True)
class IngestResult:
    batch_id: str
    source_id: str
    source_path: str
    file_format: str
    status: str
    parsed_count: int
    stored_count: int
    duplicate_count: int
    dead_letter_count: int
    skipped_count: int
    content_hash: str
    manifest_path: str
    checkpoint_path: str
    events_path: str | None
    dead_letter_refs: list[dict[str, Any]]
    stored_event_ids: list[str]


@dataclass(frozen=True)
class _RecordIteratorRequest:
    source_path: Path
    parser: LogParser
    batch_id: str
    source_id: str
    source_product: str | None
    dead_letters: DeadLetterWriter


@dataclass(frozen=True)
class IngestPipelineOptions:
    registry: ParserRegistry | None = None
    store: Any | None = None
    payload_max_chars: int = DEFAULT_PAYLOAD_MAX_CHARS
    write_batch_size: int = 1000

    @classmethod
    def from_kwargs(cls, **kwargs: Any) -> IngestPipelineOptions:
        return cls(
            registry=kwargs.get("registry"),
            store=kwargs.get("store"),
            payload_max_chars=int(kwargs.get("payload_max_chars", DEFAULT_PAYLOAD_MAX_CHARS)),
            write_batch_size=int(kwargs.get("write_batch_size", 1000)),
        )


@dataclass(frozen=True)
class IngestFileOptions:
    source_id: str | None = None
    source_product: str | None = None
    parser_id: str = "security_alert_v1"
    file_format: str | None = None

    @classmethod
    def from_kwargs(cls, **kwargs: Any) -> IngestFileOptions:
        return cls(
            source_id=kwargs.get("source_id"),
            source_product=kwargs.get("source_product"),
            parser_id=str(kwargs.get("parser_id", "security_alert_v1")),
            file_format=kwargs.get("file_format"),
        )


class JsonlEventSink:
    """Fallback event sink used until Worker C's LocalLogStore is available."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.events_path = self.root / "events.jsonl"

    def write_events(self, events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        count = 0
        for event in events:
            append_jsonl(self.events_path, dict(event), sort_keys=True)
            count += 1
        return {"count": count, "path": str(self.events_path)}


class IngestPipeline:
    """Local file ingest pipeline for SecurityAlertV1 CSV/JSONL files."""

    def __init__(
        self,
        root: str | Path,
        *,
        options: IngestPipelineOptions | None = None,
        **kwargs: Any,
    ):
        ingest_options = options or IngestPipelineOptions.from_kwargs(**kwargs)
        self.root = Path(root)
        self.payload_max_chars = ingest_options.payload_max_chars
        self.write_batch_size = max(1, ingest_options.write_batch_size)
        self.registry = ingest_options.registry or default_registry(payload_max_chars=ingest_options.payload_max_chars)
        self.fallback_sink = JsonlEventSink(self.root)
        self.store = ingest_options.store or self._default_store()
        self.checkpoints = CheckpointStore(self.root)
        self.dedup = DedupStore(self.root / "dedup.sqlite3")

    def _default_store(self) -> Any:
        from ..storage.local_store import LocalLogStore

        return LocalLogStore(self.root)

    def ingest_file(
        self,
        path: str | Path,
        *,
        options: IngestFileOptions | None = None,
        **kwargs: Any,
    ) -> IngestResult:
        """Ingest a single file and return structured result."""
        from .pipeline_enrich import enrich_ingest_file

        ingest_options = options or IngestFileOptions.from_kwargs(**kwargs)
        return enrich_ingest_file(self, path, options=ingest_options)

    def _iter_parsed_records(
        self,
        source_path: Path,
        **kwargs: Any,
    ):
        file_format = kwargs["file_format"]
        parser = kwargs["parser"]
        batch_id = kwargs["batch_id"]
        source_id = kwargs["source_id"]
        source_product = kwargs.get("source_product")
        dead_letters = kwargs["dead_letters"]
        request = _RecordIteratorRequest(source_path, parser, batch_id, source_id, source_product, dead_letters)
        if file_format in {"jsonl", "log"}:
            yield from self._iter_jsonl_records(request)
            return
        if file_format == "csv":
            yield from self._iter_csv_records(request)
            return
        raise ParserError(f"unsupported ingest file format: {file_format}")

    def _iter_jsonl_records(self, request: _RecordIteratorRequest):
        from .pipeline_stages import RecordIterator

        yield from RecordIterator(
            request.source_path,
            file_format="jsonl",
            parser=request.parser,
            batch_id=request.batch_id,
            source_id=request.source_id,
            source_product=request.source_product,
            dead_letters=request.dead_letters,
        ).iter_records()

    def _iter_csv_records(self, request: _RecordIteratorRequest):
        from .pipeline_stages import RecordIterator

        yield from RecordIterator(
            request.source_path,
            file_format="csv",
            parser=request.parser,
            batch_id=request.batch_id,
            source_id=request.source_id,
            source_product=request.source_product,
            dead_letters=request.dead_letters,
        ).iter_records()

    def _write_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Write events to store or fallback sink (delegated to pipeline_enrich)."""
        from .pipeline_enrich import write_events as _write_events

        return _write_events(self, events)

    def _flush_events(
        self, events: list[dict[str, Any]], *, batch_id: str
    ) -> tuple[dict[str, Any], list[str]]:
        """Flush events to storage and dedup store (delegated to pipeline_enrich)."""
        from .pipeline_enrich import flush_events as _flush_events

        return _flush_events(self, events, batch_id=batch_id)


def ingest_file(
    path: str | Path,
    **kwargs: Any,
) -> IngestResult:
    pipeline = IngestPipeline(
        kwargs.get("root") or default_log_analysis_root(),
        options=IngestPipelineOptions(
            store=kwargs.get("store"),
            payload_max_chars=kwargs.get("payload_max_chars", DEFAULT_PAYLOAD_MAX_CHARS),
        ),
    )
    return pipeline.ingest_file(
        path,
        options=IngestFileOptions.from_kwargs(**kwargs),
    )


def default_log_analysis_root() -> Path:
    return Path.cwd() / "agent_py_agent" / "data" / "log_analysis"


def normalize_file_format(value: str) -> str:
    fmt = value.lower().lstrip(".") or "jsonl"
    if fmt == "json":
        return "jsonl"
    if fmt in {"jsonl", "csv", "log"}:
        return fmt
    raise ParserError(f"unsupported file format: {value}")


def file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, f"sha256:{digest.hexdigest()}"


def make_batch_id(*, source_id: str, source_path: str, content_hash: str) -> str:
    digest = sha256_json(
        {
            "source_id": source_id,
            "source_path": source_path,
            "content_hash": content_hash,
        }
    )
    return f"batch-{digest[:24]}"


def _storage_result(result: Any, *, count: int, store: Any | None = None) -> dict[str, Any]:
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
    count = sum(int(info.get("count") or 0) for info in infos)
    paths = sorted({str(info.get("path")).replace("\\", "/") for info in infos if info.get("path")})
    if not paths:
        paths = [str(fallback_path).replace("\\", "/")]
    summary: dict[str, Any] = {"count": count, "path": paths[0]}
    if len(paths) > 1:
        summary["paths"] = paths
    return summary


def _jsonable_mapping(mapping: Mapping[Any, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in mapping.items()}
