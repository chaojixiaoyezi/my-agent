from __future__ import annotations

import pytest

from agent_py_agent.agent.observability.otel import _trace_endpoint, configure_otel_from_env


def test_generic_otlp_endpoint_gets_trace_signal_path() -> None:
    assert _trace_endpoint({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}) == (
        "http://collector:4318/v1/traces"
    )


def test_signal_specific_endpoint_is_preserved() -> None:
    endpoint = "https://collector.example/custom-traces?tenant=acme"
    assert _trace_endpoint({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": endpoint}) == endpoint


def test_local_without_endpoint_is_noop() -> None:
    assert configure_otel_from_env(service_name="test", env={}) is None


def test_scale_without_endpoint_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="OTEL_EXPORTER_OTLP_ENDPOINT"):
        configure_otel_from_env(service_name="test", env={}, required=True)
