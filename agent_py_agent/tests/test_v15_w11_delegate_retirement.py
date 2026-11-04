"""delegate退役入口不再读写偏好或启动任何执行。"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.command_catalog import CommandAccessContext, render_command_help
from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlCommand,
    parse_conversation_command,
)
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.delegation_control import execute_delegation_control
from agent_py_agent.tests.test_im_execution_threads import fixture_agent


# LLM: pytest为各旧delegate写法构造临时会话并调用退役控制入口；核对仅返回route迁移提示、线程字节不变且不创建旧偏好文件，不以迁移回执当执行成功。
# 函数用途: 检查所有旧委派命令都只提示迁移，不再保存开关。
@pytest.mark.parametrize('suffix', ['', 'enable', 'disable', 'status', 'true', 'enable other', 'arbitrary'])
def test_every_delegate_form_is_migration_only(tmp_path, suffix):
    agent, thread = fixture_agent(tmp_path)
    before = agent.conversation_store.storage.thread_path(thread.thread_id).read_bytes()
    command = parse_conversation_command('/delegate' + (' ' + suffix if suffix else ''))
    assert isinstance(command, ConversationControlCommand) and command.kind == 'delegate'
    result = execute_delegation_control(agent, command, GatewayControlScope('ou_a', 'feishu', 'oc_source'))
    assert '/route' in result.message and not result.ok
    assert agent.conversation_store.storage.thread_path(thread.thread_id).read_bytes() == before
    assert not (agent.home_paths.owner_home_dir / 'config' / 'im_preferences.json').exists()


# LLM: pytest只渲染普通IM私聊身份的静态帮助文本，检查唯一公开路由开关为route；不读写偏好、创建任务或连接IM。
# 函数用途: 确认帮助不再宣传delegate或另一个router命令。
def test_help_has_one_route_switch_and_no_delegate_advertisement():
    help_text = render_command_help(CommandAccessContext(surface='im', is_admin=False, private_chat=True))
    assert '/route' in help_text
    assert '/delegate' not in help_text
    assert '/router' not in help_text
