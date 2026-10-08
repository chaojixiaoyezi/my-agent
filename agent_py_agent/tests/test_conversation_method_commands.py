"""会话沿用命令：真实线程存储与 owner 裁决，命令不构造 Agent、不读方法正文。"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    execute_gateway_conversation_control,
)
from agent_py_agent.cli.chat_parts.control_runtime import _command_text
from agent_py_agent.tests.test_conversation_method_carry import (
    method_fixture,
    read_package,
    records,
    switch,
)


def _scope(fixture):
    thread = fixture.agent.conversation_store.threads.require(fixture.link.thread_id)
    binding = thread.channel_bindings[0]
    return GatewayControlScope(binding.channel_user_id, binding.channel, binding.channel_conversation_id)


def _control(fixture, text, scope=None):
    command = parse_conversation_control(text, reject_unknown_slash=True)
    return execute_gateway_conversation_control(fixture.agent, None, command, scope or _scope(fixture))


@pytest.mark.parametrize("text,operation,valid", [
    ("/skills using", "using", True),
    ("/skills Using REMOVE My-Method", "using_remove", True),
    ("/skills using remove", "using_remove", False),
    ("/skills using unknown x", "unknown", False),
])
def test_using_parser_roundtrip_preserves_method_identity(text, operation, valid):
    command = parse_conversation_control(text, reject_unknown_slash=True)
    assert (command.operation, command.valid) == (operation, valid)
    assert parse_conversation_control(_command_text(command), reject_unknown_slash=True) == command
    if valid and operation == "using_remove":
        assert command.value == "using remove My-Method"


def test_using_lists_package_resource_count_and_removes_only_current_thread(tmp_path):
    fixture = method_fixture(tmp_path)
    assert read_package(fixture, path="methods/2.md").ok
    listing = _control(fixture, "/skills using")
    assert listing.ok and "entry-0" in listing.message and "资料 1 份" in listing.message
    assert str(tmp_path) not in listing.message
    absent = _control(fixture, "/skills using remove missing-name")
    assert absent.ok is False and len(records(fixture)) == 1
    scope = _scope(fixture)
    other = GatewayControlScope(scope.user_id, scope.channel, "another-conversation",
                                metadata={"conversation_thread_id": fixture.link.thread_id})
    denied = _control(fixture, "/skills using remove entry-0", other)
    assert denied.ok is False and len(records(fixture)) == 1
    removed = _control(fixture, "/skills using remove entry-0")
    assert removed.ok and records(fixture) == ()
    assert _control(fixture, "/skills using").message == "本会话没有在用的方法。"
    assert read_package(fixture).ok, "移除沿用不得停用包或更改原授权"


@pytest.mark.parametrize("text", ["/skills using", "/skills using remove entry-0"])
def test_off_switch_commands_report_closed_without_changing_ledger(tmp_path, text):
    fixture = method_fixture(tmp_path)
    assert read_package(fixture).ok
    before = records(fixture)
    switch(fixture, False)
    result = _control(fixture, text)
    assert result.ok and result.message == "会话沿用已关闭"
    assert records(fixture) == before
