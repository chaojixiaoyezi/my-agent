"""普通用户 IM 监控安全矩阵；真实 owner/账本/控制入口，供应商外发仅用合成运输。"""
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter import feishu
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.capability.channel_message_target import resolve_message_target
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.conversation.host_actor import actor_from_scope
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.schedule_control import execute_schedule_control
from agent_py_agent.agent.scheduler.tool import ScheduleTool
from agent_py_agent.agent.session_lock.service import UnlockService
from agent_py_agent.agent.session_lock.store import SessionLockStore
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.test_scheduler_host_actor import create, group_case

_ACTIONS = ("get", "history", "update", "pause", "resume", "delete", "run_now")


@pytest.mark.parametrize("mode", ["normal", "full_access"])
@pytest.mark.parametrize("action", _ACTIONS)
def test_other_group_actor_cannot_manage_known_monitor_even_with_role_claims(tmp_path, mode, action):
    agent, _source, actor, tool = group_case(tmp_path)
    job = create(tool)
    agent.config.command_access_mode = mode
    agent._current_run_params.host_channel_actor = replace(actor, actor_id="ou_other")
    agent._current_run_params.task_attributes.update(user_id="ou_a", channel_user_id="ou_a",
        is_admin=True, permission_role="admin", command_access_mode="full_access")
    before = agent.scheduler_repository.store_path.read_bytes()
    outcome = tool.execute({"action": action, "job_id": job["job_id"],
        "expected_version": job["version"], "prompt": "攻击方不能覆盖", "delivery": {"mode": "origin"}})
    if action in {'get', 'history'}:
        assert outcome.ok
    else:
        assert not outcome.ok and outcome.error_code == ('SCHEDULER_MEMBERSHIP_UNAVAILABLE' if action in {'pause', 'delete'} else 'CHANNEL_ACTOR_SCOPE_MISMATCH')
        assert job["name"] not in outcome.output and job["prompt"] not in outcome.output
    assert agent.scheduler_repository.store_path.read_bytes() == before


@pytest.mark.parametrize("operation", ["get", "history", "pause", "resume", "run", "notify"])
def test_schedule_control_refuses_other_actor_exact_id_without_mutation(tmp_path, operation):
    from agent_py_agent.tests.test_schedule_control import _agent, _job

    agent = _agent(tmp_path, group="oc_group")
    job = _job(agent, "ou_creator", "本人合成监控")
    scope = GatewayControlScope("ou_other", "feishu", "oc_group",
        resolved_owner=OwnerIdentity.provider_group("feishu", "oc_group"))
    text = f"/schedule {job['job_id']}" if operation == "get" else f"/schedule {operation} {job['job_id']}"
    if operation == "notify":
        text += " origin"
    before = agent.scheduler_repository.store_path.read_bytes()
    result = execute_schedule_control(agent, parse_conversation_control(text), scope)
    if operation in {'get', 'history'}:
        assert result.ok
    else:
        assert not result.ok and result.error_code == ('SCHEDULER_MEMBERSHIP_UNAVAILABLE' if operation == 'pause' else 'CHANNEL_ACTOR_SCOPE_MISMATCH')
        assert job["name"] not in result.message
    assert agent.scheduler_repository.store_path.read_bytes() == before


# LLM: 两个准确 owner 共享隔离数据根，只从本测试可信通道入口冻结当前 actor；不从模型字段挑目录或目标。
# 函数用途: 为跨 owner 猜编号建立真实用户/群 Agent、原 ThreadStore 与 schedule 工具。
def _external_owner(tmp_path, identity):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
        my_agent_owner_provider=identity.provider, my_agent_owner_kind=identity.owner_kind,
        my_agent_owner_id=identity.owner_id, enable_plugins=False), tmp_path)
    conversation = identity.owner_id if identity.owner_kind == "group" else "oc_private_synthetic"
    source = agent.conversation_store.threads.get_or_create({"channel": "feishu",
        "channel_conversation_id": conversation, "channel_user_id": "ou_actor", "canonical_user_id": "ou_actor",
        "owner_id": agent.home_paths.owner_id, "owner_home": str(agent.home_paths.owner_home_dir)})
    actor = actor_from_scope(agent, SimpleNamespace(channel="feishu", channel_user_id="ou_actor",
        channel_conversation_id=conversation), source.thread_id, "gwreq-" + identity.owner_id)
    agent._current_run_params = RunParams(request_id=actor.entry_request_id,
        task_attributes={"conversation_thread_id": source.thread_id}, host_channel_actor=actor)
    if identity.owner_kind == "group":
        from agent_py_agent.tests.test_group_authorization import (
            grant_synthetic_group_monitor_capability,
        )

        grant_synthetic_group_monitor_capability(agent, actor, members={actor.actor_id})
    return agent, ScheduleTool(agent)


