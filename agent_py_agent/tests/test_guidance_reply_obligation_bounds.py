"""回复欠账只按固定FIFO页读取，故障页与其它任务不阻塞后续输入。"""
import sqlite3
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation import guidance_reply_obligations as obligations
from agent_py_agent.agent.conversation.store import ConversationStore


def _accepted(store, number, *, task='task', thread='thread'):
    entry = store.guidance.append_once({'target_type': 'task', 'target_id': task,
        'message': 'private input', 'sender': 'sender-' + str(number),
        'metadata': {'kind': 'active_turn_user_input', 'thread_id': thread}},
        dedupe_key='input-' + str(number))
    ledger = store.guidance.ledger
    path = store.storage.guidance_dedupe_path('input-' + str(number))
    receipt = ledger.read_receipt(path)
    ledger.write_receipt(path, replace(receipt, status='consumed', attempt_id='attempt', submission_id='call', submitted_at=1).to_dict())
    obligations.register(store.storage, (entry,), task_id=task)
    return entry.guidance_id


def test_pending_is_bounded_fifo_and_every_page_eventually_settles(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path)
    expected = [_accepted(store, n) for n in range(obligations.PENDING_REPLY_PAGE_SIZE * 2 + 3)]
    with sqlite3.connect(store.storage.guidance_reply_obligations_path) as connection:
        plan = ' '.join(row[3] for row in connection.execute(
            'EXPLAIN QUERY PLAN SELECT input_id, dedupe_key FROM replies '
            'WHERE thread_id=? AND task_id=? AND delivery_committed=0 ORDER BY rowid LIMIT ?',
            ('thread', 'task', obligations.PENDING_REPLY_PAGE_SIZE)))
    assert 'SEARCH' in plan and 'INDEX' in plan and 'TEMP B-TREE' not in plan
    reads = []
    original = store.guidance.ledger.__class__.read_receipt
    def counted(self, path, **kwargs):
        reads.append(path)
        return original(self, path, **kwargs)
    monkeypatch.setattr(store.guidance.ledger.__class__, 'read_receipt', counted)
    first = obligations.pending(store.storage, 'thread', 'task', limit=100000)
    assert first == tuple(expected[:obligations.PENDING_REPLY_PAGE_SIZE])
    assert len(reads) == obligations.PENDING_REPLY_PAGE_SIZE
    seen = []
    while page := obligations.pending(store.storage, 'thread', 'task'):
        assert len(page) <= obligations.PENDING_REPLY_PAGE_SIZE
        seen.extend(page)
        obligations.settle(store.storage, 'thread', 'task', page)
    assert seen == expected


def test_committed_page_failure_cannot_starve_next_page(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path)
    expected = [_accepted(store, n) for n in range(obligations.PENDING_REPLY_PAGE_SIZE + 1)]
    first = obligations.pending(store.storage, 'thread', 'task')
    original = store.guidance.ledger.__class__.write_receipt
    def fail_projection(self, path, payload, **kwargs):
        if payload.get('reply_delivered'):
            raise OSError('simulated receipt projection failure after actual delivery')
        return original(self, path, payload, **kwargs)
    monkeypatch.setattr(store.guidance.ledger.__class__, 'write_receipt', fail_projection)
    with pytest.raises(OSError, match='projection failure'):
        obligations.settle(store.storage, 'thread', 'task', first)
    # 已确认送达的第一条不再挡队首，后续固定页继续前进。
    assert obligations.pending(store.storage, 'thread', 'task') == tuple(expected[1:])
    monkeypatch.setattr(store.guidance.ledger.__class__, 'write_receipt', original)
    obligations.settle(store.storage, 'thread', 'task', first)
    assert obligations.pending(store.storage, 'thread', 'task') == (expected[-1],)


def test_same_thread_new_task_and_sender_cannot_change_frozen_input_set(tmp_path):
    store = ConversationStore(tmp_path)
    first = _accepted(store, 1)
    frozen = obligations.pending(store.storage, 'thread', 'task')
    second = _accepted(store, 2)
    unrelated = _accepted(store, 3, task='new-task')
    obligations.settle(store.storage, 'thread', 'new-task', frozen)
    assert obligations.pending(store.storage, 'thread', 'task') == (first, second)
    obligations.settle(store.storage, 'thread', 'task', frozen)
    assert obligations.pending(store.storage, 'thread', 'task') == (second,)
    assert obligations.pending(store.storage, 'thread', 'new-task') == (unrelated,)
