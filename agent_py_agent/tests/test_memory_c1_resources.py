# LLM: C1只验真实资源提取、引用与构造时点；网络传输禁止，原领域对象与Echo Agent真实装配，不验冷HTTP或写权。
# 模块用途: 核对原authority序列、显式模型纯叶与唯一embedding诊断，防提取改变默认费用/身份。
from dataclasses import fields, replace
from pathlib import Path

import pytest

from agent_py_agent.agent import core
from agent_py_agent.agent.memory_store import owner_resources as resources
from agent_py_agent.agent.retrieval import embedding_factory as embeddings
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.embedding_profile import embedding_model_config_for_resources
from agent_py_agent.agent.settings.model_profiles import (
    _default_for_owner,
    _default_for_owner_for_home,
    capture_selected_model_read,
    selected_model_config,
    selected_model_config_for_resources,
)
from agent_py_agent.agent.settings.model_scope import (
    ModelScopeDependencies,
    deployment_model_config,
    model_dependencies_scope,
)
from agent_py_agent.agent.settings.shared_model_catalog import (
    set_initial_profile,
    set_shared_profile,
)
from agent_py_agent.agent.settings.thread_model_selection import default_model_profile_id_for_home
from agent_py_agent.tests import _memory_c1_preimage as preimage
from agent_py_agent.tests.test_model_profiles import Host, add
from agent_py_agent.tests.test_shared_model_catalog import admin_host


# LLM: 外部urlopen一律失败，测试不监听socket或使用真实模型；不替代领域服务本身。
# 函数用途: 在整组纯提取测试中阻止意外网络调用。
@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError('C1 extraction cannot use network')
    monkeypatch.setattr('urllib.request.urlopen', forbidden)


# LLM: 使用真正SimpleAgent及独立home，原配额/Store/候选/索引/人格全部实际装配；不初始化外部模型。
# 函数用途: 建立Echo对照，测试只观察原构造及已有真实引用。
def agent_at(tmp_path, name):
    config = AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / name), memory_semantic_recall=False)
    return core.SimpleAgent(config, tmp_path / (name + '-project'))


