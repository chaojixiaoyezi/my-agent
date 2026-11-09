"""真实Client→HTTP→原通知投影：无hub空页复用不隐藏内存、任务、跨进程写或撤权。"""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

from agent_py_agent.agent.conversation.background_transcript import (
    TranscriptSource,
    append_background_transcript_event,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.client_credentials import GatewayClientCredentials
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.cli.chat_parts.gateway_client import NetworkGatewayObserver
from agent_py_agent.tests.test_gateway_conversation_control import _free_port, _request


def test_default_hubless_http_rechecks_live_sources_and_crossprocess_append(tmp_path, monkeypatch):
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    agent = SimpleAgent(AgentConfig(model_backend='echo', gateway_per_user_owner_scoping=False), tmp_path / 'workspace')
    assert agent.config.gateway_event_hub_ring_events == 0
    assert agent.config.gateway_idle_notice_reuse_seconds > 0
    store = agent.conversation_store
    thread = store.threads.get_or_create(_request('request')['conversation'])
    page_calls = []
    original_page = store.messages.page_after_offset_report
    def page(*args, **kwargs):
        page_calls.append(kwargs)
        return original_page(*args, **kwargs)
    monkeypatch.setattr(store.messages, 'page_after_offset_report', page)
    paths = gateway_paths(agent)
    server = GatewayHTTPServer(_free_port(), paths, params=GatewayHTTPServerParams(agent=agent))
    server.start()
    headers = GatewayClientCredentials(agent.home_paths.root).headers()
    identity = {'user_id': 'u-1', 'channel': 'feishu'}
    client = NetworkGatewayObserver(SimpleNamespace(config=agent.config,
        gateway_request_identity=lambda: dict(identity),
        gateway_network_headers=lambda: headers), f'http://127.0.0.1:{server.port}')
    body = {'conversation_id': 'c-1', 'after': 0, 'event_after': 0, 'client_capabilities': {'tool_approval': True}}
    def poll():
        status, payload = client.request_json('POST', '/client/notices', body)
        assert status == 200 and payload['ok'], (status, payload)
        return payload
    try:
        assert server.request_event_hub is None
        assert poll()['notices'] == poll()['notices'] == []
        assert len(page_calls) == 1
        store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'task-active', 'goal': 'test', 'now': 2})
        from agent_py_agent.agent.conversation.agent_activity import (
            MainActivitySource,
            publish_main_activity,
        )
        publish_main_activity(agent, MainActivitySource(thread.thread_id, 'task-active', 2, 'projection'), phase='running', begin=True)
        assert poll()['active_task_count'] == 1
        store.tasks.update_status({'task_id': 'task-active', 'status': 'cancelled', 'expected_status': 'active'})
        assert poll()['active_task_count'] == 0
        append_background_transcript_event(agent, source=TranscriptSource(thread.thread_id, '', 'notice-run'),
            kind='system_message', phase='completed', block_id='new-event', payload={'text': 'memory event'})
        assert [event['block_id'] for event in poll()['transcript_events']] == ['new-event']
        assert len(page_calls) == 1  # 以上逐请求重核并未把空消息页重复打开。
        script = '''
import sys
from agent_py_agent.agent.conversation.store import ConversationStore
store=ConversationStore(sys.argv[1],initialize=False)
store.messages.append({'thread_id':sys.argv[2],'role':'assistant','content':'new crossprocess notice',
'metadata':{'background_delivery_reason':'root_subagents_terminal','assistant_part_id':'final'}})
'''
        subprocess.run([sys.executable, '-c', script, str(store.storage.root), thread.thread_id], check=True, timeout=30)
        assert [notice['content'] for notice in poll()['notices']] == ['new crossprocess notice']
        assert len(page_calls) == 2
        identity['user_id'] = 'u-2'
        other_actor = poll()
        assert other_actor['notices'] == [] and other_actor['thread_id'] == ''
        identity['user_id'] = 'u-1'
        server.auth_middleware.set_local_client_credential('revoked-original-local-credential')
        status, payload = client.request_json('POST', '/client/notices', body)
        assert status == 403 and payload == {}  # 真实Client对HTTPError保留状态码、不解析拒绝正文。
        assert len(page_calls) == 2
    finally:
        server.stop()
