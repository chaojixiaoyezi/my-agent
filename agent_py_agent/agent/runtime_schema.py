"""正式运行库 schema 注册表与 expand-contract 部署入口。"""

from __future__ import annotations

from agent_py_agent.agent.migrations import Migration, MigrationRunner
from agent_py_agent.agent.storage_backend import StorageBackend

try:
    from sqlalchemy import inspect, text
except ImportError:  # pragma: no cover - 构造 StorageBackend 时已有清晰错误
    inspect = text = None

_VERSION_TABLE = "my_agent_schema_migrations"


def _create_ingress_table(conn) -> None:
    from agent_py_agent.agent.ingress_queue import _messages_table

    table = _messages_table(__import__("sqlalchemy").MetaData())
    table.create(conn, checkfirst=True)


def _expand_ingress_columns(conn) -> None:
    existing = {str(col["name"]) for col in inspect(conn).get_columns("ingress_messages")}
    additions = {
        "next_visible_at": "BIGINT NOT NULL DEFAULT 0",
        "last_error": "TEXT",
    }
    for name, definition in additions.items():
        if name not in existing:
            conn.execute(text(f"ALTER TABLE ingress_messages ADD COLUMN {name} {definition}"))


def _create_claim_index(conn) -> None:
    dialect = conn.dialect.name
    concurrently = " CONCURRENTLY" if dialect == "postgresql" else ""
    conn.execute(
        text(
            f"CREATE INDEX{concurrently} IF NOT EXISTS ix_ingress_status_visible "
            "ON ingress_messages (status, next_visible_at, created_at)"
        )
    )


def _create_tenant_runtime_state(conn) -> None:
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS tenant_runtime_state ("
            "tenant TEXT NOT NULL, state_key TEXT NOT NULL, payload TEXT NOT NULL, "
            "updated_at BIGINT NOT NULL, PRIMARY KEY (tenant, state_key))"
        )
    )


def _expand_release_channel(conn) -> None:
    existing = {str(col["name"]) for col in inspect(conn).get_columns("ingress_messages")}
    if "release_channel" not in existing:
        conn.execute(
            text(
                "ALTER TABLE ingress_messages ADD COLUMN release_channel "
                "VARCHAR(20) NOT NULL DEFAULT 'stable'"
            )
        )


def _create_release_claim_index(conn) -> None:
    concurrently = " CONCURRENTLY" if conn.dialect.name == "postgresql" else ""
    conn.execute(
        text(
            f"CREATE INDEX{concurrently} IF NOT EXISTS ix_ingress_release_status_visible "
            "ON ingress_messages (release_channel, status, next_visible_at, created_at)"
        )
    )


def _create_owner_file_manifest(conn) -> None:
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS owner_file_manifest ("
            "tenant TEXT NOT NULL, owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, "
            "relative_path TEXT NOT NULL, object_key TEXT NOT NULL, content_sha256 TEXT NOT NULL, "
            "size_bytes BIGINT NOT NULL, file_mode INTEGER NOT NULL, updated_at BIGINT NOT NULL, "
            "PRIMARY KEY (tenant, owner_kind, owner_id, relative_path))"
        )
    )