@pytest.mark.parametrize("kind", ["user", "group"])
@pytest.mark.parametrize("action", _ACTIONS)
def test_foreign_owner_known_monitor_id_never_resolves_in_current_repository(tmp_path, kind, action):
    owner, tool = _external_owner(tmp_path, OwnerIdentity.provider_group("feishu", "oc_source"))
    job = create(tool)
    identity = (OwnerIdentity.provider_group("feishu", "oc_other") if kind == "group"
        else OwnerIdentity("feishu", "user", "ou_actor"))
    foreign, foreign_tool = _external_owner(tmp_path, identity)
    own_job = create(foreign_tool, key="own")
    original = owner.scheduler_repository.store_path.read_bytes()
    current = foreign.scheduler_repository.store_path.read_bytes()
    source_thread_id = owner.scheduler_repository.get_job(job["job_id"])["source_thread_id"]
    result = foreign_tool.execute({"action": action, "job_id": job["job_id"], "expected_version": job["version"],
        "prompt": "越权修改", "owner_id": owner.home_paths.owner_id, "source_thread_id": source_thread_id})
    assert not result.ok and result.error_code == "SCHEDULER_NOT_FOUND"
    assert owner.scheduler_repository.store_path.read_bytes() == original
    assert foreign.scheduler_repository.store_path.read_bytes() == current
    assert foreign_tool.execute({"action": "get", "job_id": own_job["job_id"]}).ok


@pytest.mark.parametrize("channel", ["feishu", "unbound-provider"])
@pytest.mark.parametrize("kind", ["user", "group"])
@pytest.mark.parametrize("lock_state", ["setup_required", "idle_locked"])
def test_locked_notification_uses_own_identity_and_never_admin(tmp_path, monkeypatch, channel, kind, lock_state):
    identity = (OwnerIdentity.provider_group("feishu", "oc_own_group") if kind == "group"
        else OwnerIdentity("feishu", "user", "ou_actor"))
    agent, _tool = _external_owner(tmp_path, identity)
    agent.config.feishu_session_lock_enabled = True
    locks = SessionLockStore(agent.home_paths.root / "session_lock" / "private_chat_locks.db")
    service = UnlockService(locks, idle_limit_seconds=60)
    if lock_state == "idle_locked":
        assert service.set_password("ou_actor", "Synthetic-Audit-Password-123")[0]
        locks.record_activity("ou_actor", now=time.time() - 180)
        assert service.status("ou_actor").locked
    else:
        assert service.status("ou_actor").requires_password_setup
    before_activity = locks.last_activity("ou_actor")
    bind_admin_channel_identity(agent.home_paths.root, "feishu", "ou_admin_synthetic")
    sent = []
    adapter = feishu.FeishuAdapter({"feishu_session_lock_enabled": True})
    adapter._get_tenant_access_token = lambda: "synthetic-token"
    monkeypatch.setattr(feishu, "_feishu_msg_api_with_retry",
        lambda method, path, body, token: sent.append((path, body)) or {"code": 0})
    agent.channel_registry.register_adapter("feishu", adapter, capabilities=ChannelCapabilities(proactive=True))
    result = execute_registry_test_call(agent.tools, "send_message", {"notify_channel": channel,
        "message": "合成合法告警"}, register_with=agent)
    if channel == "feishu":
        assert result.ok, (result.error_code, result.output)
        target_type = "chat_id" if kind == "group" else "open_id"
        assert len(sent) == 1 and sent[0][0].endswith("receive_id_type=" + target_type)
        assert sent[0][1]["receive_id"] == identity.owner_id
    else:
        # 无合法目标就不能形成业务幂等键，原 Executor 先拒绝；目标解析本身仍给准确越界原因。
        assert resolve_message_target(agent, channel)[3] == "CHANNEL_TARGET_OUT_OF_SCOPE"
        assert not result.ok and result.error_code == "TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING"
        assert not sent
    assert "ou_admin_synthetic" not in str(sent)
    assert locks.last_activity("ou_actor") == before_activity, "告警不能替用户解锁或刷新私聊活跃"


@pytest.mark.parametrize("field", ["target", "user_id", "receive_id", "owner_id", "source_thread_id"])
def test_model_cannot_add_arbitrary_delivery_target_to_native_message_tool(tmp_path, monkeypatch, field):
    agent, _tool = _external_owner(tmp_path, OwnerIdentity.provider_group("feishu", "oc_own_group"))
    sent = []
    adapter = feishu.FeishuAdapter({"feishu_session_lock_enabled": False})
    adapter._get_tenant_access_token = lambda: "synthetic-token"
    monkeypatch.setattr(feishu, "_feishu_msg_api_with_retry",
        lambda method, path, body, token: sent.append((path, body)) or {"code": 0})
    agent.channel_registry.register_adapter("feishu", adapter, capabilities=ChannelCapabilities(proactive=True))
    result = execute_registry_test_call(agent.tools, "send_message", {"notify_channel": "feishu",
        "message": "合成正文", field: "foreign-synthetic-identity"}, register_with=agent)
    assert not result.ok and result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert not sent
