
"""Pipeline stage components extracted from IngestPipeline for size governance."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..parsers.base import LogParser, ParseContext, ParserError
from .dead_letter import DeadLetterRecord

# Re-use WriteManifestParams from pipeline_helpers for ManifestWriter
from .pipeline_helpers import WriteManifestParams  # noqa: F401


@dataclass(frozen=True)
class RecordIteratorOptions:
    file_format: str
    parser: LogParser
    batch_id: str
    source_id: str
    dead_letters: Any
    source_product: str | None = None


class RecordIterator:
    """Yields parsed records (or None for skipped lines) from JSONL / CSV sources."""

    def __init__(
        self,
        source_path: Path,
        *,
        options: RecordIteratorOptions | None = None,
        file_format: str = "",
        parser: LogParser | None = None,
        batch_id: str = "",
        source_id: str = "",
        source_product: str | None = None,
        dead_letters: Any = None,
    ):
        if options is None:
            options = RecordIteratorOptions(
                file_format=str(file_format),
                parser=parser,
                batch_id=str(batch_id),
                source_id=str(source_id),
                source_product=source_product,
                dead_letters=dead_letters,
            )
        self.source_path = source_path
        self.file_format = options.file_format
        self.parser = options.parser
        self.batch_id = options.batch_id
        self.source_id = options.source_id
        self.source_product = options.source_product
        self.dead_letters = options.dead_letters

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
                yield from self._parse_jsonl_line(line_no, raw_line)

    def _parse_jsonl_line(self, line_no: int, raw_line: str):
        text = raw_line.rstrip("\n")
        raw_ref = f"{self.batch_id}:line-{line_no}"
        if not text.strip():
            return (None,)
        try:
            parsed = self.parser.parse_json_line(
                text,
                request=ParseContext(raw_ref, self.source_id, self.source_product, line_no),
            )
        except ParserError as exc:
            self.dead_letters.write(
                record=DeadLetterRecord(
                    reason=str(exc),
                    raw_ref=raw_ref,
                    line_no=line_no,
                    raw_line=text,
                    parser_id=self.parser.parser_id,
                )
            )
            return ()
        return (parsed,)

    def _iter_csv(self):
        """Parse a CSV file row by row via the registered LogParser."""
        with self.source_path.open(
            "r", encoding="utf-8-sig", newline="", errors="replace"
        ) as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                self.dead_letters.write(
                    record=DeadLetterRecord(
                        reason="CSV file has no header",
                        raw_ref=f"{self.batch_id}:line-1",
                        line_no=1,
                        raw_line="",
                        parser_id=self.parser.parser_id,
                    )
                )
                return
            for row in reader:
                yield from self._parse_csv_row(row, reader.line_num)

    def _parse_csv_row(self, row: Mapping[str, Any], line_no: int):
        raw_ref = f"{self.batch_id}:line-{line_no}"
        try:
            parsed = self.parser.parse_csv_row(
                row,
                request=ParseContext(raw_ref, self.source_id, self.source_product, line_no),
            )
        except ParserError as exc:
            self.dead_letters.write(
                record=DeadLetterRecord(
                    reason=str(exc),
                    raw_ref=raw_ref,
                    line_no=line_no,
                    raw_line=json.dumps(_jsonable_mapping(row), ensure_ascii=False, sort_keys=True),
                    raw_fields=_jsonable_mapping(row),
                    parser_id=self.parser.parser_id,
                )
            )
            return ()
        except csv.Error as exc:
            self.dead_letters.write(
                record=DeadLetterRecord(
                    reason=f"invalid CSV: {exc}",
                    raw_ref=raw_ref,
                    line_no=line_no,
                    parser_id=self.parser.parser_id,
                )
            )
            return ()
        return (parsed,)


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
        from .checkpoint import safe_source_id, write_json_atomic

        safe_source = safe_source_id(params.source_id)
        manifest_path = self.root / "manifests" / safe_source / f"{params.batch_id}.json"
        write_json_atomic(manifest_path, _manifest_payload(params))
        return manifest_path


def _manifest_payload(params: WriteManifestParams) -> dict[str, Any]:
    from ..parsers.common import utc_now

    return {
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
        "cursor_after": _manifest_cursor_after(params),
        "dedup_policy": "source_event_fingerprint",
        "checkpoint_policy": "after_durable_write",
        "status": "stored",
        "counts": _manifest_counts(params),
        "storage": dict(params.storage_info),
        "dead_letter_refs": params.dead_letter_refs,
    }


def _manifest_cursor_after(params: WriteManifestParams) -> dict[str, Any]:
    return {
        "path": str(params.source_path),
        "format": params.file_format,
        "size_bytes": params.size_bytes,
        "content_hash": params.content_hash,
        "batch_id": params.batch_id,
    }


def _manifest_counts(params: WriteManifestParams) -> dict[str, int]:
    return {
        "parsed": params.parsed_count,
        "stored": params.stored_count,
        "duplicates": params.duplicate_count,
        "skipped": params.skipped_count,
        "dead_letter": params.dead_letter_count,
    }


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
