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
        paged = part['data']['display_events'] + paged
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


def test_workflow_steps_roundtrip_after_finish_exclude_forged_source_and_other_threads(background_http, monkeypatch):  # noqa: F811
    import copy
    import threading

    from agent_py_agent.cli.chat_parts import tui_agent_navigation as module
    from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    services, task = original.observed_task(background_http)
    store, client = services.host.conversation_store, client_for(background_http)
    sink = BackgroundTranscriptSink(SimpleNamespace(conversation_store=store),
        thread_id=task.target_thread_id, task_id=task.task_id)
    event = {'workflow_run_id': 'workflow-run', 'workflow_id': 'workflow', 'step_id': 'step',
             'name': '只读核对', 'description': '', 'next_step_ids': [], 'duration_ms': 0}
    for seq, status in enumerate(['pending', 'running', 'succeeded'], 1):
        sink.write_workflow_step_progress({**event, 'seq': seq, 'status': status,
                                           'private_input': 'private-input-canary'})
    sink._event('system_message', 'completed', sink.request_id + ':private-system', {'text': 'private-system-canary'})
    sink.finish()
    # 损坏历史的内外线程不能绕过原checkpoint身份核验。
    rows = store.messages.history_page_report(task.target_thread_id, limit=50).rows
    forged = copy.deepcopy(next(row.metadata for row in rows if row.role == 'display'))
    forged['display_checkpoint']['thread_id'] = 'another-thread'
    forged['display_checkpoint']['payload']['text'] = 'forged-source-canary'
    store.messages.append({'thread_id': task.target_thread_id, 'role': 'display', 'metadata': forged})
    params = {'target': {'kind': 'background_main', 'id': task.target_thread_id}, 'include_display': True, 'limit': 100}
    reply = client.call('messages.list', params)
    assert reply['ok'], reply
    events = reply['data']['display_events']
    assert [row['payload']['workflow_step']['status'] for row in events] == ['pending', 'running', 'succeeded']
    assert {row['thread_id'] for row in events} == {task.target_thread_id}
    assert {row['task_id'] for row in events} == {task.task_id}
    assert all(row['source_message_id'] for row in events)
    encoded = json.dumps(reply)
    assert all(canary not in encoded for canary in ('private-input-canary', 'private-system-canary', 'forged-source-canary'))
    reconnected = client_for(background_http).call('messages.list', params)
    assert reconnected['data']['display_events'] == events
    foreign = client_for(background_http, user='business-bob').call('messages.list', params)
    assert not foreign['ok'] and 'workflow-run' not in json.dumps(foreign)
    other = client.send(client.prepare('background_agents.create', {
        'space_id': task.metadata['business_context']['space_id'], 'name': '其他观察线程'},
        idempotency_key='other-workflow-thread-0001'))
    assert other['ok'], other
    other_page = client.call('messages.list', {**params, 'target': {
        'kind': 'background_main', 'id': other['data']['agent']['agent_id']}})
    assert other_page['ok'] and other_page['data']['display_events'] == []

    runtime = TuiRuntime('public-workflow')
    navigation = TuiAgentNavigationState(runtime)
    navigation.business_agents = SimpleNamespace(read_denied=False, conversation=lambda aid, **kw:
        client.call('messages.list', {**params, 'target': {'kind': 'background_main', 'id': aid}})['data'])
    navigation._business_targets['business:' + task.target_thread_id] = {'agent_id': task.target_thread_id}
    threads, original_thread = [], threading.Thread
    def tracked(*args, **kwargs):
        thread = original_thread(*args, **kwargs)
        threads.append(thread)
        return thread
    monkeypatch.setattr(module.threading, 'Thread', tracked)
    for _ in range(2):
        navigation._load_business_conversation(task.target_thread_id, runtime)
        threads[-1].join(3)
        assert not threads[-1].is_alive()
    blocks = [block for block in runtime.store.snapshot().stable_blocks if block.text.startswith('工作流 workflow · 步骤 step')]
    assert len(blocks) == 3


