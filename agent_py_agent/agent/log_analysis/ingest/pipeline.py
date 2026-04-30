from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from ...file_io import append_jsonl
from ..parsers.base import LogParser, ParserError
from ..parsers.common import DEFAULT_PAYLOAD_MAX_CHARS, sha256_json, utc_now
from ..parsers.registry import ParserRegistry, default_registry
from .checkpoint import CheckpointStore, safe_source_id, write_json_atomic
from .dead_letter import DeadLetterWriter
from .dedup import DedupStore


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
        registry: ParserRegistry | None = None,
        store: Any | None = None,
        payload_max_chars: int = DEFAULT_PAYLOAD_MAX_CHARS,
        write_batch_size: int = 1000,
    ):
        self.root = Path(root)
        self.payload_max_chars = payload_max_chars
        self.write_batch_size = max(1, write_batch_size)
        self.registry = registry or default_registry(payload_max_chars=payload_max_chars)
        self.fallback_sink = JsonlEventSink(self.root)
        self.store = store or self._default_store()
        self.checkpoints = CheckpointStore(self.root)
        self.dedup = DedupStore(self.root / "dedup.sqlite3")

    def _default_store(self) -> Any:
        from ..storage.local_store import LocalLogStore

        return LocalLogStore(self.root)

    def ingest_file(
        self,
        path: str | Path,
        *,
        source_id: str | None = None,
        source_product: str | None = None,
        parser_id: str = "security_alert_v1",
        file_format: str | None = None,
    ) -> IngestResult:
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
        parser = self.registry.choose(parser_id=parser_id, file_format=fmt)
        dead_letters = DeadLetterWriter(self.root, source_id=batch_source_id, batch_id=batch_id)
        started_at = utc_now()
        cursor_before = self.checkpoints.load(batch_source_id).get("cursor", {})

        self.dedup.begin_batch(
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

        for item in self._iter_parsed_records(
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
            if dedup_key in seen_in_batch or self.dedup.is_duplicate(dedup_key):
                duplicate_count += 1
                self.dedup.note_duplicate(dedup_key)
                continue
            seen_in_batch.add(dedup_key)
            event_buffer.append(event)
            if len(event_buffer) >= self.write_batch_size:
                storage_info, event_ids = self._flush_events(event_buffer, batch_id=batch_id)
                storage_infos.append(storage_info)
                stored_event_ids.extend(event_ids)
                event_buffer = []

        storage_info, event_ids = self._flush_events(event_buffer, batch_id=batch_id)
        storage_infos.append(storage_info)
        stored_event_ids.extend(event_ids)
        storage_summary = _storage_summary(storage_infos, fallback_path=self.fallback_sink.events_path)

        manifest_path = self._write_manifest(
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
        self.dedup.finish_batch(
            batch_id=batch_id,
            status="stored",
            event_count=len(stored_event_ids),
            duplicate_count=duplicate_count,
            dead_letter_count=dead_letters.count,
            manifest_path=str(manifest_path),
        )
        checkpoint = self.checkpoints.commit(
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
            checkpoint_path=str(self.checkpoints.path_for(checkpoint.source_id)),
            events_path=storage_summary.get("path"),
            dead_letter_refs=dead_letters.refs(),
            stored_event_ids=stored_event_ids,
        )

    def _iter_parsed_records(
        self,
        source_path: Path,
        *,
        file_format: str,
        parser: LogParser,
        batch_id: str,
        source_id: str,
        source_product: str | None,
        dead_letters: DeadLetterWriter,
    ):
        if file_format in {"jsonl", "log"}:
            yield from self._iter_jsonl_records(
                source_path,
                parser=parser,
                batch_id=batch_id,
                source_id=source_id,
                source_product=source_product,
                dead_letters=dead_letters,
            )
            return
        if file_format == "csv":
            yield from self._iter_csv_records(
                source_path,
                parser=parser,
                batch_id=batch_id,
                source_id=source_id,
                source_product=source_product,
                dead_letters=dead_letters,
            )
            return
        raise ParserError(f"unsupported ingest file format: {file_format}")

    def _iter_jsonl_records(
        self,
        source_path: Path,
        *,
        parser: LogParser,
        batch_id: str,
        source_id: str,
        source_product: str | None,
        dead_letters: DeadLetterWriter,
    ):
        with source_path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for line_no, raw_line in enumerate(handle, start=1):
                text = raw_line.rstrip("\n")
                raw_ref = f"{batch_id}:line-{line_no}"
                if not text.strip():
                    yield None
                    continue
                try:
                    yield parser.parse_json_line(
                        text,
                        raw_ref=raw_ref,
                        source_id=source_id,
                        source_product=source_product,
                        line_no=line_no,
                    )
                except ParserError as exc:
                    dead_letters.write(
                        reason=str(exc),
                        raw_ref=raw_ref,
                        line_no=line_no,
                        raw_line=text,
                        parser_id=parser.parser_id,
                    )

    def _iter_csv_records(
        self,
        source_path: Path,
        *,
        parser: LogParser,
        batch_id: str,
        source_id: str,
        source_product: str | None,
        dead_letters: DeadLetterWriter,
    ):
        with source_path.open("r", encoding="utf-8-sig", newline="", errors="replace") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                dead_letters.write(
                    reason="CSV file has no header",
                    raw_ref=f"{batch_id}:line-1",
                    line_no=1,
                    raw_line="",
                    parser_id=parser.parser_id,
                )
                return
            for row in reader:
                line_no = reader.line_num
                raw_ref = f"{batch_id}:line-{line_no}"
                try:
                    yield parser.parse_csv_row(
                        row,
                        raw_ref=raw_ref,
                        source_id=source_id,
                        source_product=source_product,
                        line_no=line_no,
                    )
                except ParserError as exc:
                    dead_letters.write(
                        reason=str(exc),
                        raw_ref=raw_ref,
                        line_no=line_no,
                        raw_line=json.dumps(_jsonable_mapping(row), ensure_ascii=False, sort_keys=True),
                        raw_fields=_jsonable_mapping(row),
                        parser_id=parser.parser_id,
                    )
                except csv.Error as exc:
                    dead_letters.write(
                        reason=f"invalid CSV: {exc}",
                        raw_ref=raw_ref,
                        line_no=line_no,
                        parser_id=parser.parser_id,
                    )

    def _write_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
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

    def _flush_events(self, events: list[dict[str, Any]], *, batch_id: str) -> tuple[dict[str, Any], list[str]]:
        if not events:
            return {"count": 0, "path": str(self.fallback_sink.events_path)}, []
        storage_info = self._write_events(events)
        stored_event_ids: list[str] = []
        for event in events:
            if self.dedup.mark_event(
                dedup_key=str(event["dedup_key"]),
                event_id=str(event["event_id"]),
                source_id=str(event["source_id"]),
                batch_id=batch_id,
            ):
                stored_event_ids.append(str(event["event_id"]))
        return storage_info, stored_event_ids

    def _write_manifest(
        self,
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
        safe_source = safe_source_id(source_id)
        manifest_path = self.root / "manifests" / safe_source / f"{batch_id}.json"
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


def ingest_file(
    path: str | Path,
    *,
    root: str | Path | None = None,
    source_id: str | None = None,
    source_product: str | None = None,
    parser_id: str = "security_alert_v1",
    file_format: str | None = None,
    payload_max_chars: int = DEFAULT_PAYLOAD_MAX_CHARS,
    store: Any | None = None,
) -> IngestResult:
    pipeline = IngestPipeline(
        root or default_log_analysis_root(),
        store=store,
        payload_max_chars=payload_max_chars,
    )
    return pipeline.ingest_file(
        path,
        source_id=source_id,
        source_product=source_product,
        parser_id=parser_id,
        file_format=file_format,
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
            payload["path"] = str(store_path)
        return payload
    if isinstance(result, (str, Path)):
        return {"count": count, "path": str(result)}
    if isinstance(result, int):
        return {"count": result, "path": str(store_path) if store_path is not None else None}
    return {"count": count, "path": str(store_path) if store_path is not None else None}


def _storage_summary(infos: list[dict[str, Any]], *, fallback_path: Path) -> dict[str, Any]:
    count = sum(int(info.get("count") or 0) for info in infos)
    paths = sorted({str(info.get("path")) for info in infos if info.get("path")})
    if not paths:
        paths = [str(fallback_path)]
    summary: dict[str, Any] = {"count": count, "path": paths[0]}
    if len(paths) > 1:
        summary["paths"] = paths
    return summary


def _jsonable_mapping(mapping: Mapping[Any, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in mapping.items()}
