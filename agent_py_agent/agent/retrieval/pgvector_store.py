"""pgvector ANN 向量库(Tier 2 规模化记忆):十万级向量用 HNSW 索引做近似最近邻,O(log n) 召回。

与 VectorStore(纯 Python 暴力 cosine)**接口一致**,可平滑替换——vector_store.py 注释早留了伏笔
"量级真涨到十万级再换 ANN 后端,接口不变"。本类即那个后端:PostgreSQL + pgvector 扩展,
``embedding <=> q`` cosine 距离算子 + HNSW 索引,十万/百万级也是亚毫秒召回。

多租户数据隔离(研究确认的"没人做"数据面空白):每行带 ``tenant`` 列、每次 upsert/search/remove
强制按 tenant 过滤——租户数据物理同表、逻辑隔离(同表+租户列是中小规模最省运维的隔离;真到超大
租户再按 tenant 分区/独立索引,接口不变)。score 用 ``1 - cosine_distance`` 与 VectorStore 同义。

借库取舍:pgvector 扩展是 PostgreSQL 端能力(不能自建);但 Python 侧不引 pgvector-python——
向量按 ``'[...]'::vector`` 文本字面量传、jsonb 存元数据,raw SQL 自建即可(少一个依赖)。
本类是 PG 专用规模后端,故用 PG 专有 SQL(与可移植的 storage_backend 分工不同)。
"""

from __future__ import annotations

import json

from agent_py_agent.agent.retrieval.vector_store import VectorHit
from agent_py_agent.agent.storage_backend import StorageBackend

try:
    from sqlalchemy import text

    _HAS_SQLALCHEMY = True
except ImportError:
    _HAS_SQLALCHEMY = False


def _vec_literal(vector: list[float]) -> str:
    """list[float] → pgvector 文本字面量 '[1.0,2.0,...]'(再 ::vector 转型)。"""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _row_to_hit(row: object) -> VectorHit:
    meta = getattr(row, "metadata", None)
    return VectorHit(
        id=str(row.id),
        score=float(row.score),
        text=str(row.text or ""),
        metadata=dict(meta) if isinstance(meta, dict) else {},
    )


class PgVectorStore:
    """pgvector + HNSW 的 ANN 向量库,多租户隔离。接口同 VectorStore,可平滑替换。

    用法::

        store = PgVectorStore(backend, dim=384, tenant="acme")
        store.ensure_schema()                 # 建扩展/表/HNSW 索引(幂等)
        store.upsert("mem-1", vec, text="...", metadata={...})
        hits = store.search(query_vec, top_k=5)
    """

    def __init__(self, backend: StorageBackend, *, dim: int, tenant: str = "default", table: str = "vector_memory") -> None:
        if not backend.is_postgres:
            raise ValueError("PgVectorStore 需 PostgreSQL 后端(ANN 规模路径);SQLite 用 VectorStore 暴力库")
        if not table.replace("_", "").isalnum():
            raise ValueError("table 名只允许字母数字下划线(防注入)")
        self._backend = backend
        self._dim = int(dim)
        self._tenant = tenant
        self._table = table

    def ensure_schema(self) -> None:
        """建 pgvector 扩展 + 表 + HNSW cosine 索引(幂等)。需 DB 已装 pgvector 扩展。"""
        with self._backend.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.execute(text(
                f"CREATE TABLE IF NOT EXISTS {self._table} ("
                "tenant text NOT NULL, id text NOT NULL, "
                f"embedding vector({self._dim}) NOT NULL, "
                "doc_text text NOT NULL DEFAULT '', "
                "metadata jsonb NOT NULL DEFAULT '{}', "
                "PRIMARY KEY (tenant, id))"
            ))
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS {self._table}_hnsw "
                f"ON {self._table} USING hnsw (embedding vector_cosine_ops)"
            ))

    def upsert(self, id: str, vector: list[float], *, text: str = "", metadata: dict[str, object] | None = None) -> None:
        if not id:
            raise ValueError("vector id required")
        with self._backend.begin() as conn:
            conn.execute(self._upsert_sql(), self._row_params((id, vector, text, metadata or {})))

    def upsert_many(self, rows: list[tuple[str, list[float], str, dict[str, object]]]) -> None:
        params = [self._row_params(r) for r in rows if r[0]]
        if not params:
            return
        with self._backend.begin() as conn:
            conn.execute(self._upsert_sql(), params)  # list → executemany

    def remove(self, id: str) -> bool:
        with self._backend.begin() as conn:
            result = conn.execute(
                text(f"DELETE FROM {self._table} WHERE tenant = :t AND id = :id"),
                {"t": self._tenant, "id": id},
            )
        return result.rowcount > 0

    def search(self, query_vector: list[float], *, top_k: int = 5, min_score: float = 0.0) -> list[VectorHit]:
        """HNSW ANN 召回 top_k(cosine 降序、过滤 < min_score、只本租户)。"""
        sql = text(
            f"SELECT id, 1 - (embedding <=> (:q)::vector) AS score, doc_text AS text, metadata "
            f"FROM {self._table} "
            "WHERE tenant = :t AND (1 - (embedding <=> (:q)::vector)) >= :min_score "
            "ORDER BY embedding <=> (:q)::vector ASC LIMIT :k"
        )
        params = {"q": _vec_literal(query_vector), "t": self._tenant, "min_score": min_score, "k": max(0, top_k)}
        with self._backend.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_hit(r) for r in rows]

    def count(self) -> int:
        return int(self._backend.scalar(
            f"SELECT count(*) FROM {self._table} WHERE tenant = :t", {"t": self._tenant}
        ) or 0)

    def _upsert_sql(self) -> object:
        return text(
            f"INSERT INTO {self._table} (tenant, id, embedding, doc_text, metadata) "
            "VALUES (:t, :id, (:vec)::vector, :doc_text, (:meta)::jsonb) "
            "ON CONFLICT (tenant, id) DO UPDATE SET "
            "embedding = EXCLUDED.embedding, doc_text = EXCLUDED.doc_text, metadata = EXCLUDED.metadata"
        )

    def _row_params(self, row: tuple[str, list[float], str, dict[str, object]]) -> dict[str, object]:
        id, vector, doc_text, metadata = row
        return {
            "t": self._tenant,
            "id": id,
            "vec": _vec_literal(vector),
            "doc_text": doc_text or "",
            "meta": json.dumps(metadata or {}, ensure_ascii=False),
        }
