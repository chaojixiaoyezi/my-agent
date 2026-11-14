"""冷工具探针共享原模型名额，等待不计费且成功缓存不再领槽。"""
import re
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.errors import ProviderTransientError
from agent_py_agent.agent.backends.http import HttpBackend, unaccounted_probe_attempt_count
from agent_py_agent.agent.backends.request_scope import provider_request_budget
from agent_py_agent.agent.concurrency.interrupt import interrupt_by_name, register_interruptible
from agent_py_agent.agent.llm_scale import hot_path


@pytest.fixture(autouse=True)
def admission(monkeypatch):
    monkeypatch.setenv('LLM_MAX_INFLIGHT', '1')
    monkeypatch.setenv('LLM_ADMISSION_WAIT_SECONDS', '10')
    hot_path.reset_hot_path_admission_for_test()
    yield
    hot_path.reset_hot_path_admission_for_test()


class ProbeBackend(HttpBackend):
    def __init__(self):
        super().__init__(BackendOptions(api_base='https://invalid.test', api_key='synthetic', model_name='offline'))
        self.calls = 0
        self.entered = threading.Event()

    def generate(self, prompt, **kwargs):
        assert hot_path._limiter().in_flight() == 1
        self.calls += 1
        self.entered.set()
        nonce = re.search(r'nonce ([0-9a-f]+)', prompt).group(1)
        return SimpleNamespace(tool_use_blocks=[{'id': 'synthetic-call', 'name': 'my_agent_capability_probe',
                                                'input': {'nonce': nonce}}])


def test_single_slot_probe_and_cache_hit_do_not_nest():
    backend = ProbeBackend()
    result = backend.probe_tool_capability()
    assert result.native_supported and backend.calls == 1
    assert hot_path._limiter().in_flight() == 0
    # 成功缓存即便在已有名额内部读取，也不领取第二个名额。
    with hot_path.global_llm_admission_slot():
        assert backend.probe_tool_capability() is result
        assert backend.calls == 1


def test_wait_budget_rejects_without_probe_attempt_and_releases_single_flight():
    backend = ProbeBackend()
    before = unaccounted_probe_attempt_count()
    with hot_path.global_llm_admission_slot():
        with provider_request_budget(0.02), pytest.raises(ProviderTransientError):
            backend.probe_tool_capability()
        assert hot_path._limiter().in_flight() == 1
    assert backend.calls == 0 and unaccounted_probe_attempt_count() == before
    assert not backend._provider_tool_capability_lock.locked()
    assert backend.probe_tool_capability().native_supported
    assert hot_path._limiter().in_flight() == 0


def test_probe_waits_for_other_transport_without_counting_queue_as_attempt():
    backend = ProbeBackend()
    waiting = threading.Event()
    result = []

    def probe():
        waiting.set()
        result.append(backend.probe_tool_capability())

    with hot_path.global_llm_admission_slot():
        thread = threading.Thread(target=probe)
        thread.start()
        assert waiting.wait(1)
        assert not backend.entered.wait(0.05)
    thread.join(2)
    assert not thread.is_alive()
    assert len(result) == 1 and result[0].native_supported
    assert backend.calls == 1 and hot_path._limiter().in_flight() == 0


def test_cancelled_admission_waiter_never_starts_probe_after_slot_frees():
    backend = ProbeBackend()
    waiting = threading.Event()
    errors = []
    before = unaccounted_probe_attempt_count()

    def probe():
        with register_interruptible('probe-admission-cancel'):
            waiting.set()
            try:
                backend.probe_tool_capability()
            except InterruptedError as error:
                errors.append(error)

    with hot_path.global_llm_admission_slot():
        thread = threading.Thread(target=probe)
        thread.start()
        assert waiting.wait(1)
        assert not backend.entered.wait(0.05)
        assert interrupt_by_name('probe-admission-cancel')
    thread.join(2)
    assert not thread.is_alive() and len(errors) == 1
    assert backend.calls == 0 and unaccounted_probe_attempt_count() == before
    assert not backend._provider_tool_capability_lock.locked()
    assert hot_path._limiter().in_flight() == 0
