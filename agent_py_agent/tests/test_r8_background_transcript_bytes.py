"""展示缓存跨线程字节门与原游标恢复；不触碰canonical或执行权限。"""
from types import SimpleNamespace

from agent_py_agent.agent.conversation import background_transcript as transcript


def append(agent, thread, index, payload):
    return transcript.append_background_transcript_event(
        agent, source=transcript.TranscriptSource(thread, 'task', 'request'),
        kind='assistant_completed', phase='completed', block_id=f'block-{index}', payload=payload)


def test_ten_threads_share_one_byte_budget_and_latest_frame_remains(monkeypatch):
    monkeypatch.setattr(transcript, 'BACKGROUND_TRANSCRIPT_MAX_BYTES', 128 * 1024)
    agent = SimpleNamespace()
    for index in range(200):
        append(agent, f'thread-{index % 10}', index, {'text': str(index) + 'x' * 12000})
    rows = agent._conversation_background_transcript_projection
    assert sum(state.retained_bytes for state in rows.values()) <= 128 * 1024
    assert sum(len(state.events) for state in rows.values()) < 200
    last = transcript.read_background_transcript_events(agent, thread_id='thread-9', after=0)
    assert last['events'][-1]['block_id'] == 'block-199'
    assert last['truncated'] is True
    cursor = last['cursor']
    again = transcript.read_background_transcript_events(agent, thread_id='thread-9', after=cursor, stream_id=last['stream_id'])
    assert again['events'] == [] and again['truncated'] is False
    last['events'][-1]['payload']['text'] = 'client mutation'
    assert rows['thread-9'].events[-1]['payload']['text'] != 'client mutation'


def test_large_nested_payload_does_not_call_deepcopy_or_lose_terminal_marker(monkeypatch):
    agent = SimpleNamespace()
    original = transcript.copy.deepcopy
    copied = []

    def tracked(value):
        copied.append(value)
        return original(value)

    monkeypatch.setattr(transcript.copy, 'deepcopy', tracked)
    oversized = {'text': 'x' * 1000000, 'nested': [list(range(10000))]}
    sequence = append(agent, 'thread', 1, oversized)
    assert copied == []
    result = transcript.read_background_transcript_events(agent, thread_id='thread', after=0)
    assert result['cursor'] == sequence and result['truncated']
    event = result['events'][-1]
    assert event['kind'] == 'assistant_completed' and event['phase'] == 'completed'
    assert event['payload']['history_incomplete'] and event['payload']['truncated']
    assert len(event['payload']['text']) <= transcript.BACKGROUND_TRANSCRIPT_TEXT_LIMIT_CHARS
    state = agent._conversation_background_transcript_projection['thread']
    assert state.retained_bytes <= transcript.BACKGROUND_TRANSCRIPT_EVENT_MAX_BYTES


def test_cyclic_payload_and_new_task_preserve_bounded_accounting():
    agent = SimpleNamespace()
    payload = {}
    payload['cycle'] = payload
    append(agent, 'thread', 1, payload)
    state = agent._conversation_background_transcript_projection['thread']
    assert state.events[-1]['payload']['history_incomplete']
    transcript.begin_background_transcript_turn(agent, thread_id='thread', task_id='next')
    assert not state.events and not state.sizes and state.retained_bytes == 0
    other = SimpleNamespace()
    append(other, 'thread', 2, {'text': 'other owner'})
    assert not state.events