def test_actual_authority_construction_and_field_assignment_order_match_preimage(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import agents_source_host
    from agent_py_agent.agent.settings import embedding_selection

    events, active = [], []
    original_wire = core._wire_memory_authorities
    actual_startup, actual_agents = embedding_selection.remember_startup_chat_host, agents_source_host.build_agents_source_repository
    constructors = {key: getattr(resources, key) for key in ('LocalStore', 'CandidateService', 'JsonlMemory')}
    persona = resources.PersonaRepository.from_home_paths

    def observe(name, constructor):
        def construct(*args, **kwargs):
            agent = active[0]
            if name == 'CandidateService':
                assert agent.local_store is events[-1][1]
            if name == 'JsonlMemory':
                assert events[-1][0] == 'startup'
                assert kwargs['local_store'] is agent.local_store
                assert kwargs['candidate_service'] is agent.memory_candidates
                assert kwargs['quota_enforcer'] is agent.owner_quota
            result = constructor(*args, **kwargs)
            events.append((name, result))
            return result
        return construct

    for name, constructor in constructors.items():
        wrapper = observe(name, constructor)
        monkeypatch.setattr(resources, name, wrapper)
        monkeypatch.setattr(preimage, name, wrapper)

    def create_persona(*args, **kwargs):
        assert active[0].memory is events[-1][1]
        result = persona(*args, **kwargs)
        events.append(('Persona', result))
        return result
    monkeypatch.setattr(resources.PersonaRepository, 'from_home_paths', create_persona)

    def startup(agent):
        assert events[-1][0] == 'semantic'
        assert agent.memory_candidates is events[-2][1]
        events.append(('startup', None))
        return actual_startup(agent)
    monkeypatch.setattr(embedding_selection, 'remember_startup_chat_host', startup)

    def semantic(agent, status):
        status['state'] = 'disabled'
        assert agent.memory_candidates is events[-1][1]
        events.append(('semantic', None))
        return None, None
    monkeypatch.setattr(core, '_memory_semantic_channel', semantic)

    def agents(agent):
        assert agent.persona_repository is events[-1][1]
        result = actual_agents(agent)
        events.append(('AGENTS', result))
        return result
    monkeypatch.setattr(agents_source_host, 'build_agents_source_repository', agents)

    def wire_original(agent, config, paths):
        active[:] = [agent]
        return preimage._wire_memory_authorities(agent, config, paths)
    monkeypatch.setattr(core, '_wire_memory_authorities', wire_original)
    first = agent_at(tmp_path, 'before')
    expected = [name for name, _ in events]
    events.clear()

    def wire_current(agent, config, paths):
        active[:] = [agent]
        return original_wire(agent, config, paths)
    monkeypatch.setattr(core, '_wire_memory_authorities', wire_current)
    second = agent_at(tmp_path, 'after')
    assert [name for name, _ in events] == expected == [
        'LocalStore', 'CandidateService', 'semantic', 'startup', 'JsonlMemory', 'Persona', 'AGENTS',
    ]
    for agent in (first, second):
        assert agent.persona_repository.agents_source is agent.agents_source_repository
        assert agent.memory.candidate_service is agent.memory_candidates
        assert agent.memory.local_store is agent.local_store
        assert agent.memory.quota_enforcer is agent.owner_quota


def test_explicit_real_resource_bundle_and_promotion_preserve_references(tmp_path):
    agent = agent_at(tmp_path, 'explicit')
    bundle = resources.OwnerMemoryResources(agent.local_store, agent.memory_candidates, agent.memory, agent.persona_repository)
    assert [field.name for field in fields(bundle)] == ['local_store', 'candidates', 'long_term', 'persona']
    assert not any(hasattr(bundle, key) for key in ('config', 'home_paths', 'run', 'backend', 'tools'))
    promotion = resources.build_owner_memory_promotion(agent.config, agent.home_paths, bundle,
        conversation_store=agent.conversation_store, lessons=agent.memory_lessons, hot=agent.memory_hot)
    assert promotion.candidates is agent.memory_candidates
    assert promotion.long_term is agent.memory
    assert promotion.persona is agent.persona_repository
    assert promotion.lessons is agent.memory_lessons and promotion.hot is agent.memory_hot
    assert promotion.exclusion_store is agent.conversation_store
    assert promotion.message_verifier.conversation_store is agent.conversation_store
    assert promotion.tool_verifier.ledger_store is agent.local_store
    assert promotion.tool_verifier.owner_root == agent.home_paths.owner_home_dir
    quota = resources.build_owner_memory_quota(agent.home_paths, agent.owner_policy)
    assert quota.owner_root == agent.owner_quota.owner_root
    assert (quota.max_bytes, quota.policy_available) == (agent.owner_quota.max_bytes, agent.owner_quota.policy_available)


@pytest.mark.parametrize('owner', ['local/main', 'alice'])
def test_explicit_profile_leaf_matches_deployment_and_capture_under_scoped_model(tmp_path, owner):
    host = admin_host(tmp_path / 'config') if owner == 'local/main' else Host(tmp_path / 'config')
    key, _ = add(host, model_name='chosen-model')
    deployment = host.config
    scoped = replace(deployment, model_name='scoped-model', temperature=0.77)
    with model_dependencies_scope(host, ModelScopeDependencies(host, (("config", scoped),))):
        assert host.config is scoped and deployment_model_config(host) is deployment
        with capture_selected_model_read() as original:
            selected = selected_model_config(host, profile_id=key)
        with capture_selected_model_read() as explicit:
            actual = selected_model_config_for_resources(deployment, host.home_paths, profile_id=key)
        assert vars(selected) == vars(actual) and original == explicit
        assert actual.temperature == deployment.temperature
    assert _default_for_owner(host) == _default_for_owner_for_home(host.home_paths)
    assert default_model_profile_id_for_home(host.home_paths) == 'default'


def test_initial_shared_default_and_embedding_capability_use_same_explicit_leaf(tmp_path):
    admin, member = admin_host(tmp_path / 'config'), Host(tmp_path / 'config')
    key, _ = add(admin)
    set_shared_profile(admin, key, True)
    set_initial_profile(admin, key)
    selected = selected_model_config(member)
    actual = selected_model_config_for_resources(member.config, member.home_paths)
    assert vars(actual) == vars(selected) and _default_for_owner_for_home(member.home_paths) == 'shared:' + key
    with pytest.raises(Exception) as error:
        embedding_model_config_for_resources(member.config, member.home_paths, 'shared:' + key)
    assert error.value.profile_reason == 'capability_mismatch'


def test_original_embedding_wrapper_and_explicit_factory_share_warning_state(tmp_path, monkeypatch, caplog):
    from agent_py_agent.agent.backends.errors import ModelNotConfiguredError
    from agent_py_agent.agent.user_space.home_layout import home_paths

    home = home_paths(tmp_path / 'never-created')
    config = AgentConfig(memory_semantic_recall=True, embedding_model_profile='missing-c1')
    calls = []
    def unavailable():
        calls.append('client')
        raise ModelNotConfiguredError(profile_id='missing-c1', profile_reason='c1-unique-reason')
    status = {}
    embeddings.build_memory_embedder_for_resources(config, home, diagnostics=status, client_factory=unavailable)
    assert status['profile_reason'] == 'c1-unique-reason'
    assert not embeddings.first_profile_warning_for_owner(home.owner_id, 'c1-unique-reason')
    embeddings.build_memory_embedder_for_resources(config, home, diagnostics={}, client_factory=unavailable)
    assert calls == ['client', 'client'] and len(caplog.records) == 1
    assert not Path(home.root).exists()
    assert not hasattr(core, '_PROFILE_WARNINGS_SEEN') and not hasattr(core, '_PROFILE_WARNINGS_LOCK')


def test_explicit_embedding_identity_same_actual_client_and_no_resource_side_effect(tmp_path):
    from agent_py_agent.agent.retrieval.embedding import OpenAICompatibleEmbedder
    from agent_py_agent.agent.settings.embedding_profile import embedding_identity
    from agent_py_agent.agent.user_space.home_layout import home_paths

    config = AgentConfig(memory_semantic_recall=True, embedding_model_profile='explicit-c1')
    home = home_paths(tmp_path / 'uncreated')
    client = OpenAICompatibleEmbedder(api_base='https://c1.invalid/v1', model='c1-model', api_key='fake')
    status = {}
    actual, identity = embeddings.memory_semantic_channel_for_resources(config, home, status,
        embedder_factory=lambda: client)
    assert actual is client and identity == embedding_identity('explicit-c1', client)
    assert not Path(home.root).exists()


@pytest.mark.parametrize('model', ['text-embedding-c1', 'embo-01'])
def test_explicit_embedding_client_matches_real_wrapper_profile_without_requests(tmp_path, model):
    from agent_py_agent.tests.test_embedding_service import _add
    host = Host(tmp_path / 'profile-config')
    key, _ = _add(host, model_name=model)
    host.config.embedding_model_profile = key
    before = sorted((str(p), p.read_bytes()) for p in tmp_path.rglob('*') if p.is_file())
    wrapped = core._embedding_client(host)
    explicit = embeddings.embedding_client_for_resources(host.config, host.home_paths)
    assert type(wrapped) is type(explicit)
    assert (wrapped.protocol, wrapped.model, wrapped.api_base, wrapped._api_key) == (
        explicit.protocol, explicit.model, explicit.api_base, explicit._api_key)
    assert sorted((str(p), p.read_bytes()) for p in tmp_path.rglob('*') if p.is_file()) == before


def test_explicit_authority_factory_uses_real_same_quota_and_canonical_refs(tmp_path):
    from agent_py_agent.agent.user_space.runtime_paths import resolve_runtime_paths_for_agent
    agent = agent_at(tmp_path, 'authority-factory')
    paths = resolve_runtime_paths_for_agent(agent.config, agent.home_paths.owner_home_dir, agent.home_paths).paths
    bundle = resources.build_owner_memory_authorities(agent.config, agent.home_paths, paths, agent.owner_quota,
        embedder=None, semantic_status={'state': 'disabled'}, vector_identity=None)
    assert bundle.long_term.path == agent.memory.path
    assert bundle.candidates.path == agent.memory_candidates.path
    assert bundle.long_term.candidate_service is bundle.candidates
    assert bundle.long_term.local_store is bundle.local_store
    assert bundle.long_term.quota_enforcer is agent.owner_quota
    assert bundle.persona.quota_enforcer is agent.owner_quota
    assert bundle.persona.agents_source is None
    assert resources.build_owner_memory_quota(None, agent.owner_policy) is None
    broken = resources.build_owner_memory_quota(agent.home_paths,
        replace(agent.owner_policy, load_errors=({'context': 'owner_policy.quota'},)))
    assert not broken.policy_available
