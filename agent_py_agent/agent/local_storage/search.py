
# LLM: 搜索只读 owner LocalStore 派生索引；有结构化会话归属的 around 查询与 search_records_in_thread 不能混入其它会话。
# 模块用途: 统一全文检索、会话内全文检索、最近列表和会话内时间邻居查询，不从记录正文推断身份。
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from .models import LocalSearchResult


class LocalStoreSearchMixin:
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

    # LLM: 窗口沿锚记录的结构化 thread 取数；旧记录没有归属时仅代表时间邻居。
    # 函数用途: 查询锚点前后各若干条记录，保持 source/visibility 和会话条件一致。
    def records_around(
        self, record_id: str, *, window: int = 5,
        source_type: str | None = None, visibility: str | None = None,
    ) -> tuple[list[LocalSearchResult], int, int]:
        """以某条记录为锚,按 updated_at 时间序取前后各 window 条(见 _records_around)。"""
        return _records_around(self, record_id, window, (source_type, visibility))

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

    def _fts_query(self, query: str) -> str:
        """\u628a\u81ea\u7136\u8bed\u8a00\u67e5\u8be2\u7f16\u8bd1\u6210 records_fts(trigram)\u7684 MATCH \u8868\u8fbe\u5f0f(\u89c1 _compile_fts_match)\u3002"""
        return _compile_fts_match(query)

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


@dataclass(frozen=True)
class _WindowSide:
    """records_around 的单侧取数描述(把 where/params/锚/窗口打包,压参数个数)。"""

    where: str
    params: list[Any]
    anchor: LocalSearchResult
    window: int


# LLM: 会话约束进入 SQL 后才 LIMIT，避免跨会话记录占掉配额；缺身份时不能捏造会话关系。
# 函数用途: 校验锚记录的过滤条件，并在同一会话内提取时间窗口。
def _records_around(
    store: Any,
    record_id: str,
    window: int,
    filters: tuple[str | None, str | None],
) -> tuple[list[LocalSearchResult], int, int]:
    """records_around 的实现(挪到模块级,保持 search mixin 精简)。

    records 没有 长期助手 那样的连续 message-id,用 updated_at(并列用 id)的时间序
    当滚动轴:锚之后(更新)取 window 条、之前(更早)取 window 条,合成中心窗。
    filters=(source_type, visibility)。返回 (窗口记录按时间正序, before, after);
    锚不存在返回空窗。"""
    anchor = store.get_record(record_id) if hasattr(store, "get_record") else None
    if anchor is None:
        return [], 0, 0
    if (filters[0] and anchor.source_type != filters[0]) or (filters[1] and anchor.visibility != filters[1]):
        return [], 0, 0
    where, params = store._record_filters(source_type=filters[0], visibility=filters[1])
    thread_id = record_thread_id(anchor.metadata)
    if thread_id:
        prefix = f"{where} AND" if where else "WHERE"
        where = f"{prefix} {_thread_expression()} = ?"
        params = [*params, thread_id]
    side = _WindowSide(where=where, params=params, anchor=anchor, window=max(1, window))
    with store._connection() as conn:
        after = _fetch_window_side(conn, side, after=True)
        before = _fetch_window_side(conn, side, after=False)
    before_results = [store._row_to_result(row) for row in before][::-1]
    after_results = [store._row_to_result(row) for row in after]
    return [*before_results, anchor, *after_results], len(before_results), len(after_results)


# LLM: 只读取宿主写入的结构化 metadata；正文、标题、task_path 都不能代替 thread 身份。
# 函数用途: 取当前记录的会话 ID，供查询约束和结果标签共用。
def record_thread_id(metadata: object) -> str:
    if not isinstance(metadata, dict):
        return ""
    runtime = metadata.get("conversation_runtime")
    value = runtime.get("thread_id") if isinstance(runtime, dict) else None
    value = value or metadata.get("thread_id")
    return value if isinstance(value, str) else ""


def _fetch_window_side(conn: sqlite3.Connection, side: _WindowSide, *, after: bool) -> list[sqlite3.Row]:
    """取锚一侧(after=更新 / 否则更早)的 window 条记录,(updated_at, id) 为序键。"""
    prefix = f"{side.where} AND" if side.where else "WHERE"
    cmp, order = (">", "ASC") if after else ("<", "DESC")
    sql = (
        f"SELECT * FROM records {prefix} "
        f"(updated_at {cmp} ? OR (updated_at = ? AND id {cmp} ?)) "
        f"ORDER BY updated_at {order}, id {order} LIMIT ?"
    )
    anchor = side.anchor
    return conn.execute(
        sql, [*side.params, anchor.updated_at, anchor.updated_at, anchor.id, side.window]
    ).fetchall()


# LLM: 会话身份只认宿主写入的结构化字段：Gateway 请求记录在 conversation_runtime.thread_id，会话消息在 thread_id。
# 函数用途: 生成按会话过滤的 SQL 表达式（可带表名前缀），供会话内检索与时间窗口共用同一口径。
def _thread_expression(table: str = "") -> str:
    column = f"{table}.metadata_json" if table else "metadata_json"
    return (f"COALESCE(NULLIF(json_extract({column}, '$.conversation_runtime.thread_id'), ''), "
            f"json_extract({column}, '$.thread_id'))")