def test_public_workflow_step_keeps_unbound_initial_task_unknown(background_http):  # noqa: F811
    services, task = original.observed_task(background_http)
    sink = BackgroundTranscriptSink(SimpleNamespace(conversation_store=services.host.conversation_store),
        thread_id=task.target_thread_id, task_id='')
    sink.write_workflow_step_progress({'workflow_run_id': 'workflow-run', 'workflow_id': 'workflow',
        'step_id': 'step', 'name': '核对', 'description': '', 'next_step_ids': [],
        'duration_ms': 0, 'seq': 1, 'status': 'running'})
    sink.finish()
    reply = client_for(background_http).call('messages.list', {'target': {
        'kind': 'background_main', 'id': task.target_thread_id}, 'include_display': True, 'limit': 100})
    assert reply['ok'], reply
    [event] = reply['data']['display_events']
    assert event['task_id'] is None
    assert event['payload']['workflow_step']['workflow_run_id'] == 'workflow-run'


def test_message_cursor_moves_older_within_one_group_and_preserves_wire_order(background_http):
    services, task = original.observed_task(background_http)
    store = services.host.conversation_store
    for text in ('first-in-group', 'second-in-group', 'last-in-group'):
        store.messages.append({'thread_id': task.target_thread_id, 'role': 'assistant', 'content': text,
                               'metadata': {'conversation_request_id': 'one-original-group'}})
    client = client_for(background_http)
    target = {'kind': 'background_main', 'id': task.target_thread_id}
    whole = client.call('messages.list', {'target': target, 'limit': 100})
    assert whole['ok'], whole
    restored, cursor = [], None
    for _ in range(20):
        params = {'target': target, 'limit': 1}
        if cursor:
            params['cursor'] = cursor
        page = client.call('messages.list', params)
        assert page['ok'], page
        restored = page['data']['items'] + restored
        cursor = page['data']['next_cursor']
        if not cursor:
            break
    assert not cursor and restored == whole['data']['items']
    assert [row['text'] for row in restored if row['text'].endswith('-in-group')] == [
        'first-in-group', 'second-in-group', 'last-in-group']


def test_history_missing_and_corrupt_remain_distinct_wire_facts(background_http):
    services, task = original.observed_task(background_http)
    store = services.host.conversation_store
    client = client_for(background_http)
    params = {'target': {'kind': 'background_main', 'id': task.target_thread_id}, 'limit': 10}
    missing = client.call('messages.list', params)
    assert missing['ok'], missing
    assert missing['data']['history_boundary']['reason_code'] == 'CANONICAL_HISTORY_NOT_RECORDED'
    path = store.storage.message_path(task.target_thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{broken-json\n', encoding='utf-8')
    corrupt = client.call('messages.list', params)
    assert not corrupt['ok'] and corrupt['error']['code'] == 'HOST_NOT_READY', corrupt
    diagnostic = json.dumps(corrupt['error'])
    assert 'CANONICAL_HISTORY_UNREADABLE' in diagnostic and 'JSONDecodeError' in diagnostic
    assert 'CLIENT_HISTORY_LOAD_FAILED' in diagnostic and str(path) not in diagnostic


def test_long_unicode_history_and_display_have_byte_safe_redacted_previews(background_http):
    services, task = original.observed_task(background_http)
    store = services.host.conversation_store
    text = 'api_key=history-secret-canary-923\n' + '中文🙂' * 110000
    saved = store.messages.append({'thread_id': task.target_thread_id, 'role': 'assistant', 'content': text})
    sink = BackgroundTranscriptSink(SimpleNamespace(conversation_store=store),
        thread_id=task.target_thread_id, task_id=task.task_id)
    sink.write_model('Authorization: Bearer display-secret-canary-924\n' + '中文🙂' * 110000)
    sink._flush_model_commentary()
    client = client_for(background_http)
    response = client.call('messages.list', {'target': {'kind': 'background_main', 'id': task.target_thread_id},
                                           'include_display': True, 'limit': 100})
    assert response['ok'], response
    data = response['data']
    previews = [row['text'] for row in data['items']] + [event['payload']['text'] for event in data['display_events']]
    assert len(previews) >= 2 and all(len(value.encode('utf-8')) <= 65536 for value in previews)
    assert all('截断预览' in value and '中文🙂' in value for value in previews)
    assert 'history-secret-canary-923' not in json.dumps(data)
    assert 'display-secret-canary-924' not in json.dumps(data)
    original_row = next(row for row in store.messages.history_page_report(task.target_thread_id, limit=100).rows
                        if row.message_id == saved.message_id)
    assert original_row.content == text
