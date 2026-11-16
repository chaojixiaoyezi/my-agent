"""外部审计 F09：时间坏值不抹掉已知身份，转录顺序仍由原字节偏移确定。"""
import json

import pytest

from agent_py_agent.agent.conversation.models import MessageLogEntry
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.history_ledger import GatewayHistoryLedger


@pytest.mark.parametrize('value', ['not-a-time', [1], {'x': 1}, float('nan'), float('inf'), 10 ** 400])
def test_bad_nonidentity_timestamp_is_unknown_with_identity_and_body_preserved(value, caplog):
    payload = dict(message_id='message', thread_id='thread', role='assistant', content='original',
                   created_at=value, metadata={'gateway_request_id': 'request'})
    row = MessageLogEntry.from_dict(payload)
    assert row.created_at == 0.0
    assert (row.message_id, row.thread_id, row.role, row.content, row.metadata) == (
        'message', 'thread', 'assistant', 'original', {'gateway_request_id': 'request'})
    assert payload['created_at'] is value
    assert 'CONVERSATION_MESSAGE_TIMESTAMP_INVALID' in caplog.text


@pytest.mark.parametrize('missing', ['message_id', 'thread_id', 'role'])
def test_bad_timestamp_does_not_relax_missing_identity(missing):
    payload = dict(message_id='message', thread_id='thread', role='assistant', created_at='bad')
    payload.pop(missing)
    with pytest.raises(ValueError, match='CONVERSATION_MESSAGE_IDENTITY_INVALID'):
        MessageLogEntry.from_dict(payload)


def test_ledger_catchup_and_append_identity_survive_bad_timestamp_without_source_rewrite(tmp_path):
    store = ConversationStore(tmp_path)
    thread = store.threads.create()
    for index in range(3):
        store.messages.append(dict(thread_id=thread.thread_id, role='assistant', content=f'original-{index}',
                                   metadata={'gateway_request_id': f'request-{index}'}))
    path = store.storage.message_path(thread.thread_id)
    lines = path.read_bytes().splitlines()
    middle = json.loads(lines[1])
    middle['created_at'] = 'bad timestamp'
    lines[1] = json.dumps(middle, ensure_ascii=False).encode()
    before = b'\n'.join(lines) + b'\n'
    path.write_bytes(before)
    ledger = GatewayHistoryLedger(store)
    with ledger.connection() as conn:
        assert ledger.catch_up(conn, thread.thread_id)
        assert conn.execute('SELECT COUNT(*) FROM facts WHERE thread=?', (thread.thread_id,)).fetchone()[0] == 3
        assert ledger.matches(conn, thread.thread_id, role='assistant', request_id='request-1',
                              channel_message_id='', assistant_part_id='final')
    rows = store.messages.recent(thread.thread_id, limit=10)
    assert [row.content for row in rows] == ['original-0', 'original-1', 'original-2']
    assert path.read_bytes() == before
