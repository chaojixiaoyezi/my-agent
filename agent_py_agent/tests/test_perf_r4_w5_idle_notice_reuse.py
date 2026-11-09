"""空闲只复用可完整核验的消息页；默认无hub可用，整响应读口每请求仍执行。"""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.message_stream import NoticeDisplayCapabilities
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import http_handlers, idle_notice_page


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / 'conversations')
    thread = store.threads.get_or_create({'canonical_user_id': 'u', 'channel': 'chat',
        'channel_conversation_id': 'c', 'channel_user_id': 'u'})
    agent = SimpleNamespace(config=SimpleNamespace(gateway_idle_notice_reuse_seconds=120.0))
    scope = SimpleNamespace(user_id='u', channel='chat', conversation_id='c', all_user_access=False)
    calls = []
    original = store.messages.page_after_offset_report
    def page(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(store.messages, 'page_after_offset_report', page)
    idle_notice_page._EMPTY_PAGES.clear()
    def read(**overrides):
        return idle_notice_page.read_idle_message_page(agent, store, thread.thread_id,
            scope=scope, after=overrides.get('after', 0), display=NoticeDisplayCapabilities())
    yield SimpleNamespace(agent=agent, store=store, thread=thread, scope=scope, calls=calls, read=read)
    idle_notice_page._EMPTY_PAGES.clear()


def _append(setup):
    return setup.store.messages.append({'thread_id': setup.thread.thread_id, 'role': 'assistant', 'content': '新通知',
        'metadata': {'background_delivery_reason': 'root_subagents_terminal', 'assistant_part_id': 'final'}})


def test_idle_page_reuses_until_canonical_write_without_owner_hub_event(setup):
    assert setup.read() == setup.read() == ([], 0, True)
    assert len(setup.calls) == 1
    message = _append(setup)
    notices, _, ok = setup.read()
    assert ok and [row['message_id'] for row in notices] == [message.message_id]
    assert len(setup.calls) == 2


def test_cursor_change_nonempty_result_and_expiry_reread(setup, monkeypatch):
    setup.read()
    setup.read(after=1)
    assert len(setup.calls) == 2
    _append(setup)
    assert setup.read()[0] and setup.read()[0]
    assert len(setup.calls) == 4
    cursor = setup.store.storage.message_path(setup.thread.thread_id).stat().st_size
    assert setup.read(after=cursor) == setup.read(after=cursor) == ([], cursor, True)
    clock = idle_notice_page.time.monotonic() + 121
    monkeypatch.setattr(idle_notice_page.time, 'monotonic', lambda: clock)
    setup.read(after=cursor)
    assert len(setup.calls) == 6


def test_disabled_or_hubless_keeps_full_response_reader_per_request(setup):
    setup.agent.config.gateway_idle_notice_reuse_seconds = 0
    setup.read()
    setup.read()
    assert len(setup.calls) == 2
    setup.agent.config.gateway_idle_notice_reuse_seconds = 120
    full_reads = []
    def reader(agent, **kwargs):
        full_reads.append(kwargs)
        return setup.read()
    for _ in range(2):
        assert http_handlers._read_client_notices_with_wait(reader, setup.agent, hub=None, after=0) == ([], 0, True)
    assert len(full_reads) == 2 and len(setup.calls) == 3


def test_crossprocess_canonical_append_is_visible_inside_reuse_window(setup):
    assert setup.read() == setup.read() == ([], 0, True)
    script = '''
import sys
from agent_py_agent.agent.conversation.store import ConversationStore
store=ConversationStore(sys.argv[1],initialize=False)
store.messages.append({'thread_id':sys.argv[2],'role':'assistant','content':'cross-process',
'metadata':{'background_delivery_reason':'root_subagents_terminal','assistant_part_id':'final'}})
'''
    subprocess.run([sys.executable, '-c', script, str(setup.store.storage.root), setup.thread.thread_id], check=True, timeout=30)
    notices, _, ok = setup.read()
    assert ok and [row['content'] for row in notices] == ['cross-process']
    assert len(setup.calls) == 2


def test_unknown_segment_manifest_never_hits_plain_file_cache(setup):
    setup.read()
    path = setup.store.storage.message_path(setup.thread.thread_id).with_suffix('.manifest.json')
    path.write_text('{bad manifest')
    assert setup.read()[2] is False and setup.read()[2] is False
    assert len(setup.calls) == 3


def test_deleted_recreated_source_and_message_permission_change_invalidate(setup):
    _append(setup)
    path = setup.store.storage.message_path(setup.thread.thread_id)
    cursor = path.stat().st_size
    setup.read(after=cursor)
    assert setup.read(after=cursor) == ([], cursor, True)
    assert len(setup.calls) == 1
    replacement = path.with_suffix('.replacement')
    replacement.write_bytes(path.read_bytes())
    replacement.replace(path)
    assert setup.read(after=cursor) == ([], cursor, True)
    assert len(setup.calls) == 2
    path.chmod(0)
    try:
        original_result = idle_notice_page.read_background_response_page(setup.store, setup.thread.thread_id, after=cursor)
        assert setup.read(after=cursor) == original_result
        assert len(setup.calls) == 4
    finally:
        path.chmod(0o600)


def test_commit_during_empty_source_read_cannot_poison_next_poll_cache(setup, monkeypatch):
    original = setup.store.messages.page_after_offset_report
    first = True
    def concurrent_commit(*args, **kwargs):
        nonlocal first
        result = original(*args, **kwargs)
        if first:
            first = False
            _append(setup)
        return result
    monkeypatch.setattr(setup.store.messages, 'page_after_offset_report', concurrent_commit)
    assert setup.read() == ([], 0, True)
    assert [row['content'] for row in setup.read()[0]] == ['新通知']
    assert len(setup.calls) == 2
