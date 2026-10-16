"""真实SimpleAgent组合根接线；只用echo与隔离home，不把构造桩当生产接线。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_agents_source_repository import choose
from agent_py_agent.tests.test_thread_persona_freeze import prefix_and_updates, request


@pytest.fixture
def agent(tmp_path):
    return SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
                                  prompt_files=[], enable_tools=False, enable_self_learning=False), tmp_path / "workspace")


def configured_source(agent):
    source = getattr(agent.persona_repository, "agents_source", None)
    assert source is not None, "真实SimpleAgent必须把唯一来源仓库接给Persona和PromptBuilder"
    return source


def test_real_core_default_and_selected_source_reach_prompt(agent):
    source = configured_source(agent)
    assert source.owner == agent.home_paths.owner_id
    assert source.owner_home == agent.home_paths.owner_home_dir
    first, _ = prefix_and_updates(agent.prompts, request())
    target = source.owner_home / "instructions.arbitrary"
    target.write_text("# 工作约定\n- CORE_SOURCE_MARKER\n")
    choose(source, target)
    prefix, updates = prefix_and_updates(agent.prompts, request())
    assert prefix == first and "CORE_SOURCE_MARKER" not in updates
    prefix, _ = prefix_and_updates(agent.prompts, request(1))
    assert "CORE_SOURCE_MARKER" in prefix


def test_real_core_current_switch_is_not_frozen_at_startup(agent):
    source = configured_source(agent)
    target = source.owner_home / "arbitrary"
    target.write_text("# 约定\n- HOST_REVOKE_MARKER\n")
    choose(source, target)
    assert "HOST_REVOKE_MARKER" in prefix_and_updates(agent.prompts, request())[0]
    agent.config = replace(agent.config, agents_source_enabled=False)
    prefix, updates = prefix_and_updates(agent.prompts, request())
    assert "HOST_REVOKE_MARKER" not in prefix + updates


def test_real_core_default_does_not_touch_network_even_with_source_feature_disabled(agent, monkeypatch):
    from agent_py_agent.agent.capability import agents_source_network
    def forbidden(*args, **kwargs):
        pytest.fail("默认来源不能新增网络请求")
    monkeypatch.setattr(agents_source_network, "load_agents_url", forbidden)
    source = configured_source(agent)
    agent.config = replace(agent.config, agents_source_enabled=False, agents_source_network_enabled=True)
    assert source.load().content == source.default_path.read_text()


def test_real_core_network_permission_reloaded_without_new_agent(agent):
    source = configured_source(agent)
    agent.config = replace(agent.config, agents_source_network_enabled=True)
    assert source._url_allowed("https://example.com/rules") is True
    path = agent.home_paths.owner_permissions_json
    policy = json.loads(path.read_text())
    policy["network"] = {"enabled": False}
    path.write_text(json.dumps(policy))
    assert source._url_allowed("https://example.com/rules") is False
    path.write_text("bad json")
    assert source._url_allowed("https://example.com/rules") is False


def test_provider_owner_uses_native_owner_identity_and_rejects_other_home(tmp_path):
    config = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[],
                         my_agent_owner_provider="feishu", my_agent_owner_kind="user", my_agent_owner_id="alice",
                         enable_tools=False, enable_self_learning=False)
    agent = SimpleAgent(config, tmp_path / "workspace")
    source = configured_source(agent)
    assert source.owner == agent.home_paths.owner_id == "providers/feishu/users/alice"
    assert source.owner_home == agent.home_paths.root / "owners/providers/feishu/users/alice"
    assert source.load().snapshot.owner == source.owner
