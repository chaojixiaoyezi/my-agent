"""Tier 0/1 两层架构端到端集成测试:POST 飞书事件 → ASGI 入站 → 队列 → worker 池 → handler。

铁证:HTTP 立即 ack(不等 LLM)、worker 异步消费、handler 收到原始事件。整条链路真跑(非 mock)。
"""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from agent_py_agent.agent.asgi_ingress import FeishuIngressConfig, create_ingress_app  # noqa: E402
from agent_py_agent.agent.ingress_queue import IngressQueue, QueueConfig  # noqa: E402
from agent_py_agent.agent.queue_worker import WorkerPool  # noqa: E402
from agent_py_agent.agent.storage_backend import StorageBackend  # noqa: E402

_VTOK = "e2e-verification-token"  # #4 fail-closed:webhook 须配验证 token,事件须带 token


def test_feishu_ingress_to_worker_end_to_end(tmp_path) -> None:
    # 文件 SQLite(WAL,worker 线程并发安全);ASGI app 与 worker 池共享同一队列
    queue = IngressQueue(StorageBackend.for_path(tmp_path / "q.db"), QueueConfig(lane_cap=50))
    queue.ensure_schema()

    processed: list[dict] = []
    done = threading.Event()

    def handler(payload: dict) -> None:
        processed.append(payload)
        done.set()

    pool = WorkerPool(queue, handler, workers=2)
    pool.start()
    try:
        client = TestClient(create_ingress_app(queue, FeishuIngressConfig(verification_token=_VTOK)))
        event = {"token": _VTOK, "header": {"event_id": "e2e-1"}, "event": {"message": {"chat_id": "c1", "content": "你好"}}}
        resp = client.post("/api/im/feishu/events", json=event)
        assert resp.status_code == 200  # HTTP 立即 ack(不内联跑 LLM)
        assert done.wait(5.0) is True  # worker 异步消费到
        assert processed[0]["header"]["event_id"] == "e2e-1"  # handler 收到原始飞书事件
    finally:
        pool.stop()
    assert queue.stats().get("completed") == 1  # 处理完落 completed


def test_backpressure_then_drains_through_worker(tmp_path) -> None:
    """背压后 worker 消费腾出容量,后续事件又能入队(队列+worker 协同不死锁)。"""
    queue = IngressQueue(StorageBackend.for_path(tmp_path / "q.db"), QueueConfig(lane_cap=1))
    queue.ensure_schema()
    gate = threading.Event()
    processed: list[dict] = []

    def handler(payload: dict) -> None:
        gate.wait(2.0)  # 卡住第一条,制造 lane 占用
        processed.append(payload)

    client = TestClient(create_ingress_app(queue, FeishuIngressConfig(verification_token=_VTOK)))
    assert client.post("/api/im/feishu/events", json={"token": _VTOK, "header": {"event_id": "a"}, "event": {"message": {"chat_id": "L"}}}).status_code == 200
    pool = WorkerPool(queue, handler, workers=1)
    pool.start()
    try:
        gate.set()  # 放行 → worker 处理完第一条、lane 腾空
        deadline_ok = False
        import time

        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            if queue.stats().get("completed", 0) >= 1:
                deadline_ok = True
                break
            time.sleep(0.05)
        assert deadline_ok  # 第一条被消费完
        # lane 腾空后新事件可入队
        assert client.post("/api/im/feishu/events", json={"token": _VTOK, "header": {"event_id": "b"}, "event": {"message": {"chat_id": "L"}}}).status_code == 200
    finally:
        pool.stop()
