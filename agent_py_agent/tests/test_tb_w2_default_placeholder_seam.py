"""TB-W2: real /new producer -> model readiness, execution scope and /model card.

The test_gateway_lane_retry existing-session rule stays unchanged: set_default
never re-points an existing session. Only a new placeholder successor is rebound.
Real store and catalog operations run in the pytest temp home; no model call.
"""

from dataclasses import replace
from uuid import uuid4

import pytest

from agent_py_agent.agent.conversation.execution_threads import ensure_execution_thread
from agent_py_agent.agent.conversation.new_session import new_session_result
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    model_profiles_path,
    read_model_profiles,
)
from agent_py_agent.agent.settings.model_scope import (
    model_dependency_lifetime,
    selected_model_scope,
)
from agent_py_agent.agent.settings.thread_model_selection import (
    thread_model_is_configured,
)


# LLM: Only pytest's temporary owner is initialized; no production model call.
# 函数用途: 创建隔离的真实Agent和会话存储。
def _agent(tmp_path):
    return SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[],
                                   stream_enabled=False, gateway_per_user_owner_scoping=False), tmp_path / "ws")


# LLM: Use the real channel binding and initial model-default producer.
# 函数用途: 在临时owner里创建飞书会话。
def _feishu_thread(agent, chat):
    home = agent.home_paths
    return agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "ou_owner", "owner_id": home.owner_id, "owner_home": str(home.owner_home_dir),
        "channel": "feishu", "channel_user_id": "ou_owner", "channel_conversation_id": chat,
        "cwd": str(home.owner_home_dir), "title": "历史会话"})


# LLM: Catalog add/set_default are real operations; credentials are inert fixtures.
# 函数用途: 用真实目录操作设置当前新会话默认模型。
def _select_real_profile(agent, model_name="fixture-model"):
    profile_id = str(uuid4())
    profile = {"model_backend": "anthropic_compatible", "model_name": model_name,
               "api_base": "https://fixture.example.invalid", "api_key": "fake",
               "model_context_window_tokens": 65536}
    assert execute_model_profile_operation(agent, "add", {"profile_id": profile_id, "profile": profile})["ok"]
    assert execute_model_profile_operation(agent, "set_default", {"profile_id": profile_id})["ok"]
    assert read_model_profiles(model_profiles_path(agent.home_paths))["selected"] == profile_id
    return profile_id


# LLM: Exercise the actual /new domain entry and reread its persisted successor.
# 函数用途: 创建后继并读取真实落盘结果。
def _new(agent, thread_id):
    result = new_session_result(agent.conversation_store, thread_id, title="新会话")
    assert result.ok, result
    return agent.conversation_store.threads.require(result.details["thread_id"])


# LLM: Compare every copied model-selection field, including the timestamp.
# 函数用途: 提取需要保持一致的模型选择事实。
def _selection(thread):
    return (thread.model_profile_id, thread.model_selection_revision, thread.model_selection_source,
            thread.model_selection_last_explicit_revision, thread.model_selection_selected_at)


# LLM: Real /new output must work for all three consumers without changing its predecessor's model facts.
# 函数用途: 验证默认或继承占位后继采用当前默认，旧会话仍未配置。
@pytest.mark.parametrize("source", ["default", "inherited"])
def test_new_from_placeholder_predecessor_starts_on_owner_default(tmp_path, source):
    agent = _agent(tmp_path)
    old = _feishu_thread(agent, "oc_private")
    assert (old.model_profile_id, old.model_selection_source) == ("default", "default")
    if source == "inherited":
        old = ensure_execution_thread(agent, old.thread_id, "tb-w2-placeholder", kind="scheduled")
    # Seed a persisted historical timestamp; update_atomic preserves timestamps at the same revision.
    agent.conversation_store.threads.write(replace(old, model_selection_selected_at=123.0))
    old = agent.conversation_store.threads.require(old.thread_id)
    assert old.model_selection_selected_at == 123.0
    assert old.model_selection_source == source
    original_selection = _selection(old)
    profile_id = _select_real_profile(agent)
    assert _selection(agent.conversation_store.threads.require(old.thread_id)) == original_selection
    assert not thread_model_is_configured(agent, old.thread_id)
    successor = _new(agent, old.thread_id)
    fresh = _feishu_thread(agent, "oc_fresh")
    assert _selection(successor)[:4] == _selection(fresh)[:4] == (profile_id, 1, "default", 0)
    assert successor.model_selection_selected_at == old.model_selection_selected_at
    assert thread_model_is_configured(agent, successor.thread_id)
    with selected_model_scope(agent, thread_id=successor.thread_id), model_dependency_lifetime(agent):
        assert agent.config.model_name == "fixture-model"
    view = execute_model_profile_operation(agent, "list", {}, thread_id=successor.thread_id)
    assert view["selected"] == profile_id and not view.get("warning")
    predecessor = agent.conversation_store.threads.require(old.thread_id)
    assert _selection(predecessor) == original_selection
    assert not thread_model_is_configured(agent, old.thread_id)


# LLM: Explicit/automatic choices, and real default/inherited ids, must remain frozen across /new.
# 函数用途: 验证显式default、自动选择和真实模型引用原样继承。
@pytest.mark.parametrize("source,placeholder", [
    ("explicit", True), ("explicit", False), ("automatic", True), ("automatic", False),
    ("default", False), ("inherited", False),
])
def test_new_keeps_explicit_selection_even_when_it_is_default(tmp_path, source, placeholder):
    agent = _agent(tmp_path)
    selected = _select_real_profile(agent, "previous-model")
    thread = _feishu_thread(agent, "oc_private")
    if source == "inherited":
        thread = ensure_execution_thread(agent, thread.thread_id, "tb-w2-real-profile", kind="scheduled")
    _select_real_profile(agent, "current-default")
    if source in {"explicit", "automatic"}:
        thread = agent.conversation_store.threads.update_atomic(thread.thread_id, lambda row: replace(
            row, model_profile_id="default" if placeholder else selected, model_selection_revision=2,
            model_selection_source=source, model_selection_last_explicit_revision=2 if source == "explicit" else 0))
    agent.conversation_store.threads.write(replace(thread, model_selection_selected_at=123.0))
    chosen = agent.conversation_store.threads.require(thread.thread_id)
    assert chosen.model_selection_selected_at == 123.0
    assert (chosen.model_profile_id, chosen.model_selection_source) == ("default" if placeholder else selected, source)
    successor = _new(agent, chosen.thread_id)
    assert _selection(successor) == _selection(chosen)
    assert _selection(agent.conversation_store.threads.require(chosen.thread_id)) == _selection(chosen)


# LLM: An unselected catalog is not an error in /new and must match a fresh thread's initial binding.
# 函数用途: 验证尚未配置目录时后继保持占位。
def test_new_before_any_selection_keeps_the_placeholder(tmp_path):
    agent = _agent(tmp_path)
    old = _feishu_thread(agent, "oc_private")
    successor = _new(agent, old.thread_id)
    fresh = _feishu_thread(agent, "oc_fresh")
    assert _selection(successor) == _selection(fresh) == _selection(old)
    assert _selection(successor)[:4] == ("default", 1, "default", 0)
