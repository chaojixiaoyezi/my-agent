from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


class ParserError(ValueError):
    """Raised when one raw record cannot be parsed into a normalized event."""


@dataclass(frozen=True)
class ParsedRecord:
    """One successfully parsed log record."""

    event: dict[str, Any]
    parser_id: str
    parser_confidence: float
    raw_ref: str
    line_no: int | None = None


@dataclass(frozen=True)
class ParseFailure:
    """A parse failure ready to be written to dead-letter storage."""

    reason: str
    raw_ref: str
    line_no: int | None = None
    raw_line: str | None = None
    raw_fields: Mapping[str, Any] = field(default_factory=dict)
    parser_id: str | None = None


@dataclass(frozen=True)
class ParseContext:
    raw_ref: str
    source_id: str | None = None
    source_product: str | None = None
    line_no: int | None = None

    @classmethod
    def from_kwargs(cls, **kwargs: Any) -> ParseContext:
        return cls(
            raw_ref=str(kwargs["raw_ref"]),
            source_id=kwargs.get("source_id"),
            source_product=kwargs.get("source_product"),
            line_no=kwargs.get("line_no"),
        )


class LogParser(Protocol):
    """Protocol implemented by log parsers used by the ingest pipeline."""

    parser_id: str
    schema: str
    supported_formats: tuple[str, ...]

    def parse_record(
        self,
        record: Mapping[str, Any],
        *,
        context: ParseContext | None = None,
        **kwargs: Any,
    ) -> ParsedRecord:
        """Parse one already decoded mapping."""

    def parse_json_line(
        self,
        line: str,
        *,
        context: ParseContext | None = None,
        **kwargs: Any,
    ) -> ParsedRecord:
        """Parse one JSONL line."""

    def parse_csv_row(
        self,
        row: Mapping[str, Any],
        *,
        context: ParseContext | None = None,
        **kwargs: Any,
    ) -> ParsedRecord:
        """Parse one CSV row from csv.DictReader."""
