"""Tier 2 加固 RLS 真测:真 PostgreSQL 行级安全在 DB 层强制租户隔离。

关键:超级用户绕过 RLS,故测试在事务里 ``SET LOCAL ROLE`` 切到非超级用户角色,让 RLS 真正生效,
证明即便按 id 直查也拿不到别租户的行。无可用 PG → skip(不假测)。
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("sqlalchemy")
from sqlalchemy import text  # noqa: E402

from agent_py_agent.agent.pg_rls import enable_tenant_rls  # noqa: E402
from agent_py_agent.agent.storage_backend import StorageBackend  # noqa: E402

_TABLE = "test_rls_tier2"
_ROLE = "rls_test_role"


def _pg() -> StorageBackend:
    url = os.environ.get("TEST_POSTGRES_URL", "postgresql+psycopg://localhost:5432/postgres")
    try:
        db = StorageBackend(url)
        with db.connect() as conn:
            conn.execute(text("SELECT 1"))
        return db
    except Exception as exc:
        pytest.skip(f"无可用 PostgreSQL 做 RLS 真测: {type(exc).__name__}")


def _setup(db: StorageBackend) -> None:
    with db.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {_TABLE}"))
        conn.execute(text(f"CREATE TABLE {_TABLE} (tenant text NOT NULL, id text NOT NULL, PRIMARY KEY (tenant, id))"))
        conn.execute(text(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{_ROLE}') "
            f"THEN CREATE ROLE {_ROLE} NOSUPERUSER; END IF; END $$;"
        ))
        conn.execute(text(f"GRANT SELECT, INSERT ON {_TABLE} TO {_ROLE}"))
        conn.execute(text(
            f"INSERT INTO {_TABLE} (tenant, id) VALUES ('acme','a1'),('acme','a2'),('globex','g1')"
        ))
    enable_tenant_rls(db, _TABLE)


def _ids_as_role(db: StorageBackend, tenant: str) -> list[str]:
    """以非超级用户角色 + 租户上下文查全表 id(RLS 只放行本租户)。"""
    with db.begin() as conn:
        conn.execute(text(f"SET LOCAL ROLE {_ROLE}"))
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
        rows = conn.execute(text(f"SELECT id FROM {_TABLE} ORDER BY id")).fetchall()
    return [r.id for r in rows]


def _count_id_as_role(db: StorageBackend, tenant: str, target_id: str) -> int:
    """租户上下文下按 id 直查(证明 RLS 连点查也挡跨租户)。"""
    with db.begin() as conn:
        conn.execute(text(f"SET LOCAL ROLE {_ROLE}"))
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
        n = conn.execute(text(f"SELECT count(*) FROM {_TABLE} WHERE id = :id"), {"id": target_id}).scalar()
    return int(n)


def test_rls_enforces_tenant_isolation() -> None:
    db = _pg()
    try:
        _setup(db)
        assert _ids_as_role(db, "acme") == ["a1", "a2"]  # acme 只看到自己 2 行
        assert _ids_as_role(db, "globex") == ["g1"]  # globex 只看到自己 1 行
        # 跨租户硬挡:acme 上下文按 id 直查 globex 的 g1 → DB 层 0 行(即便应用层漏写 WHERE)
        assert _count_id_as_role(db, "acme", "g1") == 0
        assert _count_id_as_role(db, "globex", "g1") == 1  # globex 自己能查到
    finally:
        with db.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {_TABLE}"))
        db.dispose()
