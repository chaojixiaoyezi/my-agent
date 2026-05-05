from __future__ import annotations

"""LLM: implements LocalStore FTS/LIKE search and record-filter SQL construction.

给人看的解释：
这个文件只管'怎么搜'。
能用 FTS5 就走全文索引，不能用或语法出问题就退回 LIKE，保证本地检索尽量可用。
"""

import re
import sqlite3
from typing import Any

from .models import LocalSearchResult


class LocalStoreSearchMixin:
    """LLM: mixin for recent-list, FTS search, LIKE fallback, and FTS row maintenance.

    给人看的解释：
    这里是 LocalStore 的搜索层，不负责写业务记录，只负责把查询条件翻译成 SQLite 查询。
    """

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
        这里把'能搜到'放在第一位，不让检索语法的小毛刺影响主代理运行。
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
