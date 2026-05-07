from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .base import ParseContext, ParsedRecord, ParserError
from .common import DEFAULT_PAYLOAD_MAX_CHARS, normalize_security_alert_v1


@dataclass(slots=True)
class SecurityAlertV1Parser:
    """Parser for the first SecurityAlertV1 CSV/JSONL alert schema."""

    payload_max_chars: int = DEFAULT_PAYLOAD_MAX_CHARS

    parser_id: str = "security_alert_v1"
    schema: str = "SecurityAlertV1"
    supported_formats: tuple[str, ...] = ("jsonl", "csv", "log")

    def parse_record(
        self,
        record: Mapping[str, Any],
        *,
        request: ParseContext | None = None,
        context: ParseContext | None = None,
        raw_ref: str = "",
        source_id: str | None = None,
        source_product: str | None = None,
        line_no: int | None = None,
    ) -> ParsedRecord:
        parse_context = request or context or ParseContext(str(raw_ref), source_id, source_product, line_no)
        if not isinstance(record, Mapping):
            raise ParserError("SecurityAlertV1 record must be an object")
        if not any(value not in (None, "") for value in record.values()):
            raise ParserError("SecurityAlertV1 record is empty")

        event = normalize_security_alert_v1(
            record,
            raw_ref=parse_context.raw_ref,
            source_id=parse_context.source_id,
            source_product=parse_context.source_product,
            line_no=parse_context.line_no,
            payload_max_chars=self.payload_max_chars,
        )
        return ParsedRecord(
            event=event,
            parser_id=self.parser_id,
            parser_confidence=float(event["parser_confidence"]),
            raw_ref=parse_context.raw_ref,
            line_no=parse_context.line_no,
        )

    def parse_json_line(
        self,
        line: str,
        *,
        request: ParseContext | None = None,
        context: ParseContext | None = None,
        raw_ref: str = "",
        source_id: str | None = None,
        source_product: str | None = None,
        line_no: int | None = None,
    ) -> ParsedRecord:
        parse_context = request or context or ParseContext(str(raw_ref), source_id, source_product, line_no)
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ParserError(f"invalid JSON: {exc.msg}") from exc
        if not isinstance(record, Mapping):
            raise ParserError("JSONL SecurityAlertV1 line must contain a JSON object")
        return self.parse_record(
            record,
            request=parse_context,
        )

    def parse_csv_row(
        self,
        row: Mapping[str, Any],
        *,
        request: ParseContext | None = None,
        context: ParseContext | None = None,
        raw_ref: str = "",
        source_id: str | None = None,
        source_product: str | None = None,
        line_no: int | None = None,
    ) -> ParsedRecord:
        parse_context = request or context or ParseContext(str(raw_ref), source_id, source_product, line_no)
        if None in row:
            raise ParserError("CSV row has more columns than the header")
        return self.parse_record(
            row,
            request=parse_context,
        )
