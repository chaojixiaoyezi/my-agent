from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
from agent_py_agent.agent.backends.request_scope import (
    foreground_model_active,
    foreground_model_scope,
    provider_request_budget,
    provider_request_timeout,
)
from agent_py_agent.agent.concurrency.interrupt import register_interrupt_callback
from agent_py_agent.agent.memory_store.curator_backend import (
    CuratorModelStillRunningError,
    CuratorModelTimeoutError,
    call_backend_with_timeout,
)


def test_foreground_scope_shares_origin_and_releases_on_error():
    front = SimpleNamespace(api_base="http://model.invalid/v1", model_name="a")
    same = SimpleNamespace(api_base="http://model.invalid:80/anthropic", model_name="b")
    different = SimpleNamespace(api_base="http://other.invalid/v1")
    assert not foreground_model_active(same)
    with pytest.raises(ValueError), foreground_model_scope(front):
        with foreground_model_scope(same):
            assert foreground_model_active(same)
            assert not foreground_model_active(different)
        assert foreground_model_active(same)
        raise ValueError("test")
    assert not foreground_model_active(front)


def test_budget_reaches_http_without_mutating_backend_or_other_thread():
    backend = OpenAICompatibleBackend(BackendOptions(
        api_base="http://model.invalid/v1", api_key="test", model_name="test", request_timeout=240,
    ))
    seen = []
    with provider_request_budget(720):
        request = backend._gateway_request("/chat/completions", {}, {})
        assert 710 < request.timeout <= 720
        with provider_request_budget(900):
            assert provider_request_timeout(240) <= request.timeout
        thread = threading.Thread(target=lambda: seen.append(provider_request_timeout(240)))
        thread.start()
        thread.join(timeout=1)
    assert seen == [240]
    assert backend.request_timeout == 240
    assert provider_request_timeout(240) == 240


# LLM: 测试后端用可取消 Event 模拟阻塞传输，没有网络或生产状态。
# 类用途: 核验后台超时确实关闭连接，且预算进入请求线程。
class _CancelableBackend:
    def __init__(self):
        self.cancelled = threading.Event()
        self.finished = threading.Event()
        self.budget = 0.0

    def generate_structured(self, prompt, *, response_schema):
        self.budget = provider_request_timeout(240)
        with register_interrupt_callback(self.cancelled.set):
            self.cancelled.wait(2)
        self.finished.set()
        return SimpleNamespace(text="{}")


def test_curator_timeout_cancels_transport_before_retry():
    backend = _CancelableBackend()
    with pytest.raises(CuratorModelTimeoutError) as raised:
        call_backend_with_timeout(backend, prompt="test", response_schema={}, timeout_seconds=0.05)
    assert not isinstance(raised.value, CuratorModelStillRunningError)
    assert backend.cancelled.is_set() and backend.finished.is_set()
    assert 0 < backend.budget <= 0.05


def test_uncooperative_curator_cannot_overlap_previous_request():
    release = threading.Event()
    calls = []

    def generate(prompt, *, response_schema):
        calls.append(1)
        release.wait(3)
        return SimpleNamespace(text="{}")

    backend = SimpleNamespace(generate_structured=generate)
    try:
        with pytest.raises(CuratorModelStillRunningError):
            call_backend_with_timeout(backend, prompt="test", response_schema={}, timeout_seconds=0.01)
        with pytest.raises(CuratorModelStillRunningError):
            call_backend_with_timeout(backend, prompt="test", response_schema={}, timeout_seconds=0.01)
        assert calls == [1]
    finally:
        release.set()
