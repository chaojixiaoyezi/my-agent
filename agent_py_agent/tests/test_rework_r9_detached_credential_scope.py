"""R9-22：detached 任务的审批、凭据与自学习授权读持久 TaskLink 声明的原 detached claim 车道；旧代、已取消与外来车道一律拒绝。

真实 SimpleAgent、ConversationStore、TaskLink、claim、RuntimeDB、后台 control 与 core 身份发布；只替换 IM 运输。
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.conversation.agent_tool_approval import _agent_approval_session_scope
from agent_py_agent.agent.conversation.run_claim import detached_task_claim_scope_id
from agent_py_agent.agent.conversation.tool_approval_scope import (
    tool_approval_scope,
    tool_approval_scope_is_current,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.skill_summarize_tool import _authorized_learning_scope
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.task_credentials import (
    credential_headers,
    credential_path,
    request_credential_card,
)

_URL = "https://example.com/api"


# LLM: 同一线程上另有前台车道 claim（用户继续聊天），detached 任务自己的 claim 在原 detached 车道；二者互不授权。
# 函数用途: 建立一个已领取 detached 车道的后台任务和同线程前台任务。
@pytest.fixture
def detached(tmp_path):
    from agent_py_agent.agent.user_space.approval_mode import execute_approval_mode_operation

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    execute_approval_mode_operation(agent.home_paths, "set", "ask")
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "local-agent", "channel": "chat", "owner_id": agent.home_paths.owner_id,
        "channel_conversation_id": "session-main", "channel_user_id": "local-agent",
    })
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "chat-work", "goal": "继续前台对话", "status": "active"})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "detached-work", "goal": "执行后台独立工作",
                      "status": "active", "cancellation_scope": "detached"})
    lane = detached_task_claim_scope_id(thread.thread_id, "detached-work")
    foreground = store.claims.acquire({"thread_id": thread.thread_id, "task_id": "chat-work", "reason": "thread_goal_continue"})
    claim = store.claims.acquire({"thread_id": thread.thread_id, "claim_scope_id": lane, "task_id": "detached-work",
                                  "reason": "thread_goal_continue"})
    assert claim and foreground and claim["claim_id"] != foreground["claim_id"]
    return SimpleNamespace(agent=agent, store=store, thread=thread, task_id="detached-work", lane=lane,
                           claim=claim, foreground=foreground)


# LLM: 沿生产后台 control、core binder 与原 claim publisher 在 detached 车道发布身份；RuntimeDB 决定代次。
# 函数用途: 让当前测试线程成为这条 detached 任务的真实热执行片。
def _bind_detached(ctx, *, lane=None, claim_id=None):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.agent_core.runtime_mixin import _bind_main_agent_authority
    from agent_py_agent.agent.conversation.background_execution import _BackgroundRunControl
    from agent_py_agent.agent.conversation.claim_execution_binding import (
        ClaimExecutionBinding,
        bind_claim_execution,
    )

    control = _BackgroundRunControl("detached-source", None)
    binding = ClaimExecutionBinding(ctx.store.claims, ctx.thread.thread_id, claim_id or ctx.claim["claim_id"],
                                    ctx.lane if lane is None else lane)
    with bind_claim_execution(binding):
        params = _bind_main_agent_authority(ctx.agent, RunParams(
            request_id="detached-source", run_id="detached-core", task_id=ctx.task_id,
            task_attributes={"conversation_thread_id": ctx.thread.thread_id, "conversation_task_id": ctx.task_id},
            conversation_task_binding_callback=control))
    ctx.agent._current_run_params = params
    return params


def _learning_service(agent):
    home = agent.home_paths
    return SimpleNamespace(store=SimpleNamespace(directory=Path(home.owner_skill_learning_dir),
                                                 skills_root=Path(home.owner_home_dir) / "skills"))


# LLM: 合成凭据只经原私人卡片发布与提交入口落盘；运输只记录卡片，不访问网络。
# 函数用途: 在当前 detached 执行里取得一张可用的 basic 凭据引用。
def _ready_credential(ctx):
    ctx.agent.config.feishu_session_lock_enabled = False
    bind_admin_channel_identity(ctx.agent.home_paths.root, "feishu", "ou_synthetic")

    def send(_target, _card, **_kwargs):
        return {"message_id": "om_" + __import__("uuid").uuid4().hex, "chat_id": "oc_private"}

    ctx.agent.channel_registry.register_adapter("feishu", SimpleNamespace(send_control_card=send),
                                                capabilities=ChannelCapabilities(control_cards=True, proactive=True))
    reference, _ = request_credential_card(ctx.agent, url=_URL, authentication="basic")
    row = read_json_object_report(credential_path(ctx.agent.home_paths.owner_home_dir, reference), context="test").payload
    body = {"reference": row["reference"], "decision": "submit", "message_id": row["message_id"],
            "chat_id": row["chat_id"], "fields": {"username": "synthetic-user", "secret": "Synthetic-API-Secret-123"}}
    assert resolve_private_interaction(ctx.agent, body, channel="feishu", actor_id="ou_synthetic")["ok"]
    return reference


def _consumers_allowed(ctx, scope, reference):
    attrs = ctx.agent._current_run_params.task_attributes
    allowed = {
        "scope": scope.active and tool_approval_scope_is_current(ctx.agent, scope),
        "approval_session": bool(_agent_approval_session_scope(ctx.agent, scope)),
        "learning": _authorized_learning_scope(ctx.agent, _learning_service(ctx.agent), attrs),
    }
    try:
        allowed["credential"] = bool(credential_headers(ctx.agent, reference, _URL)[0])
    except ValueError:
        allowed["credential"] = False
    return allowed


def test_current_detached_execution_reads_its_own_claim_lane(detached):
    params = _bind_detached(detached)
    scope = tool_approval_scope(detached.agent, params.run_id)
    assert scope.active
    assert (scope.root_task_id, scope.thread_id) == (detached.task_id, detached.thread.thread_id)
    assert scope.claim_id == detached.claim["claim_id"] != detached.foreground["claim_id"]
    assert scope.execution_attempt_id == params.attempt_id
    reference = _ready_credential(detached)
    assert _consumers_allowed(detached, scope, reference) == {
        "scope": True, "approval_session": True, "learning": True, "credential": True}
    # cold 读者只按同一 detached 定位复读原发布身份，不从当前参数补猜。
    hot = detached.agent._current_run_params
    del detached.agent._current_run_params
    cold = tool_approval_scope(detached.agent, detached.task_id)
    assert cold.active and cold == scope
    detached.agent._current_run_params = hot
    # 前台车道仍只属于前台任务，不被 detached 身份借用。
    assert not tool_approval_scope(detached.agent, "chat-work").active


@pytest.mark.parametrize("case", ["stale_attempt", "expired_claim", "cancelled_task", "finished_claim",
                                  "foreign_lane", "foreground_link", "foreign_thread"])
def test_stale_cancelled_or_foreign_detached_execution_is_denied(detached, case):
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic

    params = _bind_detached(detached)
    reference = _ready_credential(detached)
    path = detached.store.claims.storage.background_claim_path(detached.lane)
    if case == "stale_attempt":
        repo = detached.agent.subagents.runtime_db
        assert repo.create_attempt(repo.agent_run_for_run_id(params.run_id)["agent_run_id"])["attempt_id"] != params.attempt_id
    elif case == "expired_claim":
        update_json_file_atomic(path, lambda value: {**value, "expires_at": 1.0})
    elif case == "cancelled_task":
        detached.store.tasks.update_status({"task_id": detached.task_id, "status": "cancelled"})
    elif case == "finished_claim":
        assert detached.store.claims.finish({"thread_id": detached.thread.thread_id, "claim_scope_id": detached.lane,
                                             "claim_id": detached.claim["claim_id"], "status": "finished"})
    elif case == "foreign_lane":
        # 同一 detached 任务的身份若落在线程前台车道，不能被扫描借用：定位只认持久链接推出的 detached 车道。
        thread_id = detached.thread.thread_id
        for lane, claim in ((detached.lane, detached.claim), ("", detached.foreground)):
            assert detached.store.claims.finish({"thread_id": thread_id, "claim_scope_id": lane,
                                                 "claim_id": claim["claim_id"], "status": "finished"})
        borrowed = detached.store.claims.acquire({"thread_id": thread_id, "task_id": detached.task_id,
                                                  "reason": "thread_goal_continue"})
        params = _bind_detached(detached, lane="", claim_id=borrowed["claim_id"])
    elif case == "foreground_link":
        # 持久链接不是 detached 时不推出 detached 车道，原线程车道属于前台任务。
        update_json_file_atomic(detached.store.storage.task_path(detached.task_id),
                                lambda value: {**value, "cancellation_scope": "foreground"})
    elif case == "foreign_thread":
        detached.agent._current_run_params = replace(params, task_attributes={
            **params.task_attributes, "conversation_thread_id": "thread-foreign"})
    scope = tool_approval_scope(detached.agent, detached.task_id)
    allowed = _consumers_allowed(detached, scope, reference)
    assert allowed == {"scope": False, "approval_session": False, "learning": False, "credential": False}, allowed
