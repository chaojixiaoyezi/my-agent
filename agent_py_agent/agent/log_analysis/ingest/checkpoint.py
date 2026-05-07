from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..parsers.common import utc_now


@dataclass(frozen=True)
class Checkpoint:
    source_id: str
    cursor_kind: str
    cursor: dict[str, Any]
    last_committed_batch_id: str | None
    last_event_time: str | None
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "cursor_kind": self.cursor_kind,
            "cursor": self.cursor,
            "last_committed_batch_id": self.last_committed_batch_id,
            "last_event_time": self.last_event_time,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class CheckpointCommit:
    source_id: str
    cursor_kind: str
    cursor: Mapping[str, Any]
    last_committed_batch_id: str
    last_event_time: str | None


class CheckpointStore:
    """JSON checkpoint store scoped by source_id."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.checkpoints_dir = self.root / "checkpoints"

    def path_for(self, source_id: str) -> Path:
        return self.checkpoints_dir / f"{safe_source_id(source_id)}.json"

    def load(self, source_id: str) -> dict[str, Any]:
        path = self.path_for(source_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def commit(
        self,
        *,
        params: CheckpointCommit | None = None,
        commit: CheckpointCommit | None = None,
        source_id: str = "",
        cursor_kind: str = "",
        cursor: Mapping[str, Any] | None = None,
        last_committed_batch_id: str = "",
        last_event_time: str | None = None,
    ) -> Checkpoint:
        item = params or commit or CheckpointCommit(
            source_id=str(source_id),
            cursor_kind=str(cursor_kind),
            cursor=cursor or {},
            last_committed_batch_id=str(last_committed_batch_id),
            last_event_time=last_event_time,
        )
        checkpoint = Checkpoint(
            source_id=item.source_id,
            cursor_kind=item.cursor_kind,
            cursor=dict(item.cursor),
            last_committed_batch_id=item.last_committed_batch_id,
            last_event_time=item.last_event_time,
            updated_at=utc_now(),
        )
        write_json_atomic(self.path_for(item.source_id), checkpoint.to_dict())
        return checkpoint


def safe_source_id(source_id: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_.-]+", "_", source_id.strip())
    return value or "unknown"


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
