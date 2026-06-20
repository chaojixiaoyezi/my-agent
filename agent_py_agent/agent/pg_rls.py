"""PostgreSQL 行级安全 RLS 多租户硬隔离(Tier 2 加固):在 DB 层强制 tenant 过滤。

研究核验三家都没做 RLS:claw 用 owner 列(无 RLS)、长期助手 靠外部 user_id、通道运行时 用目录/独立文件。
RLS 是企业级数据隔离的硬通货——即便应用层漏写 ``WHERE tenant``,DB 也按策略
``tenant = current_setting('app.tenant_id')`` 挡掉越权行,把"租户隔离"从"靠每个查询自觉"升级成
"DB 强制保证"。``FORCE ROW LEVEL SECURITY`` 让表属主也受约束(仅超级用户/BYPASSRLS 角色绕过)。

连接池要点:租户上下文必须用 ``set_config(..., is_local=true)``(事务级 GUC),不能用会话级
``SET``——否则池里复用连接会把上个租户的 GUC 带给下个请求,造成跨租户泄漏。故每次访问都在事务里
设 GUC,事务结束自动复位。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from agent_py_agent.agent.storage_backend import StorageBackend

try:
    from sqlalchemy import text

    _HAS_SQLALCHEMY = True
except ImportError:
    _HAS_SQLALCHEMY = False


def _safe_ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"标识符只允许字母数字下划线(防注入): {name!r}")
    return name


def enable_tenant_rls(backend: StorageBackend, table: str, *, tenant_column: str = "tenant") -> None:
    """对 table 开启并 FORCE RLS,建按 app.tenant_id GUC 隔离的策略(幂等)。需 PostgreSQL。"""
    if not backend.is_postgres:
        raise ValueError("RLS 是 PostgreSQL 能力;SQLite 无行级安全")
    tbl = _safe_ident(table)
    col = _safe_ident(tenant_column)
    policy = f"{tbl}_tenant_isolation"
    predicate = f"{col} = current_setting('app.tenant_id', true)"
    with backend.begin() as conn:
        conn.execute(text(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY"))
        conn.execute(text(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY"))
        conn.execute(text(f"DROP POLICY IF EXISTS {policy} ON {tbl}"))
        conn.execute(text(f"CREATE POLICY {policy} ON {tbl} USING ({predicate}) WITH CHECK ({predicate})"))


@contextmanager
def tenant_session(backend: StorageBackend, tenant: str) -> Iterator[object]:
    """开事务并 SET LOCAL app.tenant_id=tenant;RLS 策略据此只放行本租户行。出作用域自动复位。

    用法::

        with tenant_session(backend, "acme") as conn:
            conn.execute(text("SELECT ... FROM vector_memory"))  # 只看到 acme 的行(DB 强制)
    """
    with backend.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
        yield conn
