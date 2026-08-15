"""规模部署的独立在线迁移进程入口。"""

from __future__ import annotations

import os

from agent_py_agent.agent.runtime_schema import apply_runtime_migrations
from agent_py_agent.agent.scale_runtime import ScaleRole, ScaleRuntimeConfig
from agent_py_agent.agent.storage_backend import StorageBackend


def run() -> int:
    config = ScaleRuntimeConfig.from_env(ScaleRole.MIGRATE, os.environ)
    url = config.migration_database_url if config.is_scale else config.database_url
    if not url:
        raise RuntimeError("迁移入口要求 DATABASE_MIGRATION_URL(scale) 或 DATABASE_URL(local)")
    backend = StorageBackend(url)
    try:
        applied = apply_runtime_migrations(
            backend,
            include_scale_data=config.is_scale,
            app_role=config.database_app_role,
        )
        print("runtime migrations applied=" + ",".join(str(item.version) for item in applied))
    finally:
        backend.dispose()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
