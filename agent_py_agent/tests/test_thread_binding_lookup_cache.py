"""精确绑定缓存沿原文件指纹失效，线程和继任仍实时读取。"""
import json
from dataclasses import replace

from agent_py_agent.agent.conversation import ConversationStore, store_threads
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic


def prepare(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / 'conversation')
    first = ConversationThread(thread_id='first', canonical_user_id='user', title='old')
    second = ConversationThread(thread_id='second', canonical_user_id='user', title='next')
    for row in (first, second):
        store.threads.write(row)
    key = store_threads._binding_key('feishu', 'chat', 'user')
    path = store.storage.bindings_path
    write_json_file_atomic(path, {key: 'first', **{f'unrelated-{i}': 'second' for i in range(1000)}})
    reads = []
    original = store_threads.read_json_object_report

    def read(path, **kwargs):
        if kwargs.get('context') == 'conversation.bindings.read':
            reads.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(store_threads, 'read_json_object_report', read)
    return store, first, second, key, path, reads


def resolve(store):
    return store.threads.resolve_report(channel='feishu', channel_conversation_id='chat', channel_user_id='user')


def test_binding_cache_reads_once_and_keeps_thread_live(tmp_path, monkeypatch):
    store, first, _, _, _, reads = prepare(tmp_path, monkeypatch)
    for _ in range(5):
        assert resolve(store)[0].title == 'old'
    assert len(reads) == 1
    store.threads.write(replace(first, title='changed'))
    reopened = ConversationStore(store.storage.root, initialize=False)
    assert resolve(reopened)[0].title == 'changed'
    assert len(reads) == 1
    store.storage.thread_path('first').unlink()
    assert resolve(store) == (None, None)
    assert len(reads) == 1


def test_binding_cache_replacement_corruption_deletion_and_rebuild(tmp_path, monkeypatch):
    store, _, _, key, path, reads = prepare(tmp_path, monkeypatch)
    assert resolve(store)[0].thread_id == 'first'
    write_json_file_atomic(path, {key: 'second'})
    assert resolve(store)[0].thread_id == 'second'
    assert len(reads) == 2
    path.write_text('{broken')
    row, error = resolve(store)
    assert row is None and error['context'] == 'conversation.bindings.read'
    path.unlink()
    assert resolve(store) == (None, None)
    write_json_file_atomic(path, {key: 'first'})
    assert resolve(store)[0].thread_id == 'first'
    assert len(reads) == 5


def test_missing_binding_is_invalidated_when_added(tmp_path, monkeypatch):
    store, _, _, key, path, reads = prepare(tmp_path, monkeypatch)
    write_json_file_atomic(path, {})
    assert resolve(store) == (None, None)
    assert resolve(store) == (None, None)
    assert len(reads) == 1
    write_json_file_atomic(path, {key: 'second'})
    assert resolve(store)[0].thread_id == 'second'
    assert len(reads) == 2


def test_binding_cache_does_not_hide_succession(tmp_path):
    store = ConversationStore(tmp_path / 'succession')
    first = store.threads.get_or_create({'channel': 'feishu', 'channel_conversation_id': 'chat',
        'channel_user_id': 'user', 'canonical_user_id': 'user',
        'owner_id': 'owner', 'owner_home': str(tmp_path)})
    assert resolve(store)[0].thread_id == first.thread_id
    second = store.threads.succeed(first.thread_id)
    assert resolve(store)[0].thread_id == second.thread_id


def test_binding_cache_has_fixed_entry_bound(tmp_path, monkeypatch):
    store, _, _, _, _, _ = prepare(tmp_path, monkeypatch)
    with store_threads._BINDING_LOOKUP_CACHE_LOCK:
        store_threads._BINDING_LOOKUP_CACHE.clear()
    monkeypatch.setattr(store_threads, '_BINDING_LOOKUP_CACHE_MAX_ENTRIES', 2)
    for number in range(10):
        store.threads.resolve_report(channel='other', channel_conversation_id=str(number), channel_user_id='user')
    assert len(store_threads._BINDING_LOOKUP_CACHE) == 2
