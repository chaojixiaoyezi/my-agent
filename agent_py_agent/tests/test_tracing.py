"""Tier 4.2 分布式追踪测试:traceparent 生成/解析/传播 + Span 计时 + 真队列跨层关联。

铁证:trace context 经真实持久化队列(enqueue→claim)传播后,worker 侧能取回同一 trace_id 建子 span。
"""

from __future__ import annotations

from agent_py_agent.agent.observability.tracing import (
    Span,
    child_context,
    extract,
    inject,
    new_trace,
    parse_traceparent,
)


class _SeqIds:
    """确定性 id 生成器:按调用顺序发可预测 hex(填到要求长度)。"""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self, n_bytes: int) -> str:
        self.n += 1
        return str(self.n).rjust(n_bytes * 2, "0")


def test_new_trace_traceparent_format() -> None:
    ctx = new_trace(_SeqIds())
    assert ctx.trace_id == "0" * 31 + "1"  # 16 字节 = 32 hex
    assert ctx.span_id == "0" * 15 + "2"  # 8 字节 = 16 hex
    assert ctx.traceparent == f"00-{ctx.trace_id}-{ctx.span_id}-01"  # W3C 格式,采样位 01


def test_parse_roundtrip() -> None:
    ctx = new_trace(_SeqIds())
    parsed = parse_traceparent(ctx.traceparent)
    assert parsed == ctx  # 往返一致


def test_parse_rejects_malformed_and_zero() -> None:
    assert parse_traceparent("garbage") is None
    assert parse_traceparent("00-tooshort-abc-01") is None
    assert parse_traceparent(f"00-{'0' * 32}-{'0' * 16}-01") is None  # 全零非法


def test_child_context_same_trace_new_span() -> None:
    gen = _SeqIds()
    root = new_trace(gen)
    child = child_context(root, gen)
    assert child.trace_id == root.trace_id  # 同一条 trace
    assert child.span_id != root.span_id  # 新 span
    assert child.sampled == root.sampled


def test_inject_extract_through_dict() -> None:
    ctx = new_trace(_SeqIds())
    carrier: dict = {}
    inject(ctx, carrier)
    assert carrier["traceparent"] == ctx.traceparent
    assert extract(carrier) == ctx
    assert extract({}) is None  # 缺失返回 None


def test_span_times_operation() -> None:
    ticks = [1_000_000, 5_000_000]  # 注入时钟:start=1ms, end=5ms
    captured = []
    with Span("llm_call", new_trace(_SeqIds()), clock=lambda: ticks.pop(0), on_end=captured.append):
        pass
    assert len(captured) == 1
    assert captured[0].name == "llm_call"
    assert captured[0].duration_ms == 4.0  # (5-1)ms


def test_trace_propagates_through_real_queue() -> None:
    """端到端关联:ingress 把 traceparent 注入入队消息 → worker claim 后取回同一 trace_id 建子 span。"""
    from agent_py_agent.agent.ingress_queue import IngressQueue, QueueConfig
    from agent_py_agent.agent.storage_backend import StorageBackend

    queue = IngressQueue(StorageBackend.in_memory(), QueueConfig(lane_cap=10))
    queue.ensure_schema()

    root = new_trace()  # ingress 侧开 trace
    payload: dict = {"event": "msg"}
    inject(root, payload)  # 注入 traceparent 到消息体
    queue.enqueue("evt-1", "laneA", payload)

    claimed = queue.claim()  # worker 侧领取
    assert claimed is not None
    worker_ctx = extract(claimed.payload)  # 取回 trace context
    assert worker_ctx is not None
    assert worker_ctx.trace_id == root.trace_id  # 同一 trace 串起两层
    child = child_context(worker_ctx)  # worker 建子 span
    assert child.trace_id == root.trace_id
    assert child.span_id != root.span_id
