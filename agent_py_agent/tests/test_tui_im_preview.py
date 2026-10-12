from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout import Layout
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.widgets import TextArea

from agent_py_agent.agent.adapter.feishu import FeishuAdapter
from agent_py_agent.agent.adapter.feishu_mentions import directly_mentions_bot
from agent_py_agent.agent.adapter.feishu_ws import lark_event_to_webhook_payload
from agent_py_agent.agent.capability.channel_message_target import resolve_message_target
from agent_py_agent.agent.conversation.control_commands import (
    ConversationTaskStatus,
    render_conversation_task_status,
)
from agent_py_agent.agent.user_space.admin_channel_identity import (
    bind_admin_channel_identity,
    remove_admin_channel_identity,
)
from agent_py_agent.cli.chat_parts.tui_application import TuiApplication


@pytest.mark.parametrize('application_class,expected_frozen', [(Application, True), (TuiApplication, False)])
def test_wall_clock_backwards_preserves_input_but_delays_old_render(application_class, expected_frozen):
    import prompt_toolkit.application.application as library

    async def scenario():
        frames = []
        with create_pipe_input() as pipe:
            area = TextArea()
            app = application_class(layout=Layout(area), input=pipe, output=DummyOutput(),
                                    min_redraw_interval=0.05, max_render_postpone_time=0.05,
                                    after_render=lambda _: frames.append((time.monotonic(), len(area.text))))
            task = asyncio.create_task(app.run_async())
            while not frames:
                await asyncio.sleep(0.001)
            original_time = library.time.time
            with patch.object(library, 'time', SimpleNamespace(time=lambda: original_time() - 0.4)):
                start = time.monotonic()
                pipe.send_text('x')
                await asyncio.sleep(0.15)
                assert area.text == 'x'  # 两者都能处理输入；卡住的是可见重绘。
                visible = any(count == 1 for _, count in frames)
                assert visible is not expected_frozen
                await asyncio.sleep(0.4)
                rendered_at = next(at for at, count in frames if count == 1)
                assert (rendered_at - start > 0.3) is expected_frozen
                app.exit()
                await task
    asyncio.run(scenario())


def test_admin_target_uses_current_binding_and_never_accepts_arbitrary_recipient(tmp_path):
    home = SimpleNamespace(root=tmp_path, owner_home_dir=tmp_path, owner_provider='local', owner_kind='main', owner_id='local/main')
    agent = SimpleNamespace(home_paths=home, config=SimpleNamespace(admin_channel_identity_enabled=True))
    assert resolve_message_target(agent, 'feishu')[3] == 'CHANNEL_ADAPTER_UNAVAILABLE'
    bind_admin_channel_identity(tmp_path, 'feishu', 'ou_admin')
    assert resolve_message_target(agent, 'feishu')[:2] == ('feishu', 'ou_admin')
    bind_admin_channel_identity(tmp_path, 'feishu', 'ou_another')
    assert resolve_message_target(agent, 'feishu')[3] == 'CHANNEL_TARGET_AMBIGUOUS'
    remove_admin_channel_identity(tmp_path, 'feishu', 'ou_another')
    home.owner_kind = 'user'
    assert resolve_message_target(agent, 'feishu')[3] == 'CHANNEL_TARGET_OUT_OF_SCOPE'
    home.owner_kind = 'main'
    assert resolve_message_target(agent, 'arbitrary-channel')[3] == 'CHANNEL_TARGET_OUT_OF_SCOPE'
    remove_admin_channel_identity(tmp_path, 'feishu', 'ou_admin')
    assert resolve_message_target(agent, 'feishu')[3] == 'CHANNEL_ADAPTER_UNAVAILABLE'


@pytest.mark.parametrize('mentions,allowed', [([], False), ([{'key':'@_all','id':{'open_id':'all'}}],False),
    ([{'key':'@_all','id':{'open_id':'ou_bot'}}],False),
    ([{'key':'@_user_1','id':{'open_id':'ou_bot'}}],True),
    ([{'key':'@_user_1','id':{'open_id':'ou_other'}}],False)])
def test_group_requires_direct_bot_mention(mentions, allowed):
    message = {'chat_type':'group','message_type':'text','content':json.dumps({'text':'hello'}),
               'message_id':'om_test','chat_id':'oc_test','mentions':mentions}
    assert directly_mentions_bot(message,'ou_bot') is allowed
    adapter = FeishuAdapter({'feishu_session_lock_enabled':False})
    adapter._resolve_bot_open_id = lambda: 'ou_bot'
    seen=[]
    adapter.on_message(seen.append)
    payload={'event':{'message':message,'sender':{'sender_id':{'open_id':'ou_sender'}}}}
    adapter._handle_feishu_event(payload)
    assert bool(seen) is allowed
    message['chat_type']='p2p'
    adapter._handle_feishu_event(payload)
    assert len(seen)==int(allowed)+1


