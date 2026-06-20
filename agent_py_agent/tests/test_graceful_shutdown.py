"""Tier 5 优雅退出 + 部署装配测试:DrainState/SIGTERM 漏排、readyz 漏排转 503、入口装配。"""

from __future__ import annotations

import signal

import pytest

pytest.importorskip("sqlalchemy")

from agent_py_agent.agent.graceful import DrainState, install_sigterm_drain  # noqa: E402
from agent_py_agent.agent.storage_backend import StorageBackend  # noqa: E402


def test_drain_state_transitions() -> None:
    d = DrainState()
    assert d.is_draining() is False
    assert d.wait(0.01) is False
    d.start_draining()
    assert d.is_draining() is True
    assert d.wait(0.01) is True


def test_install_sigterm_drain_registers_and_flips() -> None:
    d = DrainState()
    calls: list[int] = []
    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)
    try:
        install_sigterm_drain(d, on_drain=lambda: calls.append(1))
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)  # 直接调 handler 验逻辑(不真发信号,免误杀测试进程)
        assert d.is_draining() is True
        assert calls == [1]  # on_drain 回调触发(如 pool.stop)
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def test_readyz_returns_503_when_draining() -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from agent_py_agent.agent.asgi_ingress import FeishuIngressConfig, create_ingress_app
    from agent_py_agent.agent.ingress_queue import IngressQueue, QueueConfig

    queue = IngressQueue(StorageBackend.in_memory(), QueueConfig())
    queue.ensure_schema()
    drain = DrainState()
    client = TestClient(create_ingress_app(queue, FeishuIngressConfig(), drain=drain))
    assert client.get("/readyz").status_code == 200  # 就绪接流量
    drain.start_draining()
    assert client.get("/readyz").status_code == 503  # 漏排 → 摘流量(零停机滚动)
    assert client.get("/healthz").status_code == 200  # liveness 仍 200(退出中别被当死重启)


def test_build_app_assembles_ingress() -> None:
    pytest.importorskip("fastapi")
    from agent_py_agent.agent.asgi_entry import build_app

    app, drain = build_app(backend=StorageBackend.in_memory())
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/im/feishu/events" in paths and "/readyz" in paths
    assert drain.is_draining() is False


def test_build_pool_assembles_worker() -> None:
    from agent_py_agent.agent.worker_entry import build_pool

    seen: list[dict] = []
    pool, queue = build_pool(seen.append, backend=StorageBackend.in_memory())
    assert pool is not None
    assert queue.stats().get("pending", 0) == 0  # 空队列起步
