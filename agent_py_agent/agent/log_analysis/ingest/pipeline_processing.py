
from __future__ import annotations

"""Record processing loop for enrich_ingest_file."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..parsers.base import LogParser
from .dead_letter import DeadLetterWriter
from .pipeline import _EnrichCounts


@dataclass
class _ProcessRecordsParams:
    source_path: Path
    file_format: str
    parser: LogParser
    batch_id: str
    source_id: str
    source_product: str | None
    dead_letters: DeadLetterWriter

def process_records(pipeline, params: _ProcessRecordsParams) -> tuple[_EnrichCounts, list[str], list[dict[str, Any]]]:
    parsed_count = 0
    duplicate_count = 0
    skipped_count = 0
    time_window = _ProcessTimeWindow()
    flush_state = _FlushState()
    event_buffer: list[dict[str, Any]] = []
    seen_in_batch: set[str] = set()

    for item in pipeline._iter_parsed_records(
        params.source_path,
        file_format=params.file_format,
        parser=params.parser,
        batch_id=params.batch_id,
        source_id=params.source_id,
        source_product=params.source_product,
        dead_letters=params.dead_letters,
    ):
        if item is None:
            skipped_count += 1
            continue
        parsed_count += 1
        event = item.event
        time_window.note(event.get("event_time"))

        dedup_key = str(event["dedup_key"])
        if dedup_key in seen_in_batch or pipeline.dedup.is_duplicate(dedup_key):
            duplicate_count += 1
            pipeline.dedup.note_duplicate(dedup_key)
            continue
        seen_in_batch.add(dedup_key)
        event_buffer.append(event)
        if len(event_buffer) >= pipeline.write_batch_size:
            _flush_buffer(pipeline, params.batch_id, event_buffer, flush_state)
            event_buffer = []

    if event_buffer:
        _flush_buffer(pipeline, params.batch_id, event_buffer, flush_state)

    counts = _EnrichCounts(
        parsed_count=parsed_count,
        duplicate_count=duplicate_count,
        skipped_count=skipped_count,
        first_event_time=time_window.first_event_time,
        last_event_time=time_window.last_event_time,
    )
    return counts, flush_state.stored_event_ids, flush_state.storage_infos


@dataclass
class _ProcessTimeWindow:
    first_event_time: str | None = None
    last_event_time: str | None = None

    def note(self, event_time: Any) -> None:
        if not isinstance(event_time, str):
            return
        if self.first_event_time is None or event_time < self.first_event_time:
            self.first_event_time = event_time
        if self.last_event_time is None or event_time > self.last_event_time:
            self.last_event_time = event_time


@dataclass
class _FlushState:
    storage_infos: list[dict[str, Any]] = None
    stored_event_ids: list[str] = None

    def __post_init__(self) -> None:
        self.storage_infos = [] if self.storage_infos is None else self.storage_infos
        self.stored_event_ids = [] if self.stored_event_ids is None else self.stored_event_ids


def _flush_buffer(
    pipeline,
    batch_id: str,
    event_buffer: list[dict[str, Any]],
    state: _FlushState,
) -> None:
    from .pipeline_enrich import flush_events

    storage_info, event_ids = flush_events(pipeline, event_buffer, batch_id=batch_id)
    state.storage_infos.append(storage_info)
    state.stored_event_ids.extend(event_ids)
