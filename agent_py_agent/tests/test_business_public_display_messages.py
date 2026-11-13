"""公开sink检查点沿真实业务HTTP读取，默认历史合同不变。"""
import json
from types import SimpleNamespace

from agent_py_agent.agent.conversation.background_transcript import BackgroundTranscriptSink
from agent_py_agent.tests import test_v13_w0_observation_readers as original
from agent_py_agent.tests.test_acceptance_observation_projections import (
    background_http,
    client_for,
    isolated_home,
)

_FIXTURES = (background_http, isolated_home)


def test_original_sink_public_model_display_roundtrip_reconnect_and_actor_scope(background_http):  # noqa: F811
    services, task = original.observed_task(background_http)
    store = services.host.conversation_store
    sink = BackgroundTranscriptSink(SimpleNamespace(conversation_store=store),
        thread_id=task.target_thread_id, task_id=task.task_id)
    sink.write_thinking('excluded-thinking-canary')
    sink.write_model('真实公开进度：工具仍在执行')
    sink._flush_model_commentary(canonical_message_id='canonical-interim-1')
    sink.write_progress({'round': 1, 'call_index': 1, 'tool': 'run_command', 'phase': 'started'})
    client = client_for(background_http)
    params = {'target': {'kind': 'background_main', 'id': task.target_thread_id}, 'limit': 100}
    before = client.call('messages.list', params)
    assert before['ok'] and 'display_events' not in before['data'], before
    result = client.call('messages.list', {**params, 'include_display': True})
    assert result['ok'], result
    events = result['data']['display_events']
    assert len(events) == 1 and events[0]['payload']['text'] == '真实公开进度：工具仍在执行'
    assert events[0]['payload']['canonical_message_id'] == 'canonical-interim-1'
    assert 'excluded-thinking-canary' not in json.dumps(result, ensure_ascii=False)
    assert 'run_command' not in json.dumps(events)
    cursor, paged = None, []
    for _ in range(8):
        request = {**params, 'include_display': True, 'limit': 1}
        if cursor:
            request['cursor'] = cursor
        part = client.call('messages.list', request)
        assert part['ok'], part
        paged.extend(part['data']['display_events'])
        cursor = part['data']['next_cursor']
        if not cursor:
            break
    assert cursor is None and paged == events
    # 新客户端/宿主实例仍从同一原canonical页恢复，不依赖sink进程内环。
    reconnected = client_for(background_http).call('messages.list', {**params, 'include_display': True})
    assert reconnected['data']['display_events'] == events
    foreign = client_for(background_http, user='business-bob').call('messages.list', {**params, 'include_display': True})
    assert not foreign['ok'] and '真实公开进度' not in json.dumps(foreign, ensure_ascii=False)


def test_navigation_uses_original_display_ids_once_then_canonical_without_duplicate(monkeypatch):
    import threading

    from agent_py_agent.cli.chat_parts import tui_agent_navigation as module
    from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime('business-display')
    navigation = TuiAgentNavigationState(runtime)
    event = {'schema': 'conversation_history_display.v1', 'kind': 'assistant_completed', 'phase': 'completed',
        'request_id': 'bg-main:thread-business:original', 'block_id': 'bg-main:thread-business:original:model:1',
        'payload': {'text': 'original public progress', 'canonical_message_id': 'original-message'}}
    page = {'items': [], 'coverage': 'complete', 'next_cursor': None, 'display_events': [event]}
    navigation.business_agents = SimpleNamespace(read_denied=False, conversation=lambda *a, **k: page)
    navigation._business_targets['business:thread-business'] = {'agent_id': 'thread-business'}
    original_thread = threading.Thread
    threads = []

    def tracked(*args, **kwargs):
        thread = original_thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(module.threading, 'Thread', tracked)
    for _ in range(2):
        navigation._load_business_conversation('thread-business', runtime)
        threads[-1].join(2)
        assert not threads[-1].is_alive()
    page['items'] = [{'message_id': 'original-message', 'role': 'assistant', 'text': 'original public progress'}]
    navigation._load_business_conversation('thread-business', runtime)
    threads[-1].join(2)
    visible = [block for block in runtime.store.snapshot().stable_blocks if block.text == 'original public progress']
    assert len(visible) == 1
