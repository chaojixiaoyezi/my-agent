from __future__ import annotations

"""LLM: owns LocalStore record upsert, lookup, content-file paths, and row hydration.

给人看的解释：
这个文件只管'记录本身'。
新增/更新一条记录、按 ID 读取、把数据库行变成搜索结果、正文文件放在哪，都归这里。
"""

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import PREVIEW_CHARS, LocalSearchResult


@dataclass(frozen=True)
class _RecordInput:
    source_type: str
    source_id: str
    title: str
    content: str
    metadata: dict[str, Any]
    visibility: str
    record_id: object | None


@dataclass(frozen=True)
class _PreparedRecord:
    record_id: str
    source_type: str
    source_id: str
    title: str
    content: str
    metadata: dict[str, Any]
    metadata_json: str
    visibility: str
    content_hash: str
    stored_path: str
    now: float


class _LocalStoreRecordHelpers:
    def _row_to_result(self, row: sqlite3.Row, *, score: float = 0.0) -> LocalSearchResult:
        metadata = json.loads(row["metadata_json"] or "{}")
        return LocalSearchResult(
            id=row["id"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            title=row["title"],
            content=self._read_content(row),
            metadata=metadata,
            visibility=row["visibility"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            score=score,
            content_path=row["content_path"],
        )

    def _read_content(self, row: sqlite3.Row) -> str:
        path = self._resolve_content_path(row["content_path"])
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        return row["content_preview"]

    def _content_file(self, record_id: str) -> Path:
        return self.files_dir / f"{record_id}.txt"

    def _stored_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def _resolve_content_path(self, stored_path: str) -> Path:
        path = Path(stored_path)
        if path.is_absolute():
            return path
        return self.root / path


class LocalStoreRecordMixin(_LocalStoreRecordHelpers):

    def upsert_record(self, **kwargs: Any) -> LocalSearchResult:

        prepared = self._prepare_record(
            _RecordInput(
                source_type=str(kwargs["source_type"]),
                source_id=str(kwargs["source_id"]),
                title=str(kwargs["title"]),
                content=str(kwargs["content"]),
                metadata=kwargs.get("metadata") or {},
                visibility=str(kwargs.get("visibility", "private")),
                record_id=kwargs.get("record_id"),
            )
        )

        with self._connection() as conn:
            created_at = self._existing_created_at(conn, prepared.record_id, prepared.now)
            self._upsert_record_row(conn, prepared, created_at)
            if self.fts_available:
                self._replace_fts_row(conn, prepared.record_id, prepared.title, prepared.content)
            self._record_upsert_event(conn, prepared)
            conn.commit()

        return self.get_record(prepared.record_id) or LocalSearchResult(
            id=prepared.record_id,
            source_type=prepared.source_type,
            source_id=prepared.source_id,
            title=prepared.title,
            content=prepared.content,
            metadata=prepared.metadata,
            visibility=prepared.visibility,
            created_at=created_at,
            updated_at=prepared.now,
            content_path=prepared.stored_path,
        )

    def _prepare_record(self, params: _RecordInput) -> _PreparedRecord:
        clean_source_type = params.source_type.strip() or "unknown"
        clean_source_id = params.source_id.strip() or str(uuid.uuid4())
        clean_title = params.title.strip() or clean_source_id
        clean_record_id = params.record_id or self.make_record_id(clean_source_type, clean_source_id)
        content_path = self._content_file(clean_record_id)
        content_path.parent.mkdir(parents=True, exist_ok=True)
        content_path.write_text(params.content, encoding="utf-8")
        return _PreparedRecord(
            record_id=clean_record_id,
            source_type=clean_source_type,
            source_id=clean_source_id,
            title=clean_title,
            content=params.content,
            metadata=params.metadata,
            metadata_json=json.dumps(params.metadata, ensure_ascii=False, sort_keys=True),
            visibility=params.visibility,
            content_hash=hashlib.sha256(params.content.encode("utf-8")).hexdigest(),
            stored_path=self._stored_path(content_path),
            now=time.time(),
        )

    def _existing_created_at(self, conn: sqlite3.Connection, record_id: str, now: float) -> float:
        old = conn.execute("SELECT created_at FROM records WHERE id = ?", (record_id,)).fetchone()
        return float(old["created_at"]) if old else now

    def _upsert_record_row(self, conn: sqlite3.Connection, record: _PreparedRecord, created_at: float) -> None:
        conn.execute(
            """
            INSERT INTO records (
                id, source_type, source_id, title, content_path, content_hash,
                content_preview, metadata_json, visibility, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                source_type=excluded.source_type,
                source_id=excluded.source_id,
                title=excluded.title,
                content_path=excluded.content_path,
                content_hash=excluded.content_hash,
                content_preview=excluded.content_preview,
                metadata_json=excluded.metadata_json,
                visibility=excluded.visibility,
                updated_at=excluded.updated_at
            """,
            (
                record.record_id,
                record.source_type,
                record.source_id,
                record.title,
                record.stored_path,
                record.content_hash,
                record.content[:PREVIEW_CHARS],
                record.metadata_json,
                record.visibility,
                created_at,
                record.now,
            ),
        )

    def _record_upsert_event(self, conn: sqlite3.Connection, record: _PreparedRecord) -> None:
        self._record_event(
            conn,
            "record_upserted",
            record.record_id,
            {
                "source_type": record.source_type,
                "source_id": record.source_id,
                "title": record.title,
                "visibility": record.visibility,
                "content_hash": record.content_hash,
            },
        )

    def log_record(self, **kwargs: Any) -> LocalSearchResult:

        metadata = kwargs.get("metadata") or {}
        record = self.upsert_record(**{**kwargs, "metadata": metadata})
        self.record_event(
            str(kwargs.get("event_type", "local_record_logged")),
            record_id=record.id,
            payload={
                "source_type": kwargs["source_type"],
                "source_id": kwargs["source_id"],
                "title": kwargs["title"],
                **metadata,
            },
        )
        return record

    def get_record(self, record_id: str) -> LocalSearchResult | None:
        """按 ID 读取一条记录。"""

        with self._connection() as conn:
            row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        return self._row_to_result(row) if row else None

    @staticmethod
    def make_record_id(source_type: str, source_id: str) -> str:
        """按来源生成稳定记录 ID。"""

        digest = hashlib.sha256(f"{source_type}\0{source_id}".encode()).hexdigest()
        return f"rec-{digest[:24]}"
