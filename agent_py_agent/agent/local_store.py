from __future__ import annotations

"""本地事实源：SQLite + FTS5 + 文件系统 + JSONL。

这一层的定位不是替代所有文件记录，而是给本地 agent 一个稳定的“账本底座”：
- SQLite 普通表保存结构化索引，方便统计、过滤、恢复。
- SQLite FTS5 虚拟表保存全文索引，方便本地关键词检索。
- 文件系统保存正文和未来大 artifact，避免把大块文本硬塞进数据库。
- JSONL 保存追加式审计事件，方便人工排查，也方便以后同步到远端。

如果当前 Python 自带的 SQLite 没编译 FTS5，本模块会自动退回 LIKE 检索；
数据仍然会正常落盘，只是全文检索能力弱一些。
"""

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


PREVIEW_CHARS = 12000


@dataclass
class LocalStoreEvent:
    """一条本地审计事件。"""

    event_id: str
    event_type: str
    record_id: str
    payload: dict[str, Any]
    created_at: float


@dataclass
class LocalTimelineItem:
    """时间线里展示的一条事件。"""

    event_id: str
    event_type: str
    record_id: str
    source_type: str
    source_id: str
    title: str
    payload: dict[str, Any]
    created_at: float


@dataclass
class LocalSearchResult:
    """一次本地检索命中的记录。"""

    id: str
    source_type: str
    source_id: str
    title: str
    content: str
    metadata: dict[str, Any]
    visibility: str
    created_at: float
    updated_at: float
    score: float = 0.0
    content_path: str = ""


