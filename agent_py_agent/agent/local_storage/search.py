# LLM: 搜索结果排序和过滤影响记忆检索质量，变更时同步检索测试。
# 模块用途: LocalStore 的 FTS 与 LIKE 搜索、最近列表和筛选条件拼装。

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .models import LocalSearchResult


# LLM: LocalStoreSearchMixin 属于 LocalStore 本地事实索引 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: LocalStoreSearchMixin 封装 LocalStore 本地事实索引 的一组相关操作，供上层组合调用。
class LocalStoreSearchMixin:
    # LLM: LocalStoreSearchMixin.search 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 按查询词检索候选工具或本地记录并返回排序结果。
    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        source_type: str | None = None,
        visibility: str | None = None,
    ) -> list[LocalSearchResult]:
        clean_query = query.strip()
        if limit <= 0:
            return []
        if not clean_query:
            return self.list_recent(limit=limit, source_type=source_type, visibility=visibility)
        if self.fts_available:
            hits = self._safe_search_fts(clean_query, limit=limit, source_type=source_type, visibility=visibility)
            if hits:
                return hits
        return self._search_like(
            clean_query,
            limit=limit,
            source_type=source_type,
            visibility=visibility,
        )

    # LLM: LocalStoreSearchMixin.list_recent 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 列出符合条件的 list_recent 结果并遵守数量上限。
    def list_recent(
        self,
        *,
        limit: int = 20,
        source_type: str | None = None,
        visibility: str | None = None,
    ) -> list[LocalSearchResult]:
        if limit <= 0:
            return []
        where, params = self._record_filters(source_type=source_type, visibility=visibility)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM records {where} ORDER BY updated_at DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [self._row_to_result(row) for row in rows]

    # LLM: LocalStoreSearchMixin._record_filters 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把 record_filters 写入本地存储并保留后续查询需要的字段。
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

    # LLM: LocalStoreSearchMixin._search_fts 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 search_fts 步骤，并保持调用方依赖的数据形状。
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
        clauses, params = _record_fts_filters(fts_query, source_type, visibility)
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

    # LLM: LocalStoreSearchMixin._search_like 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 search_like 步骤，并保持调用方依赖的数据形状。
    def _search_like(
        self,
        query: str,
        *,
        limit: int,
        source_type: str | None,
        visibility: str | None,
    ) -> list[LocalSearchResult]:
        clauses, params = _record_like_filters(query, source_type, visibility)
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

    # LLM: LocalStoreSearchMixin._fts_query 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 fts_query 步骤，并保持调用方依赖的数据形状。
    def _fts_query(self, query: str) -> str:
        tokens = re.findall(r"[\w]+|[\u4e00-\u9fff]+", query, flags=re.UNICODE)
        cleaned = [token.replace('"', "").strip() for token in tokens]
        parts = [f'"{token}"' for token in cleaned if token]
        return " OR ".join(parts)

    # LLM: LocalStoreSearchMixin._replace_fts_row 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 replace_fts_row 步骤，并保持调用方依赖的数据形状。
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

    # LLM: LocalStoreSearchMixin._safe_search_fts 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 safe_search_fts 步骤，并保持调用方依赖的数据形状。
    def _safe_search_fts(
        self,
        query: str,
        *,
        limit: int,
        source_type: str | None,
        visibility: str | None,
    ) -> list[LocalSearchResult]:
        try:
            return self._search_fts(
                query,
                limit=limit,
                source_type=source_type,
                visibility=visibility,
            )
        except sqlite3.OperationalError:
            return self._search_like(
                query,
                limit=limit,
                source_type=source_type,
                visibility=visibility,
            )


# LLM: _record_fts_filters 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把 record_fts_filters 写入本地存储并保留后续查询需要的字段。
def _record_fts_filters(
    fts_query: str,
    source_type: str | None,
    visibility: str | None,
) -> tuple[list[str], list[Any]]:
    clauses = ["records_fts MATCH ?"]
    params: list[Any] = [fts_query]
    _append_optional_filter(clauses, params, "records.source_type", source_type)
    _append_optional_filter(clauses, params, "records.visibility", visibility)
    return clauses, params


# LLM: _record_like_filters 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把 record_like_filters 写入本地存储并保留后续查询需要的字段。
def _record_like_filters(
    query: str,
    source_type: str | None,
    visibility: str | None,
) -> tuple[list[str], list[Any]]:
    like = f"%{query}%"
    clauses = ["(title LIKE ? OR content_preview LIKE ? OR source_id LIKE ?)"]
    params: list[Any] = [like, like, like]
    _append_optional_filter(clauses, params, "source_type", source_type)
    _append_optional_filter(clauses, params, "visibility", visibility)
    return clauses, params


# LLM: _append_optional_filter 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 向结果或告警集合加入 append_optional_filter，同时保留调用方依赖的顺序。
def _append_optional_filter(
    clauses: list[str],
    values: list[Any],
    field: str,
    value: str | None,
) -> None:
    if value:
        clauses.append(f"{field} = ?")
        values.append(value)
