from __future__ import annotations

import pytest

from agent_py_agent.agent.scale_runtime import (
    ScaleConfigurationError,
    ScaleRole,
    ScaleRuntimeConfig,
)


def _scale_env() -> dict[str, str]:
    return {
        "MY_AGENT_DEPLOYMENT_MODE": "scale",
        "DATABASE_URL": "postgresql+psycopg://app@db/my_agent",
        "DATABASE_MIGRATION_URL": "postgresql+psycopg://migrator@db/my_agent",
        "DATABASE_APP_ROLE": "my_agent_app",
        "REDIS_URL": "rediss://redis/0",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4318",
        "WORKER_HANDLER": "agent_py_agent.agent.worker_handler:handle",
        "WORKER_DOWNSTREAM": "agent_py_agent.agent.scale_downstream:run",
        "MY_AGENT_TENANT_ID": "acme",
        "MY_AGENT_HOME": "/data/my-agent",
        "MY_AGENT_SCALE_WORKSPACE_ROOT": "/data/workspaces",
        "FEISHU_APP_ID": "cli_a",
        "FEISHU_APP_SECRET": "secret",
        "FEISHU_VERIFICATION_TOKEN": "configured",
    }


def test_local_mode_keeps_developer_fallbacks() -> None:
    cfg = ScaleRuntimeConfig.from_env(ScaleRole.INGRESS, {})
    assert cfg.is_scale is False


@pytest.mark.parametrize("role", list(ScaleRole))
def test_complete_scale_profile_is_accepted(role: ScaleRole) -> None:
    cfg = ScaleRuntimeConfig.from_env(role, _scale_env())
    assert cfg.is_scale is True


def test_scale_ingress_rejects_sqlite_fallback() -> None:
    env = _scale_env()
    env["DATABASE_URL"] = "sqlite:////tmp/ingress.db"
    with pytest.raises(ScaleConfigurationError, match="禁止 SQLite 回退"):
        ScaleRuntimeConfig.from_env(ScaleRole.INGRESS, env)


@pytest.mark.parametrize("missing", ["REDIS_URL", "OTEL_EXPORTER_OTLP_ENDPOINT"])
def test_scale_worker_requires_shared_state_and_export(missing: str) -> None:
    env = _scale_env()
    del env[missing]
    with pytest.raises(ScaleConfigurationError):
        ScaleRuntimeConfig.from_env(ScaleRole.WORKER, env)


def test_scale_worker_rejects_implicit_stub() -> None:
    env = _scale_env()
    env["WORKER_HANDLER"] = ""
    with pytest.raises(ScaleConfigurationError, match="禁止 stub"):
        ScaleRuntimeConfig.from_env(ScaleRole.WORKER, env)


def test_migration_role_requires_separate_migration_url() -> None:
    env = _scale_env()
    env["DATABASE_MIGRATION_URL"] = ""
    with pytest.raises(ScaleConfigurationError, match="DATABASE_MIGRATION_URL"):
        ScaleRuntimeConfig.from_env(ScaleRole.MIGRATE, env)
