"""审计 #19(热路径埋点,medium/稳定)真测:agent_core LLM 调用发 RED + token 指标到 /metrics。

原状:metrics 只在 ingress/worker 平面接线,agent_core LLM 调用热路径零埋点——最该被企业告警
消费的 RED(速率/错误/延迟)在 agent 里是空的。真走 LLM 调用 seam,断言 calls/duration/tokens
指标进了默认 registry(/metrics 暴露);埋点全程异常隔离,绝不影响 LLM 调用本身。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_model_generation
from agent_py_agent.agent.agent_core.model import llm_metrics
from agent_py_agent.agent.observability.metrics import (
    default_registry,
    reset_default_registry_for_test,
)


def _fresh() -> None:
    reset_default_registry_for_test()
    llm_metrics.reset_for_test()  # 指标实例按新 registry 重建


class _FakeBackend:
    def __init__(self, response: object, model_name: str = "claude-opus-4-8") -> None:
        self._response = response
        self.model_name = model_name

    def generate(self, prompt: str, **kwargs: object) -> object:
        return self._response


class _RaisingBackend:
    def generate(self, prompt: str, **kwargs: object) -> object:
        raise RuntimeError("backend boom")


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        agent=SimpleNamespace(conversation_store=None),
        params=SimpleNamespace(),
        ledger=None,
        call_id="test-call",
        retry_sink=None,
        tools=None,
        messages=None,
        on_chunk=None,
        tool_choice=None,
    )  # text 协议路径


def test_record_llm_call_emits_red_and_tokens() -> None:
    _fresh()
    resp = SimpleNamespace(usage={"input_tokens": 100, "output_tokens": 50})
    llm_metrics.record_llm_call("EchoBackend", 0.3, resp, ok=True)
    text = default_registry().render()
    assert 'agent_llm_calls_total{backend="EchoBackend",outcome="ok"} 1' in text  # 速率
    assert "agent_llm_duration_seconds_count" in text  # 延迟直方图
    assert 'agent_llm_input_tokens_total{backend="EchoBackend"} 100' in text  # token
    assert 'agent_llm_output_tokens_total{backend="EchoBackend"} 50' in text


def test_record_llm_call_error_outcome() -> None:
    _fresh()
    llm_metrics.record_llm_call("EchoBackend", 0.1, None, ok=False)
    text = default_registry().render()
    assert 'agent_llm_calls_total{backend="EchoBackend",outcome="error"} 1' in text  # 错误计入 RED


def test_record_llm_call_never_raises_on_bad_response() -> None:
    _fresh()

    class _BadResp:
        @property
        def usage(self) -> dict:
            raise ValueError("usage explode")

    llm_metrics.record_llm_call("X", 0.1, _BadResp(), ok=True)  # 不抛:埋点异常隔离


def test_seam_emits_metrics_and_returns_response() -> None:
    _fresh()
    resp = SimpleNamespace(usage={"input_tokens": 10, "output_tokens": 5})
    out = tool_model_generation._invoke_backend_generate(_FakeBackend(resp), "hi", _state())
    assert out is resp  # 原返回值不变
    text = default_registry().render()
    assert 'outcome="ok"' in text
    assert 'agent_llm_input_tokens_total{backend="_FakeBackend"} 10' in text


def test_seam_emits_error_and_reraises() -> None:
    _fresh()
    with pytest.raises(RuntimeError, match="backend boom"):
        tool_model_generation._invoke_backend_generate(_RaisingBackend(), "hi", _state())  # 异常仍传播
    assert 'outcome="error"' in default_registry().render()  # 失败计入 RED


def test_record_llm_cost_emits_to_metrics() -> None:
    _fresh()
    resp = SimpleNamespace(usage={"input_tokens": 100, "output_tokens": 50})
    llm_metrics.record_llm_cost("claude-opus-4-8", resp)  # opus 15/75 per Mtok → 0.0015 + 0.00375
    text = default_registry().render()
    assert 'agent_llm_cost_usd_total{model="claude-opus-4-8"} 0.00525' in text  # 真实 USD 成本可观测


def test_record_llm_cost_noop_without_model() -> None:
    _fresh()
    llm_metrics.record_llm_cost("", SimpleNamespace(usage={"input_tokens": 100}))  # 无 model → 不计
    llm_metrics.record_llm_cost("claude-opus-4-8", None)  # 无响应 → 不计
    assert "agent_llm_cost_usd_total" not in default_registry().render()


def test_seam_emits_cost_for_model() -> None:
    _fresh()
    resp = SimpleNamespace(usage={"input_tokens": 1_000_000, "output_tokens": 0})
    tool_model_generation._invoke_backend_generate(_FakeBackend(resp, model_name="claude-sonnet-4-6"), "hi", _state())
    text = default_registry().render()
    assert 'agent_llm_cost_usd_total{model="claude-sonnet-4-6"} 3' in text  # sonnet 3/Mtok in × 1M = $3


def test_default_registry_singleton_and_reset() -> None:
    _fresh()
    first = default_registry()
    assert default_registry() is first  # 单例
    reset_default_registry_for_test()
    assert default_registry() is not first  # 重置后新实例
