"""W9/W2: retry sampling, not committed or unknown tool effects."""
import json

import pytest

from agent_py_agent.agent.agent_core import provider_transient_auto_resume as retry
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderTransientError
from agent_py_agent.agent.backends.responses_failure import failed_event_error
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    make_test_runtime_policy,
)


def connection_limit_error():
    return failed_event_error({'type': 'error', 'error': {
        'code': 'websocket_connection_limit_reached', 'type': 'capacity', 'param': 'connections',
        'message': 'connection pool busy', 'request_id': 'connection-one'}})


@pytest.fixture
def short_retry(monkeypatch):
    waits = []
    monkeypatch.setattr(retry, 'provider_transient_retry_delays', lambda policy=None: (0.01, 0.01))
    monkeypatch.setattr(retry, 'provider_transient_total_budget_seconds', lambda policy=None: 60.0)
    monkeypatch.setattr(retry, 'apply_retry_jitter', lambda value: value)
    monkeypatch.setattr(retry, 'wait_interruptibly', waits.append)
    return waits


class CommitOnceTool(BaseTool):
    model_spec = make_test_model_spec('commit_once')
    runtime_policy = make_test_runtime_policy('mutating')

    def __init__(self):
        self.calls = 0

    def execute(self, arguments):
        self.calls += 1
        return ToolHandlerOutcome('commit_once', True, 'committed-once')


class ToolThenConnectionLimitBackend:
    name = 'anthropic_compatible'

    def probe_tool_capability(self):
        return ProviderToolCapability(provider=self.name, endpoint='local://w9-fixture', model='controlled',
            stream=False, native_supported=True, evidence='controlled_test_backend')

    def __init__(self):
        self.calls = 0
        self.messages = []

    def generate(self, prompt, **options):
        self.calls += 1
        self.messages.append(options.get('messages', []))
        if self.calls == 1:
            return ModelResponse('', self.name, tool_use_blocks=[
                {'id': 'committed-tool', 'name': 'commit_once', 'input': {}}])
        if self.calls == 2:
            raise connection_limit_error()
        return ModelResponse('continued', self.name)


def test_real_agent_resumes_model_call_without_replaying_committed_tool(tmp_path, short_retry):
    agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path='memory.jsonl'), tmp_path)
    backend, tool = ToolThenConnectionLimitBackend(), CommitOnceTool()
    agent.backend = backend
    agent.tools.tools[tool.model_spec.name] = tool
    result = agent.run('调用一次 commit_once，然后汇报。', save=False, allowed_tools=['commit_once'])
    assert result.response == 'continued'
    assert tool.calls == 1
    assert backend.calls == 3
    assert short_retry == [0.01]
    assert 'committed-once' in json.dumps(backend.messages[-1])


def test_budget_exhaustion_keeps_connection_diagnostic_and_existing_control_code(short_retry, monkeypatch):
    monkeypatch.setattr(retry, 'provider_transient_total_budget_seconds', lambda policy=None: 0.001)
    error = connection_limit_error()
    calls = []

    def sample():
        calls.append(1)
        raise error

    with pytest.raises(ProviderTransientError) as caught:
        retry.run_with_provider_transient_auto_resume(sample)
    assert caught.value is error
    assert error.error_code == retry.PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED
    assert error.details['provider_error']['code'] == 'websocket_connection_limit_reached'
    assert calls == [1] and short_retry == []


def test_unknown_tool_effect_guard_prevents_automatic_retry(short_retry):
    error = connection_limit_error()
    calls = []

    def sample():
        calls.append(1)
        raise error

    with pytest.raises(ProviderTransientError) as caught:
        retry.run_with_provider_transient_auto_resume(sample,
            callbacks=retry.ProviderTransientRetryCallbacks(retry_guard=lambda: False))
    assert caught.value is error
    assert calls == [1] and short_retry == []


def test_unknown_structured_provider_code_is_not_promoted_by_message(short_retry):
    error = failed_event_error({'type': 'error', 'error': {
        'code': 'unknown_code', 'message': 'websocket_connection_limit_reached'}})
    with pytest.raises(type(error)) as caught:
        retry.run_with_provider_transient_auto_resume(lambda: (_ for _ in ()).throw(error))
    assert caught.value is error
    assert error.error_code == 'MODEL_RESPONSE_FAILED' and short_retry == []
