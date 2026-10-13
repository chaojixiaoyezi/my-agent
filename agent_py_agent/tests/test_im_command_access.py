"""真实入站转换、命令目录权限和控制帮助的组合回归，不发送外部消息。"""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.adapter.feishu_mentions import directly_mentions_bot
from agent_py_agent.agent.adapter.protocol import feishu_to_incoming
from agent_py_agent.agent.command_catalog import (
    COMMAND_INDEX,
    CommandAccessContext,
    command_access_error,
    render_command_help,
    sensitive_command_name,
)
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control


# LLM: 平台身份、提及及消息类型是合成结构事实，测试不读取真实会话或凭据。
# 函数用途: 构造相同群来源的 text/post 消息，覆盖敏感命令前置提及与其它用户参数。
def _payload(kind, body, mentions):
    return {"event": {"sender": {"sender_id": {"open_id": "ou_user"}}, "message": {
        "chat_type": "group", "chat_id": "oc_group", "message_id": "om_command",
        "message_type": kind, "content": json.dumps(body), "mentions": mentions,
    }}}


@pytest.mark.parametrize("command", ["/help", "/verbose stream", "/admin synthetic-password"])
@pytest.mark.parametrize("kind", ["text", "post"])
def test_bot_prefix_reaches_original_command_and_secret_guard(kind, command):
    mentions = [{"key": "@_user_1", "id": {"open_id": "ou_bot"}}]
    body = {"text": "@_user_1 " + command} if kind == "text" else {
        "zh_cn": {"content": [[{"tag": "at", "user_id": "ou_bot"}, {"tag": "text", "text": " " + command}]]}}
    payload = _payload(kind, body, mentions)
    assert directly_mentions_bot(payload["event"]["message"], "ou_bot")
    incoming = feishu_to_incoming(payload, bot_open_id="ou_bot")
    assert incoming.content == command
    assert parse_conversation_control(incoming.content) is not None
    assert bool(sensitive_command_name(incoming.content)) == command.startswith("/admin")


def test_mentions_never_use_names_or_remove_other_parameters():
    payload = _payload("text", {"text": "@_user_1 /help @_user_2"}, [
        {"key": "@_user_1", "open_id": "ou_bot"}, {"key": "@_user_2", "open_id": "ou_other"}])
    assert feishu_to_incoming(payload, bot_open_id="ou_bot").content == "/help @_user_2"
    payload["event"]["message"]["mentions"] = [{"key": "@_all", "id": {"open_id": "ou_bot"}}]
    assert not directly_mentions_bot(payload["event"]["message"], "ou_bot")


def test_role_help_and_execution_use_same_metadata():
    user = CommandAccessContext("im", False, True)
    admin = CommandAccessContext("im", True, True)
    ordinary = render_command_help(user)
    elevated = render_command_help(admin)
    for name in ("status", "verbose", "help", "delegate"):
        assert f"/{name}" in ordinary
        assert not command_access_error(COMMAND_INDEX[name], user)
    for name in ("restart", "endtask", "wakes", "plugins"):
        assert f"/{name}" not in ordinary
        assert command_access_error(COMMAND_INDEX[name], user) == "COMMAND_ADMIN_REQUIRED"
        assert f"/{name}" in elevated
    assert "/recover owner" not in render_command_help(user, "recover")
    assert "/recover owner" in render_command_help(admin, "recover")
    assert "/verbose stream" in render_command_help(user, "verbose")


def test_locked_help_does_not_show_business_controls():
    locked = render_command_help(CommandAccessContext("im", False, True, True))
    assert "/help" in locked and "/status" not in locked and "/verbose" not in locked
    assert "解锁" in locked
    assert command_access_error(COMMAND_INDEX["admin"], CommandAccessContext("im", False, False)) == "COMMAND_PRIVATE_CHAT_REQUIRED"


def test_help_is_explicit_control_with_single_optional_topic():
    assert parse_conversation_control("/help verbose").kind == "help"
    assert parse_conversation_control("/help verbose").value == "verbose"
    assert not parse_conversation_control("/help verbose stream").valid
    assert parse_conversation_control("请帮我查看状态") is None


@pytest.mark.parametrize("is_admin", [False, True])
def test_gateway_help_control_uses_authenticated_owner_without_model_call(tmp_path, is_admin):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.control_service import (
        GatewayControlScope,
        execute_gateway_conversation_control,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    scope = GatewayControlScope("ou_user", "feishu", "oc_private", metadata={"feishu_chat_type": "p2p"},
        resolved_owner=OwnerIdentity.local_main() if is_admin else OwnerIdentity.provider_user("feishu", "ou_user"))
    result = execute_gateway_conversation_control(agent, gateway_paths(agent), parse_conversation_control("/help"), scope)
    assert result.ok and result.kind == "help"
    assert ("/restart" in result.message) == is_admin
