"""Tier 0.2 ASGI 入站层测试:用 FastAPI TestClient 真跑 ASGI 请求(真实测试,非 mock)。

覆盖:健康/就绪/metrics 端点、飞书 challenge 回显、明文事件入队+去重、加密事件验签解密入队、
坏签 fail-closed、per-lane 背压 429。整合验证 feishu_crypto + ingress_queue + metrics。
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from agent_py_agent.agent.adapter import feishu_crypto  # noqa: E402
from agent_py_agent.agent.asgi_ingress import FeishuIngressConfig, create_ingress_app  # noqa: E402
from agent_py_agent.agent.ingress_queue import IngressQueue, QueueConfig  # noqa: E402
from agent_py_agent.agent.storage_backend import StorageBackend  # noqa: E402


_VTOKEN = "test-verification-token"  # #4 fail-closed:默认配 verification_token,事件须带 token


def _app(config: FeishuIngressConfig | None = None, qcfg: QueueConfig | None = None):
    queue = IngressQueue(StorageBackend.in_memory(), qcfg or QueueConfig(lane_cap=2))
    queue.ensure_schema()
    return TestClient(create_ingress_app(queue, config or FeishuIngressConfig(verification_token=_VTOKEN))), queue


def test_healthz_liveness() -> None:
    client, _ = _app()
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_readyz_checks_queue() -> None:
    client, _ = _app()
    assert client.get("/readyz").status_code == 200


def test_metrics_prometheus() -> None:
    client, _ = _app()
    r = client.get("/metrics")
    assert r.status_code == 200 and "ingress_events_total" in r.text


def test_feishu_challenge_echo() -> None:
    client, _ = _app()
    r = client.post("/api/im/feishu/events", json={"type": "url_verification", "challenge": "abc123", "token": _VTOKEN})
    assert r.status_code == 200 and r.json()["challenge"] == "abc123"


def test_feishu_plaintext_event_enqueued() -> None:
    client, queue = _app()
    body = {"token": _VTOKEN, "header": {"event_id": "evt-1"}, "event": {"message": {"chat_id": "c1"}}}
    assert client.post("/api/im/feishu/events", json=body).status_code == 200
    assert queue.stats().get("pending") == 1  # 入队但 HTTP 立即 ack(不内联 LLM)


def test_feishu_dedup_same_event_id() -> None:
    client, queue = _app()
    body = {"token": _VTOKEN, "header": {"event_id": "evt-dup"}, "event": {}}
    client.post("/api/im/feishu/events", json=body)
    client.post("/api/im/feishu/events", json=body)  # 同 event_id → 墓碑去重
    assert queue.stats().get("pending") == 1


def test_unconfigured_webhook_rejects_all() -> None:
    # #4 fail-closed:既无 encrypt_key 也无 verification_token → 拒绝一切事件(不跑无验证公网 webhook)
    client, queue = _app(FeishuIngressConfig())
    body = {"header": {"event_id": "x"}, "event": {"message": {"chat_id": "c"}}}
    assert client.post("/api/im/feishu/events", json=body).status_code == 403
    assert queue.stats().get("pending", 0) == 0


def test_forged_or_missing_token_rejected() -> None:
    # #4:配了 verification_token,但事件 token 错/缺 → 403(伪造事件被拦)
    client, queue = _app()
    assert client.post("/api/im/feishu/events", json={"header": {"event_id": "f"}, "token": "wrong"}).status_code == 403
    assert client.post("/api/im/feishu/events", json={"header": {"event_id": "g"}, "event": {}}).status_code == 403
    assert queue.stats().get("pending", 0) == 0


def test_feishu_encrypted_event_verified_and_enqueued() -> None:
    key = "my-feishu-encrypt-key"
    client, queue = _app(FeishuIngressConfig(encrypt_key=key))
    inner = {"header": {"event_id": "enc-1"}, "event": {"message": {"chat_id": "c2"}}}
    encrypted = feishu_crypto.encrypt_event_for_test(key, json.dumps(inner).encode(), iv=b"0123456789abcdef")
    raw = json.dumps({"encrypt": encrypted}).encode()
    ts, nonce = "1700000000", "nonce1"
    sig = feishu_crypto.compute_signature(feishu_crypto.FeishuSignParts(ts, nonce, key, raw))
    r = client.post(
        "/api/im/feishu/events",
        content=raw,
        headers={
            "X-Lark-Request-Timestamp": ts,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": sig,
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 200
    assert queue.stats().get("pending") == 1  # 解密后真入队


def test_feishu_bad_signature_rejected() -> None:
    client, queue = _app(FeishuIngressConfig(encrypt_key="k"))
    r = client.post(
        "/api/im/feishu/events",
        content=b'{"encrypt":"x"}',
        headers={"X-Lark-Signature": "bad", "X-Lark-Request-Timestamp": "t", "X-Lark-Request-Nonce": "n"},
    )
    assert r.status_code == 403  # fail-closed
    assert queue.stats().get("pending", 0) == 0


def test_per_lane_backpressure_returns_429() -> None:
    client, _ = _app(qcfg=QueueConfig(lane_cap=1))

    def evt(i: int) -> dict:
        return {"token": _VTOKEN, "header": {"event_id": f"e{i}"}, "event": {"message": {"chat_id": "hot"}}}

    assert client.post("/api/im/feishu/events", json=evt(0)).status_code == 200
    assert client.post("/api/im/feishu/events", json=evt(1)).status_code == 429  # 同 lane 满 → 429 让平台重投
