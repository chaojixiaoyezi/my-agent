"""ASGI 入站层(Tier 0.2 企业规模化):异步 FastAPI/uvicorn 两层架构的接入层。

研究确认 claw 两层架构(异步 ASGI 接入 + DB 队列 + 横向无状态 worker)是三家唯一能扩到单机外的形态。
本模块=**接入层**:飞书 webhook verify→decrypt→dedup→enqueue→**立即 ack**,**不内联跑 LLM**
(LLM 在独立 worker 消费队列跑,HTTP 永不被慢 LLM 阻塞)。整合已建地基:feishu_crypto + ingress_queue + metrics。

借库取舍(研究明确建议):ASGI 服务器(uvicorn)+ 路由/校验(FastAPI)自研不现实且高危 → 借库;
worker 池自建(stdlib + 队列)。须 ``scale`` extra(fastapi/uvicorn)。
含 liveness ``/healthz`` + readiness ``/readyz``(探 DB/队列,不就绪 503,滚动升级时避免路由到未就绪实例)
+ ``/metrics``(Prometheus,企业告警消费)。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from agent_py_agent.agent.adapter import feishu_crypto
from agent_py_agent.agent.graceful import DrainState
from agent_py_agent.agent.ingress_queue import IngressQueue, QueueBackpressure
from agent_py_agent.agent.observability.metrics import Counter, MetricsRegistry, default_registry
from agent_py_agent.agent.observability.tracing import (
    Span,
    child_context,
    inject,
    new_trace,
    parse_traceparent,
)

try:
    from fastapi import FastAPI, Request, Response

    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False


@dataclass(frozen=True)
class FeishuIngressConfig:
    encrypt_key: str = ""
    verification_token: str = ""
    tenant_id: str = ""


@dataclass(frozen=True)
class IngressAppRuntime:
    registry: MetricsRegistry | None = None
    drain: DrainState | None = None
    readiness_checks: Sequence[Callable[[], object]] = ()


@dataclass(frozen=True)
class TracedEnqueueRequest:
    request: Request
    event: dict
    queue: IngressQueue
    counter: Counter


def _verification_token_ok(outer: dict, expected: str) -> bool:
    """校验飞书事件里的 verification token(url_verification 在顶层、v2 事件在 header.token)。常数时间比。"""
    header = outer.get("header") if isinstance(outer.get("header"), dict) else {}
    token = str(outer.get("token") or header.get("token") or "")
    return bool(expected) and hmac.compare_digest(token, expected)


def _verify_and_decode(request: Request, body: bytes, config: FeishuIngressConfig) -> dict | None:
    """fail-closed 验签 + 解密 + 解析。未配置任何验证手段、或验签/验 token 失败 → 返回 None(拒绝)。"""
    if not config.encrypt_key and not config.verification_token:
        return None  # fail-closed:未配置 encrypt_key 也未配 verification_token → 不跑无验证的公网 webhook
    if config.encrypt_key:
        parts = feishu_crypto.FeishuSignParts(
            request.headers.get("X-Lark-Request-Timestamp", ""),
            request.headers.get("X-Lark-Request-Nonce", ""),
            config.encrypt_key,
            body,
        )
        if not feishu_crypto.verify_signature(parts, request.headers.get("X-Lark-Signature", "")):
            return None
    try:
        outer = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(outer, dict):
        return None
    if not config.encrypt_key and not _verification_token_ok(outer, config.verification_token):
        return None  # 仅 token 模式(无 encrypt_key):必须校验 body 里的 verification token
    if "encrypt" not in outer:
        return outer
    try:
        return json.loads(feishu_crypto.decrypt_event(config.encrypt_key, str(outer["encrypt"])))
    except Exception:
        return None


def _event_keys(inner: dict) -> tuple[str, str]:
    """从飞书事件取 (dedup_key, lane)。dedup=event_id;lane=会话维度(同会话串行)。"""
    header = inner.get("header") if isinstance(inner.get("header"), dict) else {}
    event = inner.get("event") if isinstance(inner.get("event"), dict) else {}
    msg = event.get("message") if isinstance(event.get("message"), dict) else {}
    event_id = str(header.get("event_id") or inner.get("uuid") or "")
    if not event_id:
        event_id = "feishu-" + hashlib.sha256(json.dumps(inner, sort_keys=True).encode()).hexdigest()[:32]
    lane = str(msg.get("chat_id") or event.get("sender") or "default")
    return event_id, lane


def _enqueue_event(queue: IngressQueue, inner: dict, events: Counter) -> Response:
    dedup_key, lane = _event_keys(inner)
    try:
        queue.enqueue(dedup_key, lane, inner)
    except QueueBackpressure:
        events.inc(labels={"result": "backpressure"})
        return Response('{"error":"busy"}', status_code=429, media_type="application/json")
    events.inc(labels={"result": "enqueued"})
    return Response('{"code":0}', media_type="application/json")  # 立即 ack,LLM 留 worker


def _enqueue_traced_event(item: TracedEnqueueRequest) -> Response:
    parent = parse_traceparent(item.request.headers.get("traceparent", "")) or new_trace()
    context = child_context(parent)
    inject(context, item.event)
    with Span("ingress.feishu.enqueue", context):
        return _enqueue_event(item.queue, item.event, item.counter)


def create_ingress_app(
    queue: IngressQueue,
    config: FeishuIngressConfig,
    runtime: IngressAppRuntime | None = None,
):
    """造异步入站 ASGI app(FastAPI)。queue=入站队列,config=飞书凭据,registry=指标,drain=退出漏排门。"""
    if not _HAS_FASTAPI:
        raise RuntimeError("ASGI 入站层需 fastapi/uvicorn:pip install 'my-agent[scale]'")
    deps = runtime or IngressAppRuntime()
    reg = deps.registry or default_registry()  # 默认用全局 registry,/metrics 同时暴露 agent_core 热路径指标(审计 #19)
    events = reg.counter("ingress_events_total", "入站事件总数(按结果标签)")
    app = FastAPI()

    @app.post("/api/im/feishu/events")
    async def feishu_events(request: Request) -> Response:  # noqa: ANN202
        inner = _verify_and_decode(request, await request.body(), config)
        if inner is None:
            events.inc(labels={"result": "rejected"})
            return Response('{"error":"signature"}', status_code=403, media_type="application/json")
        if "challenge" in inner:  # 飞书 URL 验证:原样回 challenge
            events.inc(labels={"result": "challenge"})
            return Response(json.dumps({"challenge": inner["challenge"]}), media_type="application/json")
        if config.tenant_id:
            inner["tenant"] = config.tenant_id  # 部署事实覆盖不可信 body，禁止事件伪造租户
        return _enqueue_traced_event(TracedEnqueueRequest(request, inner, queue, events))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:  # noqa: ANN202
        return {"status": "ok"}  # liveness:进程活着即 200

    @app.get("/readyz")
    async def readyz() -> Response:  # noqa: ANN202
        if deps.drain is not None and deps.drain.is_draining():  # 退出漏排:转 503 让 LB/Service 摘流量(零停机滚动)
            return Response('{"status":"draining"}', status_code=503, media_type="application/json")
        try:
            queue.stats()  # 探 DB/队列可达
            for check in deps.readiness_checks:
                check()
        except Exception:
            return Response('{"status":"not_ready"}', status_code=503, media_type="application/json")
        return Response('{"status":"ready"}', media_type="application/json")

    @app.get("/metrics")
    async def metrics() -> Response:  # noqa: ANN202
        return Response(reg.render(), media_type="text/plain; version=0.0.4")

    return app
