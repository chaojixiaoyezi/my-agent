"""片A只验证真实依赖纯提取；不接冷HTTP、seal或第二记忆事实源。"""
from dataclasses import dataclass, fields

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
from agent_py_agent.agent.gateway_parts.owner_resources import resolve_owner_resource_configuration
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.private_interaction_transaction import (
    _owner_resource_configuration,
)
from agent_py_agent.agent.memory_store.owner_services import (
    ExplicitRememberServices,
    remember_explicit,
)
from agent_py_agent.agent.owner_scoped_pool import _config_with_owner
from agent_py_agent.agent.retrieval.embedding_usage import (
    EMBEDDING_USAGE,
    count_embedding_request,
    current_embedding_input_type,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests._owner_services_preimage import (
    _owner_resource_configuration as original_configuration,
)
from agent_py_agent.tests.test_memory_promotion_v2 import _runtime


@dataclass(frozen=True)
class _ResourceRequest:
    control: GatewayControlPlane
    owner: OwnerIdentity


@pytest.mark.parametrize('owner', [OwnerIdentity.local_main(), OwnerIdentity.provider_user('feishu', 'slice-user'),
                                   OwnerIdentity.provider_group('feishu', 'slice-group')])
@pytest.mark.parametrize('custom_paths', [False, True])
def test_owner_configuration_matches_original_without_resource_creation(tmp_path, monkeypatch, owner, custom_paths):
    config = AgentConfig(model_backend='echo')
    if custom_paths:
        config.memory_path = str(tmp_path / 'old-runtime/memory.jsonl')
        config.subagent_workspace = str(tmp_path / 'old-runtime/subagents')
    control = GatewayControlPlane(config, tmp_path / 'uncreated-home',
        gateway_paths_from_root(tmp_path / 'uncreated-gateway'), 1, 'fixture-unregistered')
    request = _ResourceRequest(control, owner)
    before = sorted(str(path) for path in tmp_path.rglob('*'))
    def forbid(*args, **kwargs):
        pytest.fail('pure owner resources constructed an Agent')
    monkeypatch.setattr(SimpleAgent, '__init__', forbid)
    expected = original_configuration(request)
    actual = resolve_owner_resource_configuration(control, owner)
    wrapped = _owner_resource_configuration(request)
    assert actual == expected == wrapped
    assert actual[0].owner_home_dir == expected[0].owner_home_dir
    assert vars(actual[1]) == vars(expected[1])
    assert actual[2] == expected[2]
    assert sorted(str(path) for path in tmp_path.rglob('*')) == before
    if custom_paths and owner != OwnerIdentity.local_main():
        assert not actual[1].memory_path and not actual[1].subagent_workspace
        assert actual[2]['memory_path'] != config.memory_path


# LLM: 只替换外部embedding传输；Candidate/Promotion/JSONL和计量入口都真实执行，不模拟领域dispatcher。
# 类用途: 为原记忆写入生成固定向量，并检查付费阶段原正式记录已写但候选尚未promoted。
class _Embedder:
    protocol = 'openai_compatible_embeddings'
    model = 'slice-embedding'
    api_base = 'https://embedding.example.invalid/v1'
    def __init__(self, events):
        self.events, self.agent = events, None
    def embed(self, texts):
        assert self.agent.memory.all()
        # 原Promotion正持候选锁；这里只读原文件，不能用list重取不可重入锁。
        assert self.agent.memory_candidates._load_unlocked()[0].status == 'approved'
        self.events.append(('embed', current_embedding_input_type()))
        return count_embedding_request(texts, lambda batch: ([[1.0, 0.0] for _ in batch], 7))


@pytest.mark.parametrize('owner', [OwnerIdentity.local_main(), OwnerIdentity.provider_user('feishu', 'slice-user'),
                                   OwnerIdentity.provider_group('feishu', 'slice-group')])
@pytest.mark.parametrize('semantic', [False, True])
@pytest.mark.parametrize('kind', ['fact', 'event', 'project'])
def test_actual_loaded_owner_remember_keeps_services_identity_order_and_cost(tmp_path, monkeypatch, owner, semantic, kind):
    from agent_py_agent.agent import core
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    events = []
    embedder = _Embedder(events)
    monkeypatch.setattr(core, '_embedding_client', lambda agent: embedder)
    config = _config_with_owner(AgentConfig(model_backend='echo', memory_semantic_recall=semantic,
                                          embedding_model_profile='slice-profile' if semantic else ''), owner)
    agent = SimpleAgent(config, tmp_path / 'root')
    embedder.agent = agent
    services = ExplicitRememberServices(agent.memory_candidates, agent.memory_promotion, agent.memory)
    assert [item.name for item in fields(services)] == ['candidates', 'promotion', 'long_term']
    assert services.candidates is agent.memory_candidates
    assert services.promotion is agent.memory_promotion
    assert services.long_term is agent.memory
    assert not hasattr(services, 'home_paths') and not hasattr(services, 'run')
    observe, review, promote = services.candidates.observe, services.promotion.review, services.promotion.promote
    def observed(value):
        events.append(('observe', value))
        return observe(value)
    def reviewed(*args, **kwargs):
        events.append(('review', kwargs))
        return review(*args, **kwargs)
    def promoted(*args, **kwargs):
        events.append(('promote', kwargs))
        result = promote(*args, **kwargs)
        events.append(('promoted', result.reason_code))
        return result
    monkeypatch.setattr(services.candidates, 'observe', observed)
    monkeypatch.setattr(services.promotion, 'review', reviewed)
    monkeypatch.setattr(services.promotion, 'promote', promoted)
    EMBEDDING_USAGE.reset()
    record = agent.remember('  原明确保存的项目事实  ', kind=' ' + kind.upper() + ' ')
    assert record.content == '原明确保存的项目事实' and record.kind == kind
    assert len(agent.memory.all()) == 1 and agent.memory_candidates.list()[0].status == 'promoted'
    assert agent.memory.path.is_relative_to(agent.home_paths.owner_home_dir)
    assert [event[0] for event in events] == (['observe', 'review', 'promote', 'embed', 'promoted'] if semantic
                                              else ['observe', 'review', 'promote', 'promoted'])
    observed_value = events[0][1]
    assert observed_value.origin == 'reviewed' and observed_value.scope.scope_type == 'personal'
    assert observed_value.observation_id.startswith('manual-remember:')
    assert events[1][1]['reviewer'] == 'local-user-explicit' and events[2][1]['confirmed'] is True
    before_calls = EMBEDDING_USAGE.snapshot()['embedding']['memory_write']['requests']
    assert before_calls == int(semantic)
    again = remember_explicit(services, '原明确保存的项目事实', kind=kind)
    assert again.entry_id == record.entry_id and len(agent.memory.all()) == 1
    assert [event[0] for event in events].count('review') == 1
    assert EMBEDDING_USAGE.snapshot()['embedding']['memory_write']['requests'] == before_calls
    if semantic:
        assert ('embed', 'db') in events
    EMBEDDING_USAGE.reset()


@pytest.mark.parametrize('content,kind,message', [('', 'fact', 'remember content 不能为空'),
    (' x ', 'preference', 'remember kind 只允许 fact/event/project；人格偏好请使用 update_persona')])
def test_original_validation_bytes_and_no_writes(tmp_path, content, kind, message):
    promotion, _, _ = _runtime(tmp_path)
    services = ExplicitRememberServices(promotion.candidates, promotion, promotion.long_term)
    before = services.candidates.path.read_bytes() if services.candidates.path.exists() else None
    with pytest.raises(ValueError) as caught:
        remember_explicit(services, content, kind=kind)
    assert str(caught.value).encode() == message.encode()
    assert (services.candidates.path.read_bytes() if services.candidates.path.exists() else None) == before
    assert not services.long_term.all()


def test_actual_service_exception_remains_same_object(tmp_path, monkeypatch):
    promotion, _, _ = _runtime(tmp_path)
    services = ExplicitRememberServices(promotion.candidates, promotion, promotion.long_term)
    fault = RuntimeError('original synthetic observation failure')
    def fail(observation):
        raise fault
    monkeypatch.setattr(services.candidates, 'observe', fail)
    with pytest.raises(RuntimeError) as caught:
        remember_explicit(services, '原事实')
    assert caught.value is fault