def test_ws_preserves_mentions():
    msg=SimpleNamespace(message_id='om_test',mentions=[SimpleNamespace(key='@_user_1', id=SimpleNamespace(open_id='ou_bot'))])
    payload=lark_event_to_webhook_payload(SimpleNamespace(event=SimpleNamespace(message=msg)))
    assert payload['event']['message']['mentions']==[{'key':'@_user_1','id':{'open_id':'ou_bot'}}]


def test_status_reports_context_role_and_zero_children():
    usage={'schema':'model_visible_context_usage.v1','current_tokens':125000,'context_window_tokens':500000,
           'compact_trigger_tokens':300000,'estimated':True}
    text=render_conversation_task_status(ConversationTaskStatus(user_role='管理员',context_usage=usage, compact_generation=3))
    assert '管理员' in text and '125,000 / 500,000' in text and '25.0%' in text
    assert '子代理 0（运行 0' in text and '压缩次数：3' in text
    assert '未知' in render_conversation_task_status(ConversationTaskStatus())


@pytest.mark.skipif(__import__('os').name != 'posix', reason='POSIX TTY cleanup')
def test_exit_closes_mouse_modes_and_drains_inflight_tty_bytes():
    import contextlib
    import os
    import pty
    import select
    import termios
    import threading
    import tty

    from agent_py_agent.cli.chat_parts.tui_terminal import release_tui_mouse_input

    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)

    class Input:
        def fileno(self):
            return slave

        @contextlib.contextmanager
        def raw_mode(self):
            previous = termios.tcgetattr(slave)
            tty.setraw(slave)
            try:
                yield
            finally:
                termios.tcsetattr(slave, termios.TCSANOW, previous)

    class Output:
        def __init__(self):
            self.closed_modes=''

        def write_raw(self, value):
            self.closed_modes+=value

        def flush(self):
            pass

    output=Output()
    def inflight_mouse():
        for delay in (0.03, 0.06, 0.06):
            time.sleep(delay)
            os.write(master,b'\x1b[<35;41;18M')
    thread=threading.Thread(target=inflight_mouse)
    try:
        thread.start()
        release_tui_mouse_input(SimpleNamespace(input=Input(),output=output))
        thread.join()
        assert '\x1b[?1006l' in output.closed_modes and '\x1b[?1003l' in output.closed_modes
        restored = termios.tcgetattr(slave)
        # macOS 可自动置 PENDIN；输入交接必须恢复 canonical/echo/signal 等实际模式。
        mode_mask = termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN | termios.ECHONL
        assert restored[3] & mode_mask == original[3] & mode_mask
        assert not select.select([slave],[],[],0)[0]
        os.write(master,b'next-command\n')
        assert select.select([slave],[],[],0.2)[0]
        assert os.read(slave,100)==b'next-command\n'
    finally:
        os.close(master)
        os.close(slave)


def test_real_home_schema_marks_admin_and_routes_send_message(tmp_path):
    from agent_py_agent.agent.capability.channel_message_tool import SendMessageTool
    from agent_py_agent.agent.delivery import (
        ChannelAdapterRegistry,
        ChannelCapabilities,
        DeliveryService,
    )
    from agent_py_agent.agent.gateway_parts.status_projection import status_display_facts
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )

    home = home_paths_with_owner(home_paths(tmp_path), resolve_owner_home(tmp_path))
    home.owner_home_dir.mkdir(parents=True)
    assert home.owner_id == 'local/main'
    registry = ChannelAdapterRegistry()
    messages=[]
    adapter=SimpleNamespace(send_message=lambda target,msg: messages.append((target,msg.content)) or True)
    registry.register_adapter('feishu',adapter,capabilities=ChannelCapabilities(proactive=True))
    agent=SimpleNamespace(home_paths=home,root=home.owner_home_dir,
        config=SimpleNamespace(admin_channel_identity_enabled=True),delivery_service=DeliveryService(registry))
    assert status_display_facts(agent)['user_role']=='管理员'
    bind_admin_channel_identity(tmp_path,'feishu','ou_test_bound')
    tool=SendMessageTool(agent)
    assert tool.availability().available
    result=tool.execute({'notify_channel':'feishu','message':'原文保持不变',
        '__run_scope':{'request_id':'gw-local-admin','run_id':'run-test'},'__tool_call_id':'call-test'})
    assert result.ok
    assert messages==[('ou_test_bound','原文保持不变')]
    remove_admin_channel_identity(tmp_path,'feishu','ou_test_bound')
    assert not tool.availability().available


def test_group_post_webhook_uses_provider_message_type():
    adapter=FeishuAdapter({'feishu_session_lock_enabled':False})
    adapter._resolve_bot_open_id=lambda:'ou_bot'
    seen=[]
    adapter.on_message(seen.append)
    content={'zh_cn':{'title':'','content':[[{'tag':'at','user_id':'ou_bot'}, {'tag':'text','text':'测试群消息'}]]}}
    adapter._handle_feishu_event({'event':{'message':{'message_type':'post','chat_type':'group',
        'message_id':'om_post','chat_id':'oc_test','content':json.dumps(content)},
        'sender':{'sender_id':{'open_id':'ou_test'}}}})
    assert len(seen)==1 and '测试群消息' in seen[0].content