class LocalStore:
    """本地事实源。

    大白话版：
    - `records` 表像目录卡片，记录“有什么东西、来自哪里、文件在哪”。
    - `records_fts` 像本地搜索引擎，专门用来搜标题和正文。
    - `files/` 放正文，后续也可以放截图、报告、patch 等大文件。
    - `events.jsonl` 是流水账，所有写入动作都追加一行。
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        files_dir: str | Path | None = None,
        events_path: str | Path | None = None,
        enable_fts: bool = True,
    ):
        self.db_path = Path(db_path)
        self.root = self.db_path.parent
        self.files_dir = Path(files_dir) if files_dir is not None else self.root / "files"
        self.events_path = Path(events_path) if events_path is not None else self.root / "events.jsonl"
        self.enable_fts = enable_fts
        self._fts_available = False
        self._init_schema()

    @property
    def fts_available(self) -> bool:
        """当前运行环境是否能使用 FTS5。"""

        return self.enable_fts and self._fts_available

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

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        source_type: str | None = None,
        visibility: str | None = None,
    ) -> list[LocalSearchResult]:
        """搜索本地记录。

        优先走 FTS5；如果 FTS5 不可用，或查询语法被 SQLite 拒绝，就退回 LIKE。
        这里把“能搜到”放在第一位，不让检索语法的小毛刺影响主代理运行。
        """

        clean_query = query.strip()
        if limit <= 0:
            return []
        if not clean_query:
            return self.list_recent(limit=limit, source_type=source_type, visibility=visibility)
        if self.fts_available:
            try:
                hits = self._search_fts(
                    clean_query,
                    limit=limit,
                    source_type=source_type,
                    visibility=visibility,
                )
                if hits:
                    return hits
            except sqlite3.OperationalError:
                return self._search_like(
                    clean_query,
                    limit=limit,
                    source_type=source_type,
                    visibility=visibility,
                )
        return self._search_like(
            clean_query,
            limit=limit,
            source_type=source_type,
            visibility=visibility,
        )

    def list_recent(
        self,
        *,
        limit: int = 20,
        source_type: str | None = None,
        visibility: str | None = None,
    ) -> list[LocalSearchResult]:
        """列出最近更新的记录。"""

        if limit <= 0:
            return []
        where, params = self._record_filters(source_type=source_type, visibility=visibility)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM records {where} ORDER BY updated_at DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [self._row_to_result(row) for row in rows]

    def timeline(
        self,
        *,
        limit: int = 20,
        source_type: str | None = None,
        event_type: str | None = None,
    ) -> list[LocalTimelineItem]:
        """读取最近本地事件。

        这是真正给 `my-agent timeline/status` 用的视图：
        它不读 JSONL 文件，而是从 SQLite 事件表读取，并尽量 join 到 records，
        这样能同时显示事件类型和对应记录标题。
        """

        if limit <= 0:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if source_type:
            clauses.append("records.source_type = ?")
            params.append(source_type)
        if event_type:
            clauses.append("events.event_type = ?")
            params.append(event_type)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    events.event_id,
                    events.event_type,
                    events.record_id,
                    events.payload_json,
                    events.created_at,
                    records.source_type,
                    records.source_id,
                    records.title
                FROM events
                LEFT JOIN records ON records.id = events.record_id
                {where}
                ORDER BY events.created_at DESC, events.seq DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return [self._timeline_row(row) for row in rows]

    def record_event(
        self,
        event_type: str,
        *,
        record_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> LocalStoreEvent:
        """追加一条自定义审计事件。"""

        with self._connection() as conn:
            event = self._record_event(conn, event_type, record_id, payload or {})
            conn.commit()
        return event

    def rebuild_fts(self) -> int:
        """用 `records` 表和文件系统内容重建 FTS5 索引。"""

        if not self.fts_available:
            return 0
        with self._connection() as conn:
            conn.execute("DELETE FROM records_fts")
            rows = conn.execute("SELECT id, title, content_path, content_preview FROM records").fetchall()
            count = 0
            for row in rows:
                content = self._read_content(row)
                self._replace_fts_row(conn, row["id"], row["title"], content)
                count += 1
            self._record_event(conn, "fts_rebuilt", "", {"count": count})
            conn.commit()
        return count

    def reset(self, *, remove_content_files: bool = False, reset_events_file: bool = True) -> None:
        """清空本地索引表，供 `local-rebuild --reset` 从文件事实源重建。

        默认不删正文文件，只清 records/events/FTS。这样即使用户误操作，原始
        JSONL、gateway 队列、subagent 工单和已写出的正文文件仍然在磁盘上。
        """

        with self._connection() as conn:
            if self.fts_available:
                conn.execute("DELETE FROM records_fts")
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM records")
            conn.commit()
        if reset_events_file:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            self.events_path.write_text("", encoding="utf-8")
        if remove_content_files and self.files_dir.exists():
            for path in self.files_dir.glob("*.txt"):
                try:
                    path.unlink()
                except OSError:
                    continue

    def count_records(self, *, source_type: str | None = None) -> int:
        """统计记录数，可按 source_type 过滤。"""

        where, params = self._record_filters(source_type=source_type)
        with self._connection() as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM records {where}", params).fetchone()[0])

    def source_counts(self) -> dict[str, int]:
        """按 source_type 汇总记录数。"""

        with self._connection() as conn:
            rows = conn.execute(
                "SELECT source_type, COUNT(*) AS count FROM records GROUP BY source_type ORDER BY source_type"
            ).fetchall()
        return {str(row["source_type"]): int(row["count"]) for row in rows}

    def missing_content_files(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """检查 records 指向的正文文件是否还存在。"""

        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, source_type, source_id, title, content_path
                FROM records
                ORDER BY updated_at DESC
                """
            ).fetchall()
        missing: list[dict[str, Any]] = []
        for row in rows:
            path = self._resolve_content_path(row["content_path"])
            if path.exists():
                continue
            missing.append(
                {
                    "id": row["id"],
                    "source_type": row["source_type"],
                    "source_id": row["source_id"],
                    "title": row["title"],
                    "content_path": str(path),
                }
            )
            if limit > 0 and len(missing) >= limit:
                break
        return missing

    def stats(self) -> dict[str, Any]:
        """返回本地事实源的当前状态。"""

        with self._connection() as conn:
            record_count = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return {
            "db_path": str(self.db_path),
            "files_dir": str(self.files_dir),
            "events_path": str(self.events_path),
            "fts5_enabled": self.fts_available,
            "record_count": record_count,
            "event_count": event_count,
        }

    @staticmethod
    def make_record_id(source_type: str, source_id: str) -> str:
        """按来源生成稳定记录 ID。"""

        digest = hashlib.sha256(f"{source_type}\0{source_id}".encode("utf-8")).hexdigest()
        return f"rec-{digest[:24]}"

    def _init_schema(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    content_preview TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    visibility TEXT NOT NULL DEFAULT 'private',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_records_source
                ON records(source_type, source_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_records_updated
                ON records(updated_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    record_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                )
                """
            )
            if self.enable_fts:
                try:
                    conn.execute(
                        """
                        CREATE VIRTUAL TABLE IF NOT EXISTS records_fts
                        USING fts5(id UNINDEXED, title, content)
                        """
                    )
                    self._fts_available = True
                except sqlite3.OperationalError:
                    self._fts_available = False
            conn.execute(
                """
                INSERT INTO metadata(key, value)
                VALUES('fts5_enabled', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                ("true" if self.fts_available else "false",),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # 另一个 gateway / CLI 进程短暂持锁时，状态查询不能因为切 WAL 失败而崩。
            pass
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _record_filters(
        self,
        *,
        source_type: str | None = None,
        visibility: str | None = None,
        table: str = "records",
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if source_type:
            clauses.append(f"{table}.source_type = ?")
            params.append(source_type)
        if visibility:
            clauses.append(f"{table}.visibility = ?")
            params.append(visibility)
        if not clauses:
            return "", params
        return "WHERE " + " AND ".join(clauses), params

    def _search_fts(
        self,
        query: str,
        *,
        limit: int,
        source_type: str | None,
        visibility: str | None,
    ) -> list[LocalSearchResult]:
        fts_query = self._fts_query(query)
        if not fts_query:
            return self._search_like(
                query,
                limit=limit,
                source_type=source_type,
                visibility=visibility,
            )
        clauses = ["records_fts MATCH ?"]
        params: list[Any] = [fts_query]
        if source_type:
            clauses.append("records.source_type = ?")
            params.append(source_type)
        if visibility:
            clauses.append("records.visibility = ?")
            params.append(visibility)
        sql = f"""
            SELECT records.*, bm25(records_fts) AS rank
            FROM records_fts
            JOIN records ON records_fts.id = records.id
            WHERE {" AND ".join(clauses)}
            ORDER BY rank, records.updated_at DESC
            LIMIT ?
        """
        with self._connection() as conn:
            rows = conn.execute(sql, [*params, limit]).fetchall()
        return [self._row_to_result(row, score=float(row["rank"])) for row in rows]

    def _search_like(
        self,
        query: str,
        *,
        limit: int,
        source_type: str | None,
        visibility: str | None,
    ) -> list[LocalSearchResult]:
        clauses = ["(title LIKE ? OR content_preview LIKE ? OR source_id LIKE ?)"]
        like = f"%{query}%"
        params: list[Any] = [like, like, like]
        if source_type:
            clauses.append("source_type = ?")
            params.append(source_type)
        if visibility:
            clauses.append("visibility = ?")
            params.append(visibility)
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM records
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return [self._row_to_result(row, score=0.0) for row in rows]

    def _fts_query(self, query: str) -> str:
        tokens = re.findall(r"[\w]+|[\u4e00-\u9fff]+", query, flags=re.UNICODE)
        cleaned = [token.replace('"', "").strip() for token in tokens]
        parts = [f'"{token}"' for token in cleaned if token]
        return " OR ".join(parts)

    def _replace_fts_row(
        self,
        conn: sqlite3.Connection,
        record_id: str,
        title: str,
        content: str,
    ) -> None:
        conn.execute("DELETE FROM records_fts WHERE id = ?", (record_id,))
        conn.execute(
            "INSERT INTO records_fts(id, title, content) VALUES (?, ?, ?)",
            (record_id, title, content),
        )

    def _record_event(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        record_id: str,
        payload: dict[str, Any],
    ) -> LocalStoreEvent:
        event = LocalStoreEvent(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            record_id=record_id,
            payload=payload,
            created_at=time.time(),
        )
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        conn.execute(
            """
            INSERT INTO events(event_id, event_type, record_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event.event_id, event.event_type, event.record_id, payload_json, event.created_at),
        )
        with self.events_path.open("a", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    {
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "record_id": event.record_id,
                        "payload": event.payload,
                        "created_at": event.created_at,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
        return event

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

    def _timeline_row(self, row: sqlite3.Row) -> LocalTimelineItem:
        payload = json.loads(row["payload_json"] or "{}")
        source_type = row["source_type"] or str(payload.get("source_type", ""))
        source_id = row["source_id"] or str(payload.get("source_id", ""))
        title = row["title"] or str(payload.get("title", ""))
        return LocalTimelineItem(
            event_id=row["event_id"],
            event_type=row["event_type"],
            record_id=row["record_id"],
            source_type=source_type,
            source_id=source_id,
            title=title,
            payload=payload,
            created_at=float(row["created_at"]),
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