# LLM: 只携带结构化条件；thread_id 来自宿主可信上下文或记录 metadata，不接受正文推断的会话。
# 类用途: 会话内检索的过滤条件（会话、可选来源类型、条数），把参数打包以免函数签名过长。
@dataclass(frozen=True)
class ThreadSearch:
    thread_id: str
    source_type: str | None = None
    limit: int = 5


# LLM: 会话条件取宿主写入的 metadata（与 records_around 同一表达式）并在 SQL 里先于 LIMIT 生效，
#   其它会话的命中不能占掉配额；会话编号为空时返回空，不退化成全库检索。FTS 失败或无命中时按预览 LIKE 兜底。只读。
# 函数用途: 只在一个结构化会话的记录里做全文检索，供 session_search 的 current_thread 检索使用。
def search_records_in_thread(store: Any, query: str, scope: ThreadSearch) -> list[LocalSearchResult]:
    clean_query, clean_scope = query.strip(), ThreadSearch(scope.thread_id.strip(), scope.source_type, scope.limit)
    if clean_scope.limit <= 0 or not clean_query or not clean_scope.thread_id:
        return []
    fts_query = store._fts_query(clean_query) if store.fts_available else ""
    if fts_query:
        try:
            hits = _search_thread_fts(store, fts_query, clean_scope)
        except sqlite3.OperationalError:
            hits = []
        if hits:
            return hits
    return _search_thread_like(store, clean_query, clean_scope)


# LLM: FTS 与会话条件同一条 SQL；排序沿用 search() 的 bm25 再按更新时间。只读。
# 函数用途: 用全文索引在指定会话内检索记录。
def _search_thread_fts(store: Any, fts_query: str, scope: ThreadSearch) -> list[LocalSearchResult]:
    clauses, params = _record_fts_filters(fts_query, scope.source_type, None)
    clauses.append(f"{_thread_expression('records')} = ?")
    sql = f"""
        SELECT records.*, bm25(records_fts) AS rank
        FROM records_fts
        JOIN records ON records_fts.id = records.id
        WHERE {" AND ".join(clauses)}
        ORDER BY rank, records.updated_at DESC
        LIMIT ?
    """
    with store._connection() as conn:
        rows = conn.execute(sql, [*params, scope.thread_id, scope.limit]).fetchall()
    return [store._row_to_result(row, score=float(row["rank"])) for row in rows]


# LLM: 兜底只匹配标题、预览和来源编号（与 _search_like 相同口径），会话条件同样在 LIMIT 之前。只读。
# 函数用途: 全文索引不可用或无命中时，在指定会话内按子串检索记录。
def _search_thread_like(store: Any, query: str, scope: ThreadSearch) -> list[LocalSearchResult]:
    clauses, params = _record_like_filters(query, scope.source_type, None)
    clauses.append(f"{_thread_expression()} = ?")
    with store._connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM records WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?",
            [*params, scope.thread_id, scope.limit],
        ).fetchall()
    return [store._row_to_result(row, score=0.0) for row in rows]


_CJK_RE = re.compile(r"[一-鿿]")


def _compile_fts_match(query: str) -> str:
    """把自然语言查询编译成 records_fts(trigram)的 MATCH 表达式。

    中文检索失灵根因之一(查询端):旧实现把一整段汉字当成 1 个精确短语
    ('"记忆推送模式已落地"'),trigram 下要求整串连续出现,只命中超串、漏掉所有
    部分重合的记忆。现在:长汉字串(>3 字)拆成重叠 3-gram 再 OR——任一 3 字窗口
    重合即召回(trigram 索引天然支持子串);ASCII 词与 <=3 字短串按整词处理
    (1-2 字查询命中不了由 search() 的 LIKE 兜底)。所有片段加引号,避免 OR/括号
    等被 FTS5 当语法符号。"""
    tokens = re.findall(r"[\w]+|[一-鿿]+", query, flags=re.UNICODE)
    fragments = [
        frag
        for raw in tokens
        if (token := raw.replace('"', "").strip())
        for frag in _fts_fragments(token)
    ]
    return " OR ".join(f'"{frag}"' for frag in dict.fromkeys(fragments))


def _fts_fragments(token: str) -> list[str]:
    """把单个查询 token 拆成用于 trigram MATCH 的片段。

    纯中文且 >3 字:拆成重叠 3-gram(trigram 的命中粒度),任一窗口重合即召回。
    其它(ASCII 词、<=3 字中文、混合串):原样返回,作为一个整词片段。"""
    if len(token) > 3 and _CJK_RE.search(token) and all(_CJK_RE.match(ch) for ch in token):
        return [token[idx : idx + 3] for idx in range(len(token) - 2)]
    return [token]


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


def _append_optional_filter(
    clauses: list[str],
    values: list[Any],
    field: str,
    value: str | None,
) -> None:
    if value:
        clauses.append(f"{field} = ?")
        values.append(value)
