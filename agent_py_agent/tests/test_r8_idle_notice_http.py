"""真实Client→HTTP→原通知投影：无hub空页复用不隐藏内存、任务、跨进程写或撤权。"""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

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


@pytest.mark.parametrize('retained', [False, True])
def test_default_http_activity_uses_one_display_thread_generation(tmp_path, monkeypatch, retained, request):
    import inspect
    import json
    import threading
    from dataclasses import replace

    from agent_py_agent.agent.agent_core.runtime.task_identity import task_path_progress_ledger_id
    from agent_py_agent.agent.gateway_parts import http_handlers
    from agent_py_agent.agent.task_progress import (
        with_task_progress_display_plan,
        write_task_progress,
    )
    from agent_py_agent.tests.test_conversation_context_usage import usage

    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    agent = SimpleAgent(AgentConfig(model_backend='echo', gateway_per_user_owner_scoping=False), tmp_path / 'workspace')
    store = agent.conversation_store
    thread = store.threads.get_or_create(_request('request')['conversation'])
    for generation in (1, 2):
        task_id = f'task-{generation}'
        task_path = str(tmp_path / f'task-{generation}')
        store.tasks.bind({'thread_id': thread.thread_id, 'task_id': task_id, 'goal': 'display',
                          'task_path': task_path, 'now': generation})
        write_task_progress(agent.home_paths.owner_home_dir, task_path_progress_ledger_id(task_path),
            with_task_progress_display_plan({'items': [{'id': 'item', 'title': 'display', 'status': 'pending'}]},
                                            generation_id=f'turn-{generation}', item_ids=['item']))
        if retained:
            store.tasks.update_status({'task_id': task_id, 'status': 'completed', 'expected_status': 'active'})
    base = store.threads.load(thread.thread_id)

    def version(generation):
        return replace(base, workspace_task_id=f'task-{generation}', compact_generation=generation,
            model_context_usage={**usage(42_100 + generation), 'compact_generation': generation},
            model_metrics={'schema': 'model_runtime_metrics.v1', 'model_rounds': generation})

    store.threads.write(version(1))
    projection = threading.local()
    observations = []
    original_activity = http_handlers.conversation_agent_activity
    original_load = store.threads.load_report
    display_callers = {'_conversation_context_state', 'model_metrics_from_thread',
                       '_conversation_workspace_task_id', '_retained_workspace_task_link',
                       '_conversation_display_thread'}
    changed = []

    def load(thread_id):
        result = original_load(thread_id)
        if getattr(projection, 'active', False) and thread_id == thread.thread_id:
            observations[-1]['all'] += 1
            if inspect.currentframe().f_back.f_code.co_name in display_callers:
                observations[-1]['display'] += 1
        return result

    def activity(*args, **kwargs):
        observations.append({'all': 0, 'display': 0})
        projection.active = True
        if not changed:
            changed.append(True)
            store.threads.write(version(2))
        try:
            return original_activity(*args, **kwargs)
        finally:
            projection.active = False

    monkeypatch.setattr(store.threads, 'load_report', load)
    monkeypatch.setattr(http_handlers, 'conversation_agent_activity', activity)
    server = GatewayHTTPServer(_free_port(), gateway_paths(agent), params=GatewayHTTPServerParams(agent=agent))
    server.start()
    headers = GatewayClientCredentials(agent.home_paths.root).headers()
    client = NetworkGatewayObserver(SimpleNamespace(config=agent.config,
        gateway_request_identity=lambda: {'user_id': 'u-1', 'channel': 'feishu'},
        gateway_network_headers=lambda: headers), f'http://127.0.0.1:{server.port}')
    body = {'conversation_id': 'c-1', 'after': 0, 'event_after': 0}
    try:
        assert server.request_event_hub is None and 'wait' not in body
        for generation in (1, 2):
            status, payload = client.request_json('POST', '/client/notices', body)
            assert status == 200 and payload['ok'], (status, payload)
            shown = payload['agent_activity']
            assert shown['compact_count'] == generation
            assert shown['context_usage']['current_tokens'] == 42_100 + generation
            assert shown['model_metrics']['model_rounds'] == generation
            assert shown['task_progress_generation_id'] == f'turn-{generation}'
            assert observations[-1]['display'] == 0  # 复用本拍绑定已核线程，不再重读展示正文。
            assert observations[-1]['all'] > 1  # 原任务/claim授权仍独立读取，不借展示快照授权。
        store.storage.thread_path(thread.thread_id).write_bytes(b'{broken')
        status, payload = client.request_json('POST', '/client/notices', body)
        assert status == 200 and payload['ok'] is False
        assert payload.get('agent_activity', {}).get('model_metrics', {}) == {}
    finally:
        request.node.user_properties.append(('activity_thread_load_counts', json.dumps(observations)))
        server.stop()


def test_activity_reuses_only_same_thread_display_record(tmp_path, monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.conversation.agent_activity import conversation_agent_activity
    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(tmp_path / 'conversations')
    thread = store.threads.get_or_create({'canonical_user_id': 'alice'})
    current = replace(thread, compact_generation=3)
    store.threads.write(current)
    reads = []
    original = store.threads.load_report

    def load(thread_id):
        reads.append(thread_id)
        return original(thread_id)

    monkeypatch.setattr(store.threads, 'load_report', load)
    agent = SimpleNamespace()
    snapshot = replace(current, compact_generation=2)
    # 本拍展示可取已核快照；任务活动仍读取当前规范账，下一拍不复用旧值。
    shown = conversation_agent_activity(agent, store, thread.thread_id, display_thread=snapshot)
    snapshot_reads = len(reads)
    assert shown.compact_count == 2
    reads.clear()
    shown = conversation_agent_activity(agent, store, thread.thread_id)
    assert shown.compact_count == 3
    assert len(reads) == snapshot_reads + 1
    reads.clear()
    foreign = replace(snapshot, thread_id='thread-foreign')
    shown = conversation_agent_activity(agent, store, thread.thread_id, display_thread=foreign)
    assert shown.compact_count == 3
    assert len(reads) == snapshot_reads + 1
