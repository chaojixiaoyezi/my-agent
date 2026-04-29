from __future__ import annotations

"""LLM: owns LocalStore record upsert, lookup, content-file paths, and row hydration.

给人看的解释：
这个文件只管“记录本身”。
新增/更新一条记录、按 ID 读取、把数据库行变成搜索结果、正文文件放在哪，都归这里。
"""

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .models import PREVIEW_CHARS, LocalSearchResult


class LocalStoreRecordMixin:
    """LLM: mixin for record persistence and content-file mapping.

    给人看的解释：
    records 表像目录卡片，正文文件像内容仓库。
    这个 mixin 把两者绑在一起，保证同一来源重复索引时会更新同一条记录。
    """

    def upsert_record(
        self,
        *,
        source_type: str,
        source_id: str,
        title: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        visibility: str = "private",
        record_id: str | None = None,
    ) -> LocalSearchResult:
        """新增或更新一条本地记录。

        `source_type/source_id` 用来描述来源，例如：
        - `memory` + 某条记忆的稳定 ID
        - `gateway_request` + request_id
        - `subagent_run` + run_id

        没传 `record_id` 时会按来源生成稳定 ID，因此重复索引同一来源会覆盖旧记录，
        不会越写越多。
        """

        clean_source_type = source_type.strip() or "unknown"
        clean_source_id = source_id.strip() or str(uuid.uuid4())
        clean_title = title.strip() or clean_source_id
        record_id = record_id or self.make_record_id(clean_source_type, clean_source_id)
        now = time.time()
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        content_path = self._content_file(record_id)
        content_path.parent.mkdir(parents=True, exist_ok=True)
        content_path.write_text(content, encoding="utf-8")
        stored_path = self._stored_path(content_path)

        with self._connection() as conn:
            old = conn.execute("SELECT created_at FROM records WHERE id = ?", (record_id,)).fetchone()
            created_at = float(old["created_at"]) if old else now
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
                    record_id,
                    clean_source_type,
                    clean_source_id,
                    clean_title,
                    stored_path,
                    content_hash,
                    content[:PREVIEW_CHARS],
                    metadata_json,
                    visibility,
                    created_at,
                    now,
                ),
            )
            if self.fts_available:
                self._replace_fts_row(conn, record_id, clean_title, content)
            self._record_event(
                conn,
                "record_upserted",
                record_id,
                {
                    "source_type": clean_source_type,
                    "source_id": clean_source_id,
                    "title": clean_title,
                    "visibility": visibility,
                    "content_hash": content_hash,
                },
            )
            conn.commit()

        return self.get_record(record_id) or LocalSearchResult(
            id=record_id,
            source_type=clean_source_type,
            source_id=clean_source_id,
            title=clean_title,
            content=content,
            metadata=metadata or {},
            visibility=visibility,
            created_at=created_at,
            updated_at=now,
            content_path=stored_path,
        )

    def log_record(
        self,
        *,
        source_type: str,
        source_id: str,
        title: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        event_type: str = "local_record_logged",
        visibility: str = "private",
    ) -> LocalSearchResult:
        """写一条可搜索记录，并追加一条语义化审计事件。

        `upsert_record()` 只表达“索引里有这条记录”。
        `log_record()` 额外表达“发生了一件事”，适合 gateway/subagent/runner
        这类流程日志使用。
        """

        record = self.upsert_record(
            source_type=source_type,
            source_id=source_id,
            title=title,
            content=content,
            metadata=metadata or {},
            visibility=visibility,
        )
        self.record_event(
            event_type,
            record_id=record.id,
            payload={
                "source_type": source_type,
                "source_id": source_id,
                "title": title,
                **(metadata or {}),
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

        digest = hashlib.sha256(f"{source_type}\0{source_id}".encode("utf-8")).hexdigest()
        return f"rec-{digest[:24]}"

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
