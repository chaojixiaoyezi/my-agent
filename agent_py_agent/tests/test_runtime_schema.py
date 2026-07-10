from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")

from agent_py_agent.agent.migrations import MigrationError  # noqa: E402
from agent_py_agent.agent.runtime_schema import (  # noqa: E402
    apply_runtime_migrations,
    require_runtime_schema_current,
)
from agent_py_agent.agent.storage_backend import StorageBackend  # noqa: E402


def test_scale_schema_is_migration_owned_and_readiness_is_read_only(tmp_path) -> None:
    db = StorageBackend.for_path(tmp_path / "runtime.db")
    with pytest.raises(MigrationError):
        require_runtime_schema_current(db)
    assert db.table_names() == []
    assert [item.version for item in apply_runtime_migrations(db)] == [1, 2, 3]
    require_runtime_schema_current(db)
    assert "ingress_messages" in db.table_names()
    assert "my_agent_schema_migrations" in db.table_names()


def test_runtime_migrations_upgrade_legacy_ingress_table(tmp_path) -> None:
    db = StorageBackend.for_path(tmp_path / "legacy.db")
    with db.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE ingress_messages ("
            "id INTEGER PRIMARY KEY, dedup_key TEXT NOT NULL UNIQUE, lane TEXT NOT NULL, "
            "payload TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL, "
            "claim_token TEXT, lease_until BIGINT, created_at BIGINT NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO ingress_messages VALUES "
            "(1, 'd1', 'lane', '{}', 'pending', 0, NULL, NULL, 1)"
        )
    apply_runtime_migrations(db)
    with db.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT dedup_key, next_visible_at, last_error FROM ingress_messages WHERE id=1"
        ).one()
    assert tuple(row) == ("d1", 0, None)
