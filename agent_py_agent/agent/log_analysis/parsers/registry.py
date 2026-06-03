
from __future__ import annotations

from dataclasses import dataclass, field

from .base import LogParser, ParserError
from .common import DEFAULT_PAYLOAD_MAX_CHARS
from .security_alert_v1 import SecurityAlertV1Parser


@dataclass
class ParserRegistry:
    """Small parser registry for local ingest."""

    parsers: dict[str, LogParser] = field(default_factory=dict)

    def register(self, parser: LogParser) -> None:
        self.parsers[parser.parser_id] = parser

    def get(self, parser_id: str) -> LogParser:
        try:
            return self.parsers[parser_id]
        except KeyError as exc:
            raise ParserError(f"unknown parser_id: {parser_id}") from exc

    def choose(self, *, parser_id: str = "auto", file_format: str | None = None) -> LogParser:
        if parser_id and parser_id != "auto":
            parser = self.get(parser_id)
            if file_format and file_format not in parser.supported_formats:
                raise ParserError(f"parser {parser_id} does not support {file_format}")
            return parser

        for parser in self.parsers.values():
            if file_format is None or file_format in parser.supported_formats:
                return parser
        raise ParserError(f"no parser supports format: {file_format or 'unknown'}")


def default_registry(*, payload_max_chars: int = DEFAULT_PAYLOAD_MAX_CHARS) -> ParserRegistry:
    registry = ParserRegistry()
    registry.register(SecurityAlertV1Parser(payload_max_chars=payload_max_chars))
    return registry


def get_default_parser(*, payload_max_chars: int = DEFAULT_PAYLOAD_MAX_CHARS) -> LogParser:
    return default_registry(payload_max_chars=payload_max_chars).get("security_alert_v1")

