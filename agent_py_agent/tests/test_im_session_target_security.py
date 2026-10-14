"""错落会话记录必须在原工具入口拒绝；不把合成损坏账本声称为真实用户能写宿主状态。"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.tools.create_session_task import (
    CreateSessionTaskTool,
)
from agent_py_agent.agent.agent_core.orchestration.tools.send_session_message import (
    SendSessionMessageTool,
)
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.conversation.host_actor import actor_from_scope
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call


# LLM: 当前主体和开关均由合成宿主配置；只造一个明确错落/旧目标记录，仍走原 Registry/Executor/Store。
# 函数用途: 生成启用会话消息或管理员派活的准确 owner 和发送线程，不调用模型或外部服务。
def _case(tmp_path, tool_name, *, outside_store=False):
    ordinary = tool_name == "send_session_message"
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), enable_plugins=False,
        my_agent_owner_provider="feishu" if ordinary else "local",
        my_agent_owner_kind="group" if ordinary else "main",
        my_agent_owner_id="oc_group" if ordinary else "main"), tmp_path)
    agent._capability_config_runtime_snapshot = SimpleNamespace(config=CapabilityConfig(
        session_messaging_user_enabled=True, session_task_admin_enabled=True))
    if outside_store:
        agent.conversation_store = ConversationStore(tmp_path / "noncanonical-conversation")
    home = agent.home_paths
    channel, conversation = ("feishu", "oc_group") if ordinary else ("chat", "synthetic-source")
    source = agent.conversation_store.threads.get_or_create({"channel": channel, "channel_user_id": "synthetic-sender",
        "channel_conversation_id": conversation, "canonical_user_id": "synthetic-sender",
        "owner_id": home.owner_id, "owner_home": str(home.owner_home_dir)})
    actor = actor_from_scope(agent, SimpleNamespace(channel=channel, channel_user_id="synthetic-sender",
        channel_conversation_id=conversation), source.thread_id, "synthetic-request")
    agent._current_run_params = RunParams(request_id="synthetic-request",
        task_attributes={"conversation_thread_id": source.thread_id}, host_channel_actor=actor)
    tool = SendSessionMessageTool(agent) if ordinary else CreateSessionTaskTool(agent)
    if tool_name not in agent.tools.tools:
        agent.tools.register(tool)
    return agent


# LLM: 目标记录的 owner/home 变化只在测试临时账中发生，不修改发送方事实或签发新的权限。
# 函数用途: 给目标加载合同建立当前本人、冲突和旧无字段记录。
def _target(agent, *, owner_id=None, owner_home=None):
    home = agent.home_paths
    return agent.conversation_store.threads.get_or_create({"channel": "chat", "channel_user_id": "synthetic-target",
        "channel_conversation_id": "synthetic-target", "canonical_user_id": "synthetic-target",
        "owner_id": home.owner_id if owner_id is None else owner_id,
        "owner_home": str(home.owner_home_dir) if owner_home is None else owner_home})


@pytest.mark.parametrize("tool_name", ["send_session_message", "create_session_task"])
@pytest.mark.parametrize("invalid", ["foreign_owner", "foreign_home", "relative_home"])
def test_declared_target_owner_or_home_conflict_is_denied_before_any_queue(tmp_path, tool_name, invalid):
    agent = _case(tmp_path, tool_name)
    target = _target(agent, owner_id="providers/feishu/groups/oc_foreign" if invalid == "foreign_owner" else None,
        owner_home=str(tmp_path / "foreign-owner") if invalid == "foreign_home" else "relative-home" if invalid == "relative_home" else None)
    args = {"target_thread_id": target.thread_id,
        "message" if tool_name == "send_session_message" else "goal": "合成普通任务"}
    result = execute_registry_test_call(agent.tools, tool_name, args, register_with=agent)
    assert not result.ok and result.error_code == "SESSION_TARGET_OUT_OF_SCOPE"
    store = agent.conversation_store
    assert not store.guidance.recent("thread", target.thread_id)
    assert not store.wakes.pending()
    assert not store.session_tasks.list_report(limit=0)[0]


@pytest.mark.parametrize("tool_name", ["send_session_message", "create_session_task"])
def test_complete_current_owner_target_still_queues_through_original_executor(tmp_path, tool_name):
    agent = _case(tmp_path, tool_name)
    target = _target(agent)
    result = execute_registry_test_call(agent.tools, tool_name, {"target_thread_id": target.thread_id,
        "message" if tool_name == "send_session_message" else "goal": "合成合法工作"}, register_with=agent)
    assert result.ok, (result.error_code, result.output)
    assert agent.conversation_store.guidance.recent("thread", target.thread_id)
    assert agent.conversation_store.wakes.pending()


def test_external_owner_unknown_legacy_target_is_not_claimed_by_current_directory(tmp_path):
    agent = _case(tmp_path, "send_session_message")
    target = _target(agent, owner_id="", owner_home="")
    result = execute_registry_test_call(agent.tools, "send_session_message", {"target_thread_id": target.thread_id,
        "message": "未知身份不能授权"}, register_with=agent)
    assert not result.ok and result.error_code == "SESSION_TARGET_OUT_OF_SCOPE"
    assert not agent.conversation_store.guidance.recent("thread", target.thread_id)
    assert not agent.conversation_store.wakes.pending()


@pytest.mark.parametrize("outside_store", [False, True])
def test_legacy_local_ownerless_target_requires_original_canonical_store(tmp_path, outside_store):
    agent = _case(tmp_path, "create_session_task", outside_store=outside_store)
    target = _target(agent, owner_id="", owner_home="")
    result = execute_registry_test_call(agent.tools, "create_session_task", {"target_thread_id": target.thread_id,
        "goal": "本地旧记录"}, register_with=agent)
    if outside_store:
        assert not result.ok and result.error_code == "SESSION_TARGET_OUT_OF_SCOPE"
        assert not agent.conversation_store.guidance.recent("thread", target.thread_id)
        assert not agent.conversation_store.wakes.pending()
    else:
        assert result.ok, (result.error_code, result.output)
        assert Path(agent.conversation_store.storage.root).resolve() == Path(
            agent.runtime_path_resolution.paths["conversation_workspace"]).resolve()
