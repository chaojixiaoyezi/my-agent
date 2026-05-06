from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...file_io import append_jsonl
from ..parsers.common import sha256_text, utc_now
from .checkpoint import safe_source_id


@dataclass(frozen=True)
class DeadLetterRef:
    path: str
    count: int


@dataclass(frozen=True)
class DeadLetterRecord:
    reason: str
    raw_ref: str
    line_no: int | None = None
    raw_line: str | None = None
    raw_fields: Mapping[str, Any] | None = None
    parser_id: str | None = None

    @classmethod
    def from_kwargs(cls, **kwargs: Any) -> DeadLetterRecord:
        return cls(
            reason=str(kwargs["reason"]),
            raw_ref=str(kwargs["raw_ref"]),
            line_no=kwargs.get("line_no"),
            raw_line=kwargs.get("raw_line"),
            raw_fields=kwargs.get("raw_fields"),
            parser_id=kwargs.get("parser_id"),
        )


class DeadLetterWriter:
    """Append-only dead-letter writer for malformed ingest records."""

    def __init__(self, root: str | Path, *, source_id: str, batch_id: str):
        self.root = Path(root)
        self.source_id = source_id
        self.batch_id = batch_id
        safe_source = safe_source_id(source_id)
        self.path = self.root / "dead_letter" / safe_source / f"{batch_id}.jsonl"
        self.diagnostic_path = self.root / "events" / "dead_letter_diagnostics.jsonl"
        self.count = 0

    def write(
        self,
        *,
        record: DeadLetterRecord | None = None,
        **kwargs: Any,
    ) -> None:
        item = record or DeadLetterRecord.from_kwargs(**kwargs)
        now = utc_now()
        record = {
            "dead_letter_id": f"dlq-{sha256_text(f'{self.batch_id}:{item.raw_ref}:{item.reason}')[:24]}",
            "batch_id": self.batch_id,
            "source_id": self.source_id,
            "parser_id": item.parser_id,
            "reason": item.reason,
            "raw_ref": item.raw_ref,
            "line_no": item.line_no,
            "raw_line_preview": _preview(item.raw_line),
            "raw_line_sha256": f"sha256:{sha256_text(item.raw_line or '')}",
            "raw_fields": dict(item.raw_fields or {}),
            "created_at": now,
        }
        append_jsonl(self.path, record, sort_keys=True)
        append_jsonl(
            self.diagnostic_path,
            {
                "event_type": "log_parse_failure",
                "batch_id": self.batch_id,
                "source_id": self.source_id,
                "raw_ref": item.raw_ref,
                "line_no": item.line_no,
                "reason": item.reason,
                "dead_letter_path": str(self.path),
                "created_at": now,
            },
            sort_keys=True,
        )
        self.count += 1

    def refs(self) -> list[dict[str, Any]]:
        if self.count == 0:
            return []
        return [{"path": str(self.path), "count": self.count}]


def _preview(value: str | None, *, max_chars: int = 2048) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:max_chars]