def _secure_owner_file_manifest(conn, app_role: str) -> None:
    if not app_role or not app_role.replace("_", "").isalnum():
        raise ValueError("DATABASE_APP_ROLE 非法")
    policy = "owner_file_manifest_tenant_isolation"
    predicate = "tenant = current_setting('app.tenant_id', true)"
    conn.execute(text("ALTER TABLE owner_file_manifest ENABLE ROW LEVEL SECURITY"))
    conn.execute(text("ALTER TABLE owner_file_manifest FORCE ROW LEVEL SECURITY"))
    conn.execute(text(f"DROP POLICY IF EXISTS {policy} ON owner_file_manifest"))
    conn.execute(
        text(
            f"CREATE POLICY {policy} ON owner_file_manifest "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )
    conn.execute(text("REVOKE ALL ON owner_file_manifest FROM PUBLIC"))
    conn.execute(
        text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON owner_file_manifest TO {app_role}")
    )


def _enable_tenant_runtime_rls(conn, app_role: str) -> None:
    if not app_role or not app_role.replace("_", "").isalnum():
        raise ValueError("DATABASE_APP_ROLE 非法")
    policy = "tenant_runtime_state_tenant_isolation"
    predicate = "tenant = current_setting('app.tenant_id', true)"
    conn.execute(text("ALTER TABLE tenant_runtime_state ENABLE ROW LEVEL SECURITY"))
    conn.execute(text("ALTER TABLE tenant_runtime_state FORCE ROW LEVEL SECURITY"))
    conn.execute(text(f"DROP POLICY IF EXISTS {policy} ON tenant_runtime_state"))
    conn.execute(
        text(
            f"CREATE POLICY {policy} ON tenant_runtime_state "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )
    conn.execute(text("REVOKE ALL ON tenant_runtime_state FROM PUBLIC"))
    conn.execute(
        text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_runtime_state TO {app_role}")
    )


def _grant_scale_application_schema(conn, app_role: str) -> None:
    if not app_role or not app_role.replace("_", "").isalnum():
        raise ValueError("DATABASE_APP_ROLE 非法")
    conn.execute(
        text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ingress_messages TO {app_role}")
    )
    conn.execute(
        text(f"GRANT USAGE, SELECT ON SEQUENCE ingress_messages_id_seq TO {app_role}")
    )
    conn.execute(text(f"GRANT SELECT ON my_agent_schema_migrations TO {app_role}"))


def runtime_migrations(
    backend: StorageBackend,
    *,
    include_scale_data: bool = False,
    app_role: str = "",
) -> tuple[Migration, ...]:
    base = (
        Migration(1, "expand_create_ingress_table", _create_ingress_table),
        Migration(2, "expand_ingress_retry_columns", _expand_ingress_columns),
        Migration(
            3,
            "expand_ingress_claim_index",
            _create_claim_index,
            transactional=not backend.is_postgres,
        ),
    )
    if not include_scale_data:
        return base
    return base + (
        Migration(4, "expand_tenant_runtime_state", _create_tenant_runtime_state),
        Migration(5, "secure_tenant_runtime_state_rls", lambda conn: _enable_tenant_runtime_rls(conn, app_role)),
        Migration(6, "grant_scale_application_schema", lambda conn: _grant_scale_application_schema(conn, app_role)),
        Migration(7, "expand_release_channel", _expand_release_channel),
        Migration(
            8,
            "expand_release_claim_index",
            _create_release_claim_index,
            transactional=not backend.is_postgres,
        ),
        Migration(9, "expand_owner_file_manifest", _create_owner_file_manifest),
        Migration(
            10,
            "secure_owner_file_manifest_rls",
            lambda conn: _secure_owner_file_manifest(conn, app_role),
        ),
    )


def runtime_migration_runner(
    backend: StorageBackend,
    *,
    include_scale_data: bool = False,
    app_role: str = "",
) -> MigrationRunner:
    return MigrationRunner(
        backend,
        runtime_migrations(backend, include_scale_data=include_scale_data, app_role=app_role),
        table_name=_VERSION_TABLE,
    )


def apply_runtime_migrations(
    backend: StorageBackend,
    *,
    include_scale_data: bool = False,
    app_role: str = "",
) -> list[Migration]:
    """供独立 migration Job 调用；应用 Pod 不调用。"""
    applied = runtime_migration_runner(
        backend,
        include_scale_data=include_scale_data,
        app_role=app_role,
    ).apply_pending()
    _require_physical_schema(backend, include_scale_data=include_scale_data)
    return applied


def require_runtime_schema_current(
    backend: StorageBackend,
    *,
    include_scale_data: bool = False,
    app_role: str = "",
) -> None:
    """规模应用启动只读验版本；待迁移时 fail-closed，由 migration Job 先升级。"""
    runtime_migration_runner(
        backend,
        include_scale_data=include_scale_data,
        app_role=app_role,
    ).require_current()
    _require_physical_schema(backend, include_scale_data=include_scale_data)


def _require_physical_schema(backend: StorageBackend, *, include_scale_data: bool = False) -> None:
    inspector = inspect(backend.engine)
    if "ingress_messages" not in inspector.get_table_names():
        raise RuntimeError("数据库迁移版本与物理 schema 漂移: 缺 ingress_messages")
    columns = {str(item["name"]) for item in inspector.get_columns("ingress_messages")}
    missing = {"next_visible_at", "last_error"} - columns
    if missing:
        raise RuntimeError("数据库迁移版本与物理 schema 漂移: 缺列 " + ",".join(sorted(missing)))
    indexes = {str(item["name"]) for item in inspector.get_indexes("ingress_messages")}
    if "ix_ingress_status_visible" not in indexes:
        raise RuntimeError("数据库迁移版本与物理 schema 漂移: 缺 ix_ingress_status_visible")
    if include_scale_data:
        _require_scale_physical_schema(backend, inspector)


def _require_scale_physical_schema(backend: StorageBackend, inspector) -> None:
    if "tenant_runtime_state" not in inspector.get_table_names():
        raise RuntimeError("数据库迁移版本与物理 schema 漂移: 缺 tenant_runtime_state")
    if "owner_file_manifest" not in inspector.get_table_names():
        raise RuntimeError("数据库迁移版本与物理 schema 漂移: 缺 owner_file_manifest")
    ingress_columns = {str(item["name"]) for item in inspector.get_columns("ingress_messages")}
    if "release_channel" not in ingress_columns:
        raise RuntimeError("数据库迁移版本与物理 schema 漂移: 缺 release_channel")
    ingress_indexes = {str(item["name"]) for item in inspector.get_indexes("ingress_messages")}
    if "ix_ingress_release_status_visible" not in ingress_indexes:
        raise RuntimeError("数据库迁移版本与物理 schema 漂移: 缺 ix_ingress_release_status_visible")
    if not backend.is_postgres:
        return
    with backend.connect() as conn:
        flags = conn.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
                "WHERE c.oid IN ('tenant_runtime_state'::regclass, 'owner_file_manifest'::regclass)"
            )
        ).all()
    if len(flags) != 2 or any(not bool(row.relrowsecurity) or not bool(row.relforcerowsecurity) for row in flags):
        raise RuntimeError("scale 租户表未同时 ENABLE/FORCE RLS")
    _require_scale_privileges(backend)


def _require_scale_privileges(backend: StorageBackend) -> None:
    with backend.connect() as conn:
        privileges = conn.execute(
            text(
                "SELECT "
                "has_table_privilege(current_user, 'ingress_messages', 'SELECT,INSERT,UPDATE,DELETE') AS queue_ok, "
                "has_sequence_privilege(current_user, 'ingress_messages_id_seq', 'USAGE,SELECT') AS sequence_ok, "
                "has_table_privilege(current_user, 'tenant_runtime_state', 'SELECT,INSERT,UPDATE,DELETE') AS tenant_ok, "
                "has_table_privilege(current_user, 'owner_file_manifest', 'SELECT,INSERT,UPDATE,DELETE') AS owner_ok, "
                "has_table_privilege(current_user, 'my_agent_schema_migrations', 'SELECT') AS version_ok"
            )
        ).one()
    if not all(
        (
            privileges.queue_ok,
            privileges.sequence_ok,
            privileges.tenant_ok,
            privileges.owner_ok,
            privileges.version_ok,
        )
    ):
        raise RuntimeError("当前应用角色缺少 scale schema 必需权限")


__all__ = [
    "apply_runtime_migrations",
    "require_runtime_schema_current",
    "runtime_migration_runner",
    "runtime_migrations",
]
