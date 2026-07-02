"""§6-A"先量化再动手"的并发占用探针真测。

四个占用面(worker 忙数/后台 tick 在飞/runner 在飞/LLM 在飞)+ 一个等待面(网关排队等待),
全部落默认 registry、GET /metrics 可读。埋点异常隔离,inc/dec 成对(异常路径也归零)。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_model_generation
from agent_py_agent.agent.agent_core.model import llm_metrics
from agent_py_agent.agent.observability import concurrency_metrics
from agent_py_agent.agent.observability.metrics import default_registry, reset_default_registry_for_test


def _fresh() -> None:
    reset_default_registry_for_test()
    llm_metrics.reset_for_test()
    concurrency_metrics.reset_concurrency_metrics_for_test()


def test_gauges_and_queue_wait_render_to_metrics() -> None:
    _fresh()
    concurrency_metrics.gateway_worker_busy(1)
    concurrency_metrics.background_tick_inflight(1)
    concurrency_metrics.subagent_runner_inflight(1)
    concurrency_metrics.record_gateway_queue_wait(42.0)
    text = default_registry().render()
    assert "agent_gateway_workers_busy 1" in text
    assert "agent_background_owner_ticks_inflight 1" in text
    assert "agent_subagent_runners_inflight 1" in text
    assert "agent_gateway_queue_wait_seconds_count 1" in text
    # 42s 落进 60s 桶(排队饿死是几十秒~分钟级,桶必须够粗才能看出量级)
    assert 'agent_gateway_queue_wait_seconds_bucket{le="60"} 1' in text

    concurrency_metrics.gateway_worker_busy(-1)
    concurrency_metrics.background_tick_inflight(-1)
    concurrency_metrics.subagent_runner_inflight(-1)
    text = default_registry().render()
    assert "agent_gateway_workers_busy 0" in text
    assert "agent_background_owner_ticks_inflight 0" in text
    assert "agent_subagent_runners_inflight 0" in text


def _state() -> SimpleNamespace:
    return SimpleNamespace(tools=None, messages=None, on_chunk=None)


class _InflightCheckingBackend:
    """generate 期间读 gauge,证明 llm_inflight 真在调用期间为 1、调用外归零。"""

    def __init__(self) -> None:
        self.inflight_during_call: str = ""

    def generate(self, prompt: str, **kwargs: object) -> object:
        self.inflight_during_call = default_registry().render()
        return SimpleNamespace(text="ok")


class _RaisingBackend:
    def generate(self, prompt: str, **kwargs: object) -> object:
        raise RuntimeError("backend boom")


def test_llm_inflight_gauge_wraps_generation_and_resets_on_error() -> None:
    _fresh()
    backend = _InflightCheckingBackend()
    tool_model_generation._invoke_backend_generate(backend, "hi", _state())
    assert "agent_llm_inflight 1" in backend.inflight_during_call  # 调用期间在飞=1
    assert "agent_llm_inflight 0" in default_registry().render()  # 调用结束归零

    with pytest.raises(RuntimeError):
        tool_model_generation._invoke_backend_generate(_RaisingBackend(), "hi", _state())
    assert "agent_llm_inflight 0" in default_registry().render()  # 异常路径也归零
