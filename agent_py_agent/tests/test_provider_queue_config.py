"""根配置delta的独立合同，禁止并入F5作者patch。"""
import pytest

from agent_py_agent.agent.backends.factory import get_backend
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig, load_config
from agent_py_agent.agent.settings.config import normalize_agent_config
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call


@pytest.mark.parametrize('raw,expected', [('2', 2), (0, 0), ('0', 0)])
def test_provider_limit_coerces_and_reaches_real_backend_options(raw, expected):
    normalized, warnings = normalize_agent_config({'provider_max_concurrent_requests': raw})
    assert not warnings and normalized['provider_max_concurrent_requests'] == expected
    config = AgentConfig(model_backend='anthropic_compatible', api_base='https://example.invalid',
        api_key='synthetic', model_name='fake', provider_max_concurrent_requests=expected)
    backend = get_backend(config.model_backend, config)
    assert backend.provider_max_concurrent_requests == expected


@pytest.mark.parametrize('raw', [True, False, -1, '-3', 'invalid'])
def test_provider_limit_rejects_bool_negative_and_invalid_with_warning(raw):
    normalized, warnings = normalize_agent_config({'provider_max_concurrent_requests': raw})
    assert normalized['provider_max_concurrent_requests'] == 0
    assert any('provider_max_concurrent_requests' in warning for warning in warnings)


def test_provider_limit_yaml_is_effective_but_user_config_cannot_change_it(tmp_path, monkeypatch):
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    path = tmp_path / 'settings.yaml'
    path.write_text('model_backend: echo\nprovider_max_concurrent_requests: 2\n')
    config = load_config(path)
    config.my_agent_home = str(tmp_path / 'home')
    config.execution_mode = 'local_unmanaged'
    assert config.provider_max_concurrent_requests == 2
    agent = SimpleAgent(config, tmp_path / 'workspace')
    before = path.read_bytes()
    denied = execute_registry_test_call(agent.tools, 'user_config',
        {'action': 'set', 'key': 'provider_max_concurrent_requests', 'value': '0'})
    assert not denied.ok and 'PARAMETER_BOUNDARY' in denied.output
    assert path.read_bytes() == before and agent.config.provider_max_concurrent_requests == 2
    spec = parameter_registry()['provider_max_concurrent_requests']
    assert not spec.writable and spec.default == 0
