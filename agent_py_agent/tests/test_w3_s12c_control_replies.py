"""S12c: control replies in the bound admin's Full Access private chat keep full host paths; others keep basenames.

Live clone (2026-11-17, Wave F): in the bound admin's Full Access p2p, final replies and progress cards kept full
paths, but /dir answered with the basename note and /sessions control <thread> /status showed only file names.
Control results never carried the viewer's S12 fact, /status rendering always shortened paths, and the adapter's
direct control reply ignored the receipt's display flag.
"""
from __future__ import annotations

import time

import pytest

from agent_py_agent.agent.adapter.ingress import GatewayIngressRecord
from agent_py_agent.agent.adapter.manager import _ChannelIngressCoordinator
from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    execute_gateway_conversation_control,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.approval_mode import execute_approval_mode_operation
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests.test_admin_identity_clients import _manager, _wait_for_reply

_PATH = "/Users/alice/projects/heron/src/lexer.py"

# (user, chat type, /permissions choice, full paths expected)
_VIEWERS = [
    ("ou_admin", "p2p", None, True),
    ("ou_admin", "p2p", "full-access", True),
    ("ou_admin", "group", None, False),
    ("ou_other", "p2p", None, False),
    ("ou_admin", "p2p", "ask", False),
]


# 函数用途: 本机 full-access、开启管理员私聊绑定的真实 Agent（echo 后端，不调模型）；mode 为管理员 /permissions 选择。
def _agent(tmp_path, mode):
    root = tmp_path / "home"
    config = AgentConfig(model_backend="echo", my_agent_home=str(root), enable_plugins=False,
                         gateway_per_user_owner_scoping=False, admin_channel_identity_enabled=True,
                         access_mode="full-access")
    agent = SimpleAgent(config, resolve_owner_home(root, OwnerIdentity.local_main()).home_dir)
    agent.backend = get_backend("echo", agent.config)
    bind_admin_channel_identity(agent.home_paths.root, "feishu", "ou_admin")
    if mode is not None:
        main = home_paths_with_owner(home_paths(agent.home_paths.root),
                                     resolve_owner_home(agent.home_paths.root, OwnerIdentity.local_main()))
        execute_approval_mode_operation(main, "set", mode)
    return agent


def _scope(user, chat, conversation):
    return GatewayControlScope(user, "feishu", conversation, metadata={"channel_chat_type": chat})


# 函数用途: 给 (user, conversation) 的飞书会话挂一条目标里带宿主绝对路径的持久主任务，供 /status 展示。
def _bind_task(agent, task_id, user, conversation):
    thread = agent.conversation_store.threads.get_or_create({
        "canonical_user_id": user, "channel": "feishu", "channel_conversation_id": conversation,
        "channel_user_id": user, "owner_id": agent.home_paths.owner_id,
        "owner_home": str(agent.home_paths.owner_home_dir), "now": time.time() - 40})
    agent.conversation_store.tasks.bind({
        "thread_id": thread.thread_id, "task_id": task_id, "goal": f"整理 {_PATH} 的词法规则",
        "status": "active", "task_path": "", "now": time.time() - 30})
    return thread


def _run(agent, text, scope):
    command = parse_conversation_control(text, reject_unknown_slash=True)
    assert command is not None and command.valid, text
    return execute_gateway_conversation_control(agent, gateway_paths(agent), command, scope)


@pytest.mark.parametrize(("user", "chat", "mode", "full"), _VIEWERS)
def test_status_reply_keeps_full_paths_only_for_the_admin_full_access_private_chat(tmp_path, user, chat, mode, full):
    agent = _agent(tmp_path, mode)
    conversation = "oc_group" if chat == "group" else "oc_p2p"
    _bind_task(agent, "req-s12c-status", user, conversation)
    result = _run(agent, "/status", _scope(user, chat, conversation))
    assert result.ok is True and result.status is not None and result.status.task, result
    assert (_PATH in result.message) is full and "lexer.py" in result.message
    payload = result.to_dict()
    assert payload.get("host_path_display") == ("full" if full else None)
    assert (_PATH in payload["task_status"]["task"]) is full and "lexer.py" in payload["task_status"]["task"]


@pytest.mark.parametrize(("user", "chat", "mode", "full"), _VIEWERS)
def test_dir_reply_reaches_the_im_exit_with_the_viewers_display(tmp_path, user, chat, mode, full):
    agent = _agent(tmp_path, mode)
    conversation = "oc_group" if chat == "group" else "oc_p2p"
    result = _run(agent, "/dir", _scope(user, chat, conversation))
    assert result.ok is True and result.kind == "dir", result
    shown_dir = result.message.split("：", 1)[1].strip()  # a new thread shows the owner home
    assert shown_dir.startswith("/") and shown_dir.count("/") > 2, result.message
    receipt = {**result.to_dict(), "status": "control", "disposition": "system_command",
               "operation_id": "gwctl-msg-" + "1" * 32, "control_state": "completed", "delivery_status": "accepted"}
    assert receipt.get("host_path_display") == ("full" if full else None)

    manager, adapter = _manager(tmp_path / "adapter")
    record = GatewayIngressRecord("ing-s12c", "feishu", user, conversation, "om-s12c-dir", "0" * 64, "/dir",
                                  {"feishu_chat_type": chat, "feishu_chat_id": conversation},
                                  state="submitted", submission=receipt)
    assert _ChannelIngressCoordinator(manager).advance_submission(record).state == "completed"
    text = _wait_for_reply(adapter)
    assert (shown_dir in text) is full
    assert shown_dir.rstrip("/").rsplit("/", 1)[-1] in text


def test_sessions_control_status_follows_the_viewer_not_the_target_binding(tmp_path):
    agent = _agent(tmp_path, None)
    _bind_task(agent, "req-s12c-other", "ou_admin", "oc_other_chat")
    target = agent.conversation_store.threads.resolve(channel="feishu", channel_conversation_id="oc_other_chat",
                                                      channel_user_id="ou_admin")
    viewer = _scope("ou_admin", "p2p", "oc_p2p")
    result = _run(agent, f"/sessions control {target.thread_id} /status", viewer)
    assert result.ok is True and result.details.get("cross_entry") is True, result
    assert _PATH in result.message and result.to_dict().get("host_path_display") == "full"

    grouped = _run(agent, f"/sessions control {target.thread_id} /status", _scope("ou_admin", "group", "oc_group"))
    assert _PATH not in grouped.message and "host_path_display" not in grouped.to_dict()
