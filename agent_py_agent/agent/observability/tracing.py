"""分布式追踪(Tier 4.2 可观测性):W3C Trace Context,让一个请求在 ingress→队列→worker 全链路关联。

自建取舍:W3C ``traceparent`` 格式(``00-{trace_id}-{span_id}-{flags}``)是个简单标准,自建生成/解析
零依赖、且产出就是 OTel/Jaeger 认的标准格式(将来要接 Jaeger/Tempo 再借 opentelemetry-sdk 做 export,
trace_id 不变可对接)。核心价值——跨两层架构的请求关联——自建即可拿到,不必先扛 OTel 重依赖。

跨进程/队列传播用 inject/extract(同 OTel propagator 思路):ingress 把 traceparent 注入入队消息,
worker 取出建子 span → 同一 trace_id 串起整条链路。id 生成可注入(确定性测试);Span 计时可注入时钟。
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16


def _gen_hex(n_bytes: int) -> str:
    return secrets.token_hex(n_bytes)


@dataclass(frozen=True)
class TraceContext:
    """W3C trace context:trace_id(16 字节/32 hex)+ span_id(8 字节/16 hex)+ 采样位。"""

    trace_id: str
    span_id: str
    sampled: bool = True

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{'01' if self.sampled else '00'}"


def new_trace(id_gen: Callable[[int], str] | None = None) -> TraceContext:
    """开一条新 trace(根 span)。id_gen 可注入做确定性测试。"""
    gen = id_gen or _gen_hex
    return TraceContext(trace_id=gen(16), span_id=gen(8))


def child_context(parent: TraceContext, id_gen: Callable[[int], str] | None = None) -> TraceContext:
    """同 trace 下的子 span:trace_id 不变、span_id 新生成、继承采样位。"""
    gen = id_gen or _gen_hex
    return TraceContext(trace_id=parent.trace_id, span_id=gen(8), sampled=parent.sampled)


def parse_traceparent(header: str) -> TraceContext | None:
    """解析 W3C traceparent 串 → TraceContext。格式非法/全零 id 返回 None。"""
    parts = header.split("-")
    if len(parts) != 4 or len(parts[1]) != 32 or len(parts[2]) != 16:
        return None
    if not _is_hex(parts[1]) or not _is_hex(parts[2]) or parts[1] == _ZERO_TRACE or parts[2] == _ZERO_SPAN:
        return None
    return TraceContext(trace_id=parts[1], span_id=parts[2], sampled=parts[3].endswith("1"))


def inject(context: TraceContext, carrier: dict) -> dict:
    """把 trace context 写入 carrier(用标准 traceparent 键),供跨队列/进程传播。"""
    carrier["traceparent"] = context.traceparent
    return carrier


def extract(carrier: dict) -> TraceContext | None:
    """从 carrier 取 traceparent → TraceContext(缺失/非法返回 None)。"""
    value = carrier.get("traceparent") if isinstance(carrier, dict) else None
    return parse_traceparent(value) if isinstance(value, str) else None


def _is_hex(value: str) -> bool:
    try:
        int(value, 16)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class SpanData:
    name: str
    context: TraceContext
    start_ns: int
    end_ns: int

    @property
    def duration_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1_000_000.0


class Span:
    """计时 span 上下文管理器:进入记开始、退出记结束+时长,回调 on_end(SpanData)落指标/日志。"""

    def __init__(self, name: str, context: TraceContext, *, clock: Callable[[], int] | None = None, on_end: Callable[[SpanData], None] | None = None) -> None:
        self._name = name
        self._ctx = context
        self._clock = clock or time.monotonic_ns
        self._on_end = on_end
        self._start = 0
        self._otel_span = None

    def __enter__(self) -> Span:
        self._start = self._clock()
        from agent_py_agent.agent.observability.otel import start_otel_span

        self._otel_span = start_otel_span(self._name, self._ctx)
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> bool:
        end = self._clock()
        from agent_py_agent.agent.observability.otel import finish_otel_span

        finish_otel_span(self._otel_span, exc_val if isinstance(exc_val, BaseException) else None)
        if self._on_end is not None:
            self._on_end(SpanData(self._name, self._ctx, self._start, end))
        return False  # 不吞异常
