"""Pipeline stage components extracted from IngestPipeline for size governance."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..parsers.base import LogParser, ParserError

# Re-use WriteManifestParams from pipeline_helpers for ManifestWriter
from .pipeline_helpers import WriteManifestParams  # noqa: F401


class RecordIterator:
    """Yields parsed records (or None for skipped lines) from JSONL / CSV sources."""

    def __init__(
        self,
        source_path: Path,
        **kwargs: Any,
    ):
        self.source_path = source_path
        self.file_format = kwargs["file_format"]
        self.parser = kwargs["parser"]
        self.batch_id = kwargs["batch_id"]
        self.source_id = kwargs["source_id"]
        self.source_product = kwargs.get("source_product")
        self.dead_letters = kwargs["dead_letters"]

    def iter_records(self):
        """Dispatch to format-specific iterator."""
        if self.file_format in {"jsonl", "log"}:
            yield from self._iter_jsonl()
            return
        if self.file_format == "csv":
            yield from self._iter_csv()
            return
        raise ParserError(f"unsupported ingest file format: {self.file_format}")

    def _iter_jsonl(self):
        """Parse a JSONL / .log file line by line."""
        with self.source_path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for line_no, raw_line in enumerate(handle, start=1):
                text = raw_line.rstrip("\n")
                raw_ref = f"{self.batch_id}:line-{line_no}"
                if not text.strip():
                    yield None
                    continue
                try:
                    yield self.parser.parse_json_line(
                        text,
                        raw_ref=raw_ref,
                        source_id=self.source_id,
                        source_product=self.source_product,
                        line_no=line_no,
                    )
                except ParserError as exc:
                    self.dead_letters.write(
                        reason=str(exc),
                        raw_ref=raw_ref,
                        line_no=line_no,
                        raw_line=text,
                        parser_id=self.parser.parser_id,
                    )

    def _iter_csv(self):
        """Parse a CSV file row by row via the registered LogParser."""
        with self.source_path.open(
            "r", encoding="utf-8-sig", newline="", errors="replace"
        ) as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                self.dead_letters.write(
                    reason="CSV file has no header",
                    raw_ref=f"{self.batch_id}:line-1",
                    line_no=1,
                    raw_line="",
                    parser_id=self.parser.parser_id,
                )
                return
            for row in reader:
                line_no = reader.line_num
                raw_ref = f"{self.batch_id}:line-{line_no}"
                try:
                    yield self.parser.parse_csv_row(
                        row,
                        raw_ref=raw_ref,
                        source_id=self.source_id,
                        source_product=self.source_product,
                        line_no=line_no,
                    )
                except ParserError as exc:
                    self.dead_letters.write(
                        reason=str(exc),
                        raw_ref=raw_ref,
                        line_no=line_no,
                        raw_line=json.dumps(
                            _jsonable_mapping(row), ensure_ascii=False, sort_keys=True
                        ),
                        raw_fields=_jsonable_mapping(row),
                        parser_id=self.parser.parser_id,
                    )
                except csv.Error as exc:
                    self.dead_letters.write(
                        reason=f"invalid CSV: {exc}",
                        raw_ref=raw_ref,
                        line_no=line_no,
                        parser_id=self.parser.parser_id,
                    )


class EventWriter:
    """Write events to a store (or JsonlEventSink fallback) and return storage metadata."""

    def __init__(self, store: Any, fallback_sink: Any):
        self.store = store
        self.fallback_sink = fallback_sink

    def write(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Dispatch to the first available write method on the store."""
        if not events:
            return {"count": 0, "path": str(self.fallback_sink.events_path)}

        if self.store is None:
            return self.fallback_sink.write_events(events)

        for method_name in ("write_events", "append_events", "upsert_events"):
            method = getattr(self.store, method_name, None)
            if callable(method):
                result = method(events)
                return _storage_result(result, count=len(events), store=self.store)

        for method_name in ("write_event", "append_event", "upsert_event"):
            method = getattr(self.store, method_name, None)
            if callable(method):
                for event in events:
                    method(event)
                return {"count": len(events), "path": None}

        return self.fallback_sink.write_events(events)


class ManifestWriter:
    """Build and atomically write a batch manifest JSON file."""

    def __init__(self, root: Path):
        self.root = root

    def write(
        self,
        *,
        params: WriteManifestParams,
    ) -> Path:
        """Write the manifest JSON file and return its path."""
        from ..parsers.common import utc_now
        from .checkpoint import safe_source_id, write_json_atomic

        safe_source = safe_source_id(params.source_id)
        manifest_path = self.root / "manifests" / safe_source / f"{params.batch_id}.json"
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


def _jsonable_mapping(mapping: Mapping[Any, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in mapping.items()}
