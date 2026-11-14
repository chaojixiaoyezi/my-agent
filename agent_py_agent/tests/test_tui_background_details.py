"""后台详情只在显式打开时查询，异步返回不随导航改投目标。"""
import asyncio
from types import SimpleNamespace

from prompt_toolkit.buffer import Buffer

from agent_py_agent.cli.chat_parts.tui_background_agents import (
    TuiBackgroundAgents,
    submit_business_input,
)
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime


def test_details_freezes_target_and_reads_only_on_explicit_request():
    class Client:
        def __init__(self):
            self.calls = []

        def call(self, action, params):
            self.calls.append((action, params))
            if action == 'background_agents.get':
                return {'ok': True, 'data': {'current_execution': None,
                    'scheduling': {'state': 'waiting', 'reason_code': 'DEPENDENCY_MISSING'}}}
            if action == 'usage.get':
                return {'ok': False, 'http_status': 503,
                    'error': {'code': 'USAGE_SOURCE_UNAVAILABLE', 'message': '计量来源暂不可读'}}
            if action == 'agent_tree.get':
                return {'ok': True, 'data': {'coverage': 'complete', 'nodes': [
                    {'id': 'agent-original', 'thread_id': 'thread-original', 'space_id': 'space-original'}],
                    'edges': [], 'next_cursor': None}}
            raise AssertionError(action)

    client = Client()
    projection = TuiBackgroundAgents(client)
    runtime = TuiRuntime('original')
    selected = SimpleNamespace(active_agent_id='agent-original', active_thread_id='thread-original',
        business_space_id='space-original')
    nav = SimpleNamespace(snapshot=lambda: selected, active_runtime=lambda: runtime, business_agents=projection)
    pending = []
    event = SimpleNamespace(app=SimpleNamespace(create_background_task=pending.append, invalidate=lambda: None))
    params = SimpleNamespace(agent_navigation=nav, input_area=SimpleNamespace(buffer=Buffer()))
    assert not client.calls
    assert submit_business_input(event, params, '/details')
    assert not client.calls  # 查询未在 UI 线程同步执行。
    selected = SimpleNamespace(active_agent_id='other', active_thread_id='other', business_space_id='other')
    asyncio.run(pending.pop())
    assert ('usage.get', {'filter': {'agent_id': 'agent-original', 'space_id': 'space-original'}}) in client.calls
    assert all(params.get('agent_id', 'agent-original') == 'agent-original'
               for action, params in client.calls)
    assert not any(action.endswith(('.send', '.create', '.set', '.cancel')) for action, _ in client.calls)
    rendered = str(runtime.store.snapshot())
    assert 'DEPENDENCY_MISSING' in rendered and 'USAGE_SOURCE_UNAVAILABLE' in rendered
    assert '未知值保留' in rendered
