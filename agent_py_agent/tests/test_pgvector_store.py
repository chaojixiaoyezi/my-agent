"""Tier 2 pgvector ANN 真测:真 PostgreSQL + pgvector 扩展。

真实测试(用户要求):需 DB 装了 pgvector 扩展。无可用 PG 或扩展装不上 → skip(不假测)。
验:upsert → HNSW cosine 召回排序、min_score 过滤、多租户隔离(同 id 不同租户互不可见)、批量/删除。
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("sqlalchemy")
from sqlalchemy import text  # noqa: E402

from agent_py_agent.agent.retrieval.pgvector_store import PgVectorStore  # noqa: E402
from agent_py_agent.agent.storage_backend import StorageBackend  # noqa: E402

_TABLE = "test_vector_memory_tier2"


def _pg_with_vector() -> StorageBackend:
    url = os.environ.get("TEST_POSTGRES_URL", "postgresql+psycopg://localhost:5432/postgres")
    try:
        db = StorageBackend(url)
        with db.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        return db
    except Exception as exc:  # 无 PG / 无 pgvector / 无建扩展权限 → 跳过(不假测)
        pytest.skip(f"无可用 PostgreSQL+pgvector 做真测: {type(exc).__name__}")


def _drop(db: StorageBackend) -> None:
    with db.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {_TABLE}"))


@pytest.fixture
def store():
    db = _pg_with_vector()
    _drop(db)
    s = PgVectorStore(db, dim=3, tenant="acme", table=_TABLE)
    s.ensure_schema()
    yield s, db
    _drop(db)
    db.dispose()


def test_upsert_and_ann_search_ranks_by_cosine(store) -> None:
    s, _ = store
    s.upsert("a", [1.0, 0.0, 0.0], text="east")
    s.upsert("b", [0.0, 1.0, 0.0], text="north")
    s.upsert("c", [0.9, 0.1, 0.0], text="east-ish")
    hits = s.search([1.0, 0.0, 0.0], top_k=2)
    assert [h.id for h in hits] == ["a", "c"]  # 同向最近,east-ish 次之,north 落选
    assert hits[0].score > hits[1].score
    assert hits[0].text == "east"


def test_min_score_filters_dissimilar(store) -> None:
    s, _ = store
    s.upsert("a", [1.0, 0.0, 0.0])
    s.upsert("opp", [-1.0, 0.0, 0.0])  # 反向,cosine = -1
    hits = s.search([1.0, 0.0, 0.0], top_k=5, min_score=0.5)
    assert [h.id for h in hits] == ["a"]  # opp 被 min_score 挡掉


def test_multi_tenant_isolation(store) -> None:
    s, db = store
    s.upsert("shared-id", [1.0, 0.0, 0.0], text="acme-data")
    other = PgVectorStore(db, dim=3, tenant="globex", table=_TABLE)
    other.upsert("shared-id", [1.0, 0.0, 0.0], text="globex-data")  # 同 id、不同租户
    acme_hits = s.search([1.0, 0.0, 0.0], top_k=5)
    assert len(acme_hits) == 1 and acme_hits[0].text == "acme-data"  # 只看到自己的
    globex_hits = other.search([1.0, 0.0, 0.0], top_k=5)
    assert globex_hits[0].text == "globex-data"
    assert s.count() == 1 and other.count() == 1  # 各租户只算自己的


def test_upsert_many_metadata_and_remove(store) -> None:
    s, _ = store
    s.upsert_many([
        ("x", [1.0, 0.0, 0.0], "tx", {"k": 1}),
        ("y", [0.0, 1.0, 0.0], "ty", {"k": 2}),
    ])
    assert s.count() == 2
    hits = s.search([0.0, 1.0, 0.0], top_k=1)
    assert hits[0].id == "y"
    assert hits[0].metadata == {"k": 2}  # jsonb 元数据原样回来
    assert s.remove("y") is True
    assert s.remove("nope") is False
    assert s.count() == 1
