
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


def _jsonable_mapping(mapping: Mapping[Any, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in mapping.items()}
