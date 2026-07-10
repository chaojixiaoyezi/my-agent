"""ASGI 入站进程入口(Tier 5 部署):从环境变量装配入站 app,供 uvicorn / 容器起。

装配链:StorageBackend(DATABASE_URL,PG 规模/SQLite 本地)→ IngressQueue(ensure_schema)
→ create_ingress_app(+ DrainState 就绪门)。``python -m ...asgi_entry`` 走 serve():
装信号漏排后 uvicorn.run;容器 ENTRYPOINT tini 作 PID 1 转发 SIGTERM,触发就绪门转 503 优雅退出。

env:DATABASE_URL、FEISHU_ENCRYPT_KEY、FEISHU_VERIFICATION_TOKEN、PORT(默认 8080)。
"""

from __future__ import annotations

import os

from agent_py_agent.agent.asgi_ingress import (
    FeishuIngressConfig,
    IngressAppRuntime,
    create_ingress_app,
)
from agent_py_agent.agent.graceful import DrainState, install_sigterm_drain
from agent_py_agent.agent.ingress_queue import IngressQueue
from agent_py_agent.agent.llm_scale import redis_client_from_url
from agent_py_agent.agent.observability.otel import configure_otel_from_env
from agent_py_agent.agent.pg_rls import require_restricted_app_role
from agent_py_agent.agent.scale_runtime import ScaleRole, ScaleRuntimeConfig
from agent_py_agent.agent.storage_backend import StorageBackend, sqlite_url


def env_int(name: str, default: int) -> int:
    """从环境变量读整数,缺失/非法用默认(防运维误配空格/非数字值打挂入口进程,审计 #24)。"""
    try:
        return int(os.environ[name])
    except (KeyError, ValueError, TypeError):
        return default


def env_float(name: str, default: float) -> float:
    """从环境变量读浮点,缺失/非法用默认。"""
    try:
        return float(os.environ[name])
    except (KeyError, ValueError, TypeError):
        return default


def backend_from_env() -> StorageBackend:
    config = ScaleRuntimeConfig.from_env(ScaleRole.INGRESS, os.environ)
    url = config.database_url or sqlite_url(os.environ.get("INGRESS_DB", "/tmp/my_agent_ingress.db"))
    return StorageBackend(url)


def build_app(backend: StorageBackend | None = None, drain: DrainState | None = None):
    """装配入站 ASGI app。返回 (app, drain)。backend 可注入做测试(默认从 env)。"""
    runtime = ScaleRuntimeConfig.from_env(ScaleRole.INGRESS, os.environ)
    backend = backend or backend_from_env()
    queue = IngressQueue(backend)
    if runtime.is_scale:
        from agent_py_agent.agent.runtime_schema import require_runtime_schema_current

        require_restricted_app_role(backend, runtime.database_app_role)
        require_runtime_schema_current(
            backend,
            include_scale_data=True,
            app_role=runtime.database_app_role,
        )
    else:
        queue.ensure_schema()
    config = FeishuIngressConfig(
        encrypt_key=os.environ.get("FEISHU_ENCRYPT_KEY", ""),
        verification_token=os.environ.get("FEISHU_VERIFICATION_TOKEN", ""),
        tenant_id=runtime.tenant_id,
    )
    drain = drain or DrainState()
    readiness_checks = ()
    if runtime.is_scale:
        redis_client = redis_client_from_url(runtime.redis_url)
        readiness_checks = (redis_client.ping,)
    app = create_ingress_app(
        queue,
        config,
        IngressAppRuntime(drain=drain, readiness_checks=readiness_checks),
    )
    configure_otel_from_env(
        service_name="my-agent-ingress",
        env=os.environ,
        app=app,
        required=runtime.is_scale,
    )
    return app, drain


def serve() -> None:  # pragma: no cover - 真进程入口(容器内跑,单测不起 uvicorn)
    import uvicorn

    from agent_py_agent.agent.common.thread_hooks import install_thread_excepthook

    install_thread_excepthook()  # 后台线程未捕获异常落日志可告警,不静默死(审计 #19)
    app, drain = build_app()
    install_sigterm_drain(drain)  # SIGTERM → 就绪门转 503 → 摘流量优雅退出
    uvicorn.run(app, host="0.0.0.0", port=env_int("PORT", 8080))


if __name__ == "__main__":  # pragma: no cover
    serve()
