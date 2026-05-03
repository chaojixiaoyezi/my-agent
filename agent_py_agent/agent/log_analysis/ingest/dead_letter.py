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
        reason: str,
        raw_ref: str,
        line_no: int | None = None,
        raw_line: str | None = None,
        raw_fields: Mapping[str, Any] | None = None,
        parser_id: str | None = None,
    ) -> None:
        now = utc_now()
        record = {
            "dead_letter_id": f"dlq-{sha256_text(f'{self.batch_id}:{raw_ref}:{reason}')[:24]}",
            "batch_id": self.batch_id,
            "source_id": self.source_id,
            "parser_id": parser_id,
            "reason": reason,
            "raw_ref": raw_ref,
            "line_no": line_no,
            "raw_line_preview": _preview(raw_line),
            "raw_line_sha256": f"sha256:{sha256_text(raw_line or '')}",
            "raw_fields": dict(raw_fields or {}),
            "created_at": now,
        }
        append_jsonl(self.path, record, sort_keys=True)
        append_jsonl(
            self.diagnostic_path,
            {
                "event_type": "log_parse_failure",
                "batch_id": self.batch_id,
                "source_id": self.source_id,
                "raw_ref": raw_ref,
                "line_no": line_no,
                "reason": reason,
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

