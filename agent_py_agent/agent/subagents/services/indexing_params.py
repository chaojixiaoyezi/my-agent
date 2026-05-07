from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LocalRecordParams:
    """Bundle of log_local_record parameters."""
    source_type: str
    source_id: str
    title: str
    content: str
    event_type: str
    metadata: dict[str, object] | None = None
