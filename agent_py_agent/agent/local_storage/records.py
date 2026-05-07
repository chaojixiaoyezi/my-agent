# LLM: 这里连接 SQLite 行、正文文件和事件日志，改动需同时考虑三者一致性。
# 模块用途: LocalStore 记录写入、读取、内容文件映射和 upsert 事件。

from __future__ import annotations

"""owns LocalStore record upsert, lookup, content-file paths, and row hydration.

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


# LLM: LocalRecordInput 属于 LocalStore 本地事实索引 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: LocalStore 记录输入模型，承载 upsert 所需字段。
@dataclass(frozen=True)
class LocalRecordInput:
    source_type: str
    source_id: str
    title: str
    content: str
    metadata: dict[str, Any] | None = None
    visibility: str = "private"
    record_id: object | None = None


# LLM: LocalRecordLogInput 属于 LocalStore 本地事实索引 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: LocalStore 日志输入模型，承载追加记录所需字段。
@dataclass(frozen=True)
class LocalRecordLogInput:
    source_type: str
    source_id: str
    title: str
    content: str
    metadata: dict[str, Any] | None = None
    visibility: str = "private"
    record_id: object | None = None
    event_type: str = "local_record_logged"


# LLM: _PreparedRecord 属于 LocalStore 本地事实索引 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 写入前的 LocalStore 记录快照，保存正文、摘要和文件路径。
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


# LLM: _LocalStoreRecordHelpers 属于 LocalStore 本地事实索引 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: _LocalStoreRecordHelpers 数据模型，集中保存 LocalStore 本地事实索引 的结构化状态。
class _LocalStoreRecordHelpers:
    # LLM: _LocalStoreRecordHelpers._row_to_result 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 row_to_result 步骤，并保持调用方依赖的数据形状。
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

    # LLM: _LocalStoreRecordHelpers._read_content 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 读取 read_content 数据并转换成内部对象。
    def _read_content(self, row: sqlite3.Row) -> str:
        path = self._resolve_content_path(row["content_path"])
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        return row["content_preview"]

    # LLM: _LocalStoreRecordHelpers._content_file 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 content_file 步骤，并保持调用方依赖的数据形状。
    def _content_file(self, record_id: str) -> Path:
        return self.files_dir / f"{record_id}.txt"

    # LLM: _LocalStoreRecordHelpers._stored_path 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 stored_path 步骤，并保持调用方依赖的数据形状。
    def _stored_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    # LLM: _LocalStoreRecordHelpers._resolve_content_path 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 解析 resolve_content_path 并确认结果仍在允许边界内。
    def _resolve_content_path(self, stored_path: str) -> Path:
        path = Path(stored_path)
        if path.is_absolute():
            return path
        return self.root / path


# LLM: _LocalStoreRecordWriter 属于 LocalStore 本地事实索引 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: _LocalStoreRecordWriter 数据模型，集中保存 LocalStore 本地事实索引 的结构化状态。
class _LocalStoreRecordWriter(_LocalStoreRecordHelpers):
    # LLM: _LocalStoreRecordWriter.upsert_record 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 upsert_record 步骤，并保持调用方依赖的数据形状。
    def upsert_record(
        self,
        params: LocalRecordInput | None = None,
        *,
        source_type: str | None = None,
        source_id: str | None = None,
        title: str | None = None,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
        visibility: str = "private",
        record_id: object | None = None,
    ) -> LocalSearchResult:

        params = params or LocalRecordInput(
            source_type=str(source_type),
            source_id=str(source_id),
            title=str(title),
            content=str(content),
            metadata=metadata,
            visibility=visibility,
            record_id=record_id,
        )
        prepared = self._prepare_record(params)

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

    # LLM: _LocalStoreRecordWriter._prepare_record 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 prepare_record 步骤，并保持调用方依赖的数据形状。
    def _prepare_record(self, params: LocalRecordInput) -> _PreparedRecord:
        clean_source_type = params.source_type.strip() or "unknown"
        clean_source_id = params.source_id.strip() or str(uuid.uuid4())
        clean_title = params.title.strip() or clean_source_id
        clean_record_id = params.record_id or self.make_record_id(clean_source_type, clean_source_id)
        content_path = self._content_file(clean_record_id)
        content_path.parent.mkdir(parents=True, exist_ok=True)
        content_path.write_text(params.content, encoding="utf-8")
        metadata = params.metadata or {}
        return _PreparedRecord(
            record_id=clean_record_id,
            source_type=clean_source_type,
            source_id=clean_source_id,
            title=clean_title,
            content=params.content,
            metadata=metadata,
            metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            visibility=str(params.visibility),
            content_hash=hashlib.sha256(params.content.encode("utf-8")).hexdigest(),
            stored_path=self._stored_path(content_path),
            now=time.time(),
        )

    # LLM: _LocalStoreRecordWriter._existing_created_at 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 existing_created_at 步骤，并保持调用方依赖的数据形状。
    def _existing_created_at(self, conn: sqlite3.Connection, record_id: str, now: float) -> float:
        old = conn.execute("SELECT created_at FROM records WHERE id = ?", (record_id,)).fetchone()
        return float(old["created_at"]) if old else now

    # LLM: _LocalStoreRecordWriter._upsert_record_row 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 upsert_record_row 步骤，并保持调用方依赖的数据形状。
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

    # LLM: _LocalStoreRecordWriter._record_upsert_event 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把 record_upsert_event 写入本地存储并保留后续查询需要的字段。
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


# LLM: _LocalStoreRecordLogger 属于 LocalStore 本地事实索引 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: _LocalStoreRecordLogger 数据模型，集中保存 LocalStore 本地事实索引 的结构化状态。
class _LocalStoreRecordLogger:
    # LLM: _LocalStoreRecordLogger.log_record 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把 log_record 写入本地存储并保留后续查询需要的字段。
    def log_record(
        self,
        params: LocalRecordLogInput | None = None,
        *,
        source_type: str | None = None,
        source_id: str | None = None,
        title: str | None = None,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
        visibility: str = "private",
        record_id: object | None = None,
        event_type: str = "local_record_logged",
    ) -> LocalSearchResult:

        params = params or LocalRecordLogInput(
            source_type=str(source_type),
            source_id=str(source_id),
            title=str(title),
            content=str(content),
            metadata=metadata,
            visibility=visibility,
            record_id=record_id,
            event_type=event_type,
        )
        metadata = params.metadata or {}
        record = self.upsert_record(
            LocalRecordInput(
                source_type=params.source_type,
                source_id=params.source_id,
                title=params.title,
                content=params.content,
                metadata=metadata,
                visibility=params.visibility,
                record_id=params.record_id,
            )
        )
        self.record_event(
            str(params.event_type),
            record_id=record.id,
            payload={
                "source_type": params.source_type,
                "source_id": params.source_id,
                "title": params.title,
                **metadata,
            },
        )
        return record

    # LLM: _LocalStoreRecordLogger.get_record 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 查询并返回 get_record，保持返回形状给上层调用。
    def get_record(self, record_id: str) -> LocalSearchResult | None:
        """按 ID 读取一条记录。"""

        with self._connection() as conn:
            row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        return self._row_to_result(row) if row else None

    # LLM: _LocalStoreRecordLogger.make_record_id 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 按来源、类型和内容生成稳定记录 id。
    @staticmethod
    def make_record_id(source_type: str, source_id: str) -> str:
        """按来源生成稳定记录 ID。"""

        digest = hashlib.sha256(f"{source_type}\0{source_id}".encode()).hexdigest()
        return f"rec-{digest[:24]}"


# LLM: LocalStoreRecordMixin 属于 LocalStore 本地事实索引 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: LocalStoreRecordMixin 封装 LocalStore 本地事实索引 的一组相关操作，供上层组合调用。
class LocalStoreRecordMixin(_LocalStoreRecordWriter, _LocalStoreRecordLogger):
    """Compatibility facade for LocalStore record write, lookup, and log APIs."""
