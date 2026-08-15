"""OpenTelemetry SDK/OTLP 正式导出桥；本地未配置时保持零依赖。"""

from __future__ import annotations

import atexit
import os
import threading
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_LOCK = threading.Lock()
_RUNTIME: OtelRuntime | None = None


def _trace_endpoint(env: Mapping[str, str]) -> str:
    specific = str(env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or "").strip()
    if specific:
        return specific
    base = str(env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
    if not base:
        return ""
    parts = urlsplit(base)
    path = parts.path.rstrip("/")
    if not path.endswith("/v1/traces"):
        path += "/v1/traces"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


class OtelRuntime:
    def __init__(self, provider: Any, tracer: Any) -> None:
        self.provider = provider
        self.tracer = tracer
        self._shutdown = False

    def start_span(self, name: str, context: object) -> Any:
        from opentelemetry import trace

        trace_id = int(str(getattr(context, "trace_id", "0")), 16)
        span_id = int(str(getattr(context, "span_id", "0")), 16)
        flag_value = trace.TraceFlags.SAMPLED if bool(getattr(context, "sampled", True)) else trace.TraceFlags.DEFAULT
        flags = trace.TraceFlags(flag_value)
        parent = trace.NonRecordingSpan(
            trace.SpanContext(
                trace_id=trace_id,
                span_id=span_id,
                is_remote=True,
                trace_flags=flags,
                trace_state=trace.TraceState(),
            )
        )
        return self.tracer.start_span(
            name,
            context=trace.set_span_in_context(parent),
            attributes={"my_agent.traceparent": str(getattr(context, "traceparent", ""))},
        )

    def finish_span(self, span: Any, exc: BaseException | None) -> None:
        if exc is not None:
            from opentelemetry.trace import Status, StatusCode

            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
        span.end()

    def instrument_fastapi(self, app: object) -> None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, tracer_provider=self.provider)

    def shutdown(self) -> None:
        if not self._shutdown:
            self._shutdown = True
            self.provider.shutdown()


def configure_otel_from_env(
    *,
    service_name: str,
    env: Mapping[str, str] | None = None,
    app: object | None = None,
    required: bool = False,
) -> OtelRuntime | None:
    """按标准 OTEL env 启动 Batch OTLP/HTTP exporter；scale 模式缺依赖/endpoint 时硬失败。"""
    global _RUNTIME
    values = env if env is not None else os.environ
    endpoint = _trace_endpoint(values)
    if not endpoint:
        if required:
            raise RuntimeError("scale 模式要求 OTEL_EXPORTER_OTLP_ENDPOINT")
        return None
    with _LOCK:
        if _RUNTIME is None:
            _RUNTIME = _build_runtime(service_name, endpoint, required=required)
            atexit.register(_RUNTIME.shutdown)
        runtime = _RUNTIME
    if app is not None:
        runtime.instrument_fastapi(app)
    return runtime


def _build_runtime(service_name: str, endpoint: str, *, required: bool) -> OtelRuntime:
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        message = "OpenTelemetry 导出需安装 my-agent[scale] 的 OTel 依赖"
        if required:
            raise RuntimeError(message) from exc
        raise
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    return OtelRuntime(provider, provider.get_tracer("my-agent"))


def start_otel_span(name: str, context: object) -> Any | None:
    runtime = _RUNTIME
    return runtime.start_span(name, context) if runtime is not None else None


def finish_otel_span(span: Any | None, exc: BaseException | None) -> None:
    runtime = _RUNTIME
    if runtime is not None and span is not None:
        runtime.finish_span(span, exc)


def reset_otel_for_test() -> None:
    global _RUNTIME
    with _LOCK:
        runtime, _RUNTIME = _RUNTIME, None
    if runtime is not None:
        runtime.shutdown()


__all__ = [
    "configure_otel_from_env",
    "finish_otel_span",
    "reset_otel_for_test",
    "start_otel_span",
]
