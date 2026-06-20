"""ASGI 入站进程入口(Tier 5 部署):从环境变量装配入站 app,供 uvicorn / 容器起。

装配链:StorageBackend(DATABASE_URL,PG 规模/SQLite 本地)→ IngressQueue(ensure_schema)
→ create_ingress_app(+ DrainState 就绪门)。``python -m ...asgi_entry`` 走 serve():
装信号漏排后 uvicorn.run;容器 ENTRYPOINT tini 作 PID 1 转发 SIGTERM,触发就绪门转 503 优雅退出。

env:DATABASE_URL、FEISHU_ENCRYPT_KEY、FEISHU_VERIFICATION_TOKEN、PORT(默认 8080)。
"""

from __future__ import annotations

import os

from agent_py_agent.agent.asgi_ingress import FeishuIngressConfig, create_ingress_app
from agent_py_agent.agent.graceful import DrainState, install_sigterm_drain
from agent_py_agent.agent.ingress_queue import IngressQueue
from agent_py_agent.agent.storage_backend import StorageBackend, sqlite_url


def backend_from_env() -> StorageBackend:
    url = os.environ.get("DATABASE_URL") or sqlite_url(os.environ.get("INGRESS_DB", "/tmp/my_agent_ingress.db"))
    return StorageBackend(url)


def build_app(backend: StorageBackend | None = None, drain: DrainState | None = None):
    """装配入站 ASGI app。返回 (app, drain)。backend 可注入做测试(默认从 env)。"""
    backend = backend or backend_from_env()
    queue = IngressQueue(backend)
    queue.ensure_schema()
    config = FeishuIngressConfig(
        encrypt_key=os.environ.get("FEISHU_ENCRYPT_KEY", ""),
        verification_token=os.environ.get("FEISHU_VERIFICATION_TOKEN", ""),
    )
    drain = drain or DrainState()
    return create_ingress_app(queue, config, drain=drain), drain


def serve() -> None:  # pragma: no cover - 真进程入口(容器内跑,单测不起 uvicorn)
    import uvicorn

    app, drain = build_app()
    install_sigterm_drain(drain)  # SIGTERM → 就绪门转 503 → 摘流量优雅退出
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":  # pragma: no cover
    serve()
