"""RLS 强制隔离的正式租户运行状态仓储。"""

from __future__ import annotations

import json
import time
from typing import Any

from agent_py_agent.agent.pg_rls import tenant_session
from agent_py_agent.agent.storage_backend import StorageBackend

try:
    from sqlalchemy import text
except ImportError:  # pragma: no cover
    text = None


class TenantRuntimeStateStore:
    """所有读写都先设置事务级 ``app.tenant_id``；SQL 即使漏 tenant WHERE 也由 RLS 拦。"""

    def __init__(self, backend: StorageBackend) -> None:
        if not backend.is_postgres:
            raise ValueError("TenantRuntimeStateStore 只在正式 PostgreSQL/RLS 路径使用")
        self._backend = backend

    def put(self, tenant: str, key: str, payload: dict[str, Any]) -> None:
        if not tenant or not key:
            raise ValueError("tenant 和 key 必填")
        with tenant_session(self._backend, tenant) as conn:
            conn.execute(
                text(
                    "INSERT INTO tenant_runtime_state (tenant, state_key, payload, updated_at) "
                    "VALUES (:tenant, :key, :payload, :updated_at) "
                    "ON CONFLICT (tenant, state_key) DO UPDATE SET "
                    "payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at"
                ),
                {
                    "tenant": tenant,
                    "key": key,
                    "payload": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    "updated_at": int(time.time() * 1000),
                },
            )

    def get(self, tenant: str, key: str) -> dict[str, Any] | None:
        with tenant_session(self._backend, tenant) as conn:
            row = conn.execute(
                text("SELECT payload FROM tenant_runtime_state WHERE state_key = :key"),
                {"key": key},
            ).one_or_none()
        if row is None:
            return None
        value = json.loads(str(row.payload))
        return value if isinstance(value, dict) else None


__all__ = ["TenantRuntimeStateStore"]
