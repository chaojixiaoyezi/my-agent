"""延迟历史超过重放页时不能越过旧回复追加用户。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import request_history as history
from agent_py_agent.agent.gateway_parts.request_errors import GatewayHistoryPreparationPending


def test_more_than_replay_page_blocks_user_and_converges(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path)
    thread = store.threads.create()
    agent = SimpleNamespace(conversation_store=store)
    conversation = SimpleNamespace(thread_id=thread.thread_id)
    monkeypatch.setattr(history, 'index_conversation_message', lambda *args: None)
    for i in range(101):
        history._queue_gateway_conversation_repair(agent, {}, conversation,
            request_id=f'old-{i}', role='assistant', content=f'reply-{i}')
    progress = {}
    history.repair_gateway_conversation_messages(store, thread.thread_id, [], progress=progress)
    assert progress['repairs_consumed'] == 100
    assert progress['repairs_remaining'] is True
    with pytest.raises(GatewayHistoryPreparationPending):
        history.append_gateway_conversation_message(agent, {}, conversation,
            request_id='new', role='user', content='new', defer_index_pending=True)
    history.repair_gateway_conversation_messages(store, thread.thread_id, [], progress=progress)
    assert history.history_repair_ready(progress)
    assert history.append_gateway_conversation_message(agent, {}, conversation,
        request_id='new', role='user', content='new')
    rows = store.messages.recent(thread.thread_id, limit=200)
    assert len(rows) == 102
    assert rows[-1].role == 'user'


@pytest.mark.parametrize('field', ['legacy_entries_advanced', 'repairs_imported', 'repairs_consumed'])
def test_progress_requires_committed_error_free_integer(field):
    assert GatewayHistoryPreparationPending({}, 't', {field: 1}).progressed
    assert not GatewayHistoryPreparationPending({}, 't', {field: 1, 'repair_error': True}).progressed
    assert not GatewayHistoryPreparationPending({}, 't', {field: True}).progressed


def test_partial_migration_never_replays_and_resumes(tmp_path):
    store = ConversationStore(tmp_path)
    thread = store.threads.create()
    agent = SimpleNamespace(conversation_store=store)
    conversation = SimpleNamespace(thread_id=thread.thread_id)
    for i in range(4):
        history._queue_gateway_conversation_repair(agent, {}, conversation,
            request_id=str(i), role='assistant', content=str(i))
    progress = {}
    history.repair_gateway_conversation_messages(store, thread.thread_id, [], progress=progress, limit=2)
    assert not progress['legacy_complete']
    assert progress['legacy_entries_advanced'] == 2
    assert not store.messages.recent(thread.thread_id, limit=10)
    for _ in range(8):
        history.repair_gateway_conversation_messages(store, thread.thread_id, [], progress=progress, limit=2)
        if history.history_repair_ready(progress):
            break
    assert history.history_repair_ready(progress)
    assert len(store.messages.recent(thread.thread_id, limit=10)) == 4


def test_failed_page_cannot_disappear_from_coverage(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path)
    thread = store.threads.create()
    agent = SimpleNamespace(conversation_store=store)
    for i in range(3):
        history._queue_gateway_conversation_repair(agent, {}, SimpleNamespace(thread_id=thread.thread_id),
            request_id=str(i), role='assistant', content=str(i))
    original = history.GatewayHistoryLedger.import_repair
    def fail(*args):
        raise OSError('injected import failure')
    monkeypatch.setattr(history.GatewayHistoryLedger, 'import_repair', fail)
    progress = {}
    # 发现页也包含锁等设施；保持每页一项，直到真实JSON消费点执行故障注入。
    for _ in range(10):
        history.repair_gateway_conversation_messages(store, thread.thread_id, [], progress=progress, limit=1)
        if progress["repair_error"]:
            break
        assert not progress["legacy_complete"]
        assert not store.messages.recent(thread.thread_id, limit=10)
    assert progress['repair_error'] and not progress['legacy_complete']
    assert not GatewayHistoryPreparationPending({}, thread.thread_id, progress).progressed
    monkeypatch.setattr(history.GatewayHistoryLedger, 'import_repair', original)
    for _ in range(10):
        history.repair_gateway_conversation_messages(store, thread.thread_id, [], progress=progress, limit=2)
        if history.history_repair_ready(progress):
            break
    assert history.history_repair_ready(progress)
    assert len(store.messages.recent(thread.thread_id, limit=10)) == 3


def test_malformed_first_file_is_quarantined_without_losing_healthy(tmp_path):
    store = ConversationStore(tmp_path)
    thread = store.threads.create()
    directory = store.storage.root / 'message_repairs'
    directory.mkdir()
    (directory / 'bad.json').write_text('{')
    history._queue_gateway_conversation_repair(SimpleNamespace(conversation_store=store), {},
        SimpleNamespace(thread_id=thread.thread_id), request_id='ok', role='assistant', content='ok')
    progress = {}
    history.repair_gateway_conversation_messages(store, thread.thread_id, [], progress=progress)
    assert history.history_repair_ready(progress)
    assert [row.content for row in store.messages.recent(thread.thread_id, limit=10)] == ['ok']
    assert not (directory / 'bad.json').exists()


def test_directory_mutation_restarts_discovery(tmp_path):
    from agent_py_agent.agent.gateway_parts.history_ledger import legacy_repair_page
    directory = tmp_path / 'repair'
    directory.mkdir()
    (directory / 'one').write_text('')
    (directory / 'two').write_text('')
    legacy_repair_page(directory, 1)
    (directory / 'three').write_text('')
    entries, complete = legacy_repair_page(directory, 10)
    assert complete
    assert {entry.name for entry in entries} == {'one', 'two', 'three'}


def test_zero_budget_is_explicit_stall(tmp_path):
    store = ConversationStore(tmp_path)
    thread = store.threads.create()
    history._queue_gateway_conversation_repair(SimpleNamespace(conversation_store=store), {},
        SimpleNamespace(thread_id=thread.thread_id), request_id='ok', role='assistant', content='ok')
    progress = {}
    history.repair_gateway_conversation_messages(store, thread.thread_id, [], progress=progress, limit=0)
    assert not history.history_repair_ready(progress)
    assert not GatewayHistoryPreparationPending({}, thread.thread_id, progress).progressed


@pytest.mark.parametrize('allow', [False, True])
def test_refresh_cannot_mint_unstarted_retry(tmp_path, monkeypatch, allow):
    from agent_py_agent.agent.gateway_parts import request_context as context
    from agent_py_agent.agent.gateway_parts.request_errors import ConversationPersistenceError
    store = ConversationStore(tmp_path)
    thread = store.threads.create()
    monkeypatch.setattr(context, '_load_gateway_thread', lambda *args: (thread, None))
    monkeypatch.setattr(context, '_bind_gateway_stream_identity', lambda *args, **kwargs: None)
    def pending(*args, progress, **kwargs):
        progress.update(legacy_complete=False, repairs_imported=1, repair_error=False)
    monkeypatch.setattr(history, 'repair_gateway_conversation_messages', pending)
    request = context.GatewayConversationLoadRequest(SimpleNamespace(conversation_store=store),
        {'conversation': {}}, 'request', 'prompt', allow_history_defer=allow)
    expected = GatewayHistoryPreparationPending if allow else ConversationPersistenceError
    with pytest.raises(expected):
        context.gateway_conversation_context(request)


def test_idle_busy_oldest_does_not_starve_healthy_thread(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import compact_operations as compact
    store = ConversationStore(tmp_path)
    threads = sorted([store.threads.create(), store.threads.create()], key=lambda row: row.thread_id)
    agent = SimpleNamespace(conversation_store=store, home_paths=SimpleNamespace(owner_home_dir=tmp_path),
                            config=SimpleNamespace(memory_compact_idle_max_per_tick=1,
                                                   retention_scan_slice_entries=1000,
                                                   retention_scan_slice_ms=10000))
    for row in threads:
        history._queue_gateway_conversation_repair(agent, {}, SimpleNamespace(thread_id=row.thread_id),
            request_id=row.thread_id, role='assistant', content='repaired')
    monkeypatch.setattr(compact, 'idle_compact_busy_state',
                        lambda agent, store, row: 'busy' if row.thread_id == threads[0].thread_id else '')
    cursor = {}
    for _ in range(3):
        compact.idle_compact_maintenance_tick(agent, cursor=cursor)
    assert not store.messages.recent(threads[0].thread_id, limit=10)
    assert len(store.messages.recent(threads[1].thread_id, limit=10)) == 1


def test_repair_arriving_between_final_prepare_and_append_reloads_context(tmp_path, monkeypatch):
    from contextlib import contextmanager
    store = ConversationStore(tmp_path)
    thread = store.threads.create()
    agent = SimpleNamespace(conversation_store=store)
    conversation = SimpleNamespace(thread_id=thread.thread_id)
    original = history.GatewayHistoryLedger.connection
    with original(history.GatewayHistoryLedger(store)):
        pass
    injected = False
    @contextmanager
    def interleave(ledger):
        nonlocal injected
        with original(ledger) as conn:
            yield conn
        if not injected:
            injected = True
            history._queue_gateway_conversation_repair(agent, {}, conversation,
                request_id='old', role='assistant', content='old reply')
    monkeypatch.setattr(history.GatewayHistoryLedger, 'connection', interleave)
    with pytest.raises(GatewayHistoryPreparationPending) as error:
        history.append_gateway_conversation_message(agent, {}, conversation,
            request_id='new', role='user', content='new', defer_index_pending=True)
    assert error.value.progressed
    assert [row.role for row in store.messages.recent(thread.thread_id, limit=10)] == ['assistant']


def test_idle_no_work_does_not_create_ledger_or_repair_directory(tmp_path):
    from agent_py_agent.agent.gateway_parts import compact_operations as compact
    store = ConversationStore(tmp_path)
    agent = SimpleNamespace(conversation_store=store, home_paths=SimpleNamespace(owner_home_dir=tmp_path),
        config=SimpleNamespace(memory_compact_idle_max_per_tick=1,
            retention_scan_slice_entries=10, retention_scan_slice_ms=10000))
    compact.idle_compact_maintenance_tick(agent, cursor={})
    assert not history.GatewayHistoryLedger(store).path.exists()
    assert not (store.storage.root / 'message_repairs').exists()


@pytest.mark.parametrize('budget', [{'max_bytes': 0}, {'deadline': 0.0}, {'limit': 0}])
def test_zero_byte_or_expired_deadline_cannot_claim_progress(tmp_path, monkeypatch, budget):
    store = ConversationStore(tmp_path)
    thread = store.threads.create()
    history._queue_gateway_conversation_repair(SimpleNamespace(conversation_store=store), {},
        SimpleNamespace(thread_id=thread.thread_id), request_id='old', role='assistant', content='old')
    def forbidden(*args, **kwargs):
        pytest.fail('无额度不能封尾、扫描修复目录或打开账本')
    monkeypatch.setattr(store.messages, 'seal_torn_tail', forbidden)
    monkeypatch.setattr(history, 'legacy_repair_page', forbidden)
    monkeypatch.setattr(history.GatewayHistoryLedger, 'connection', forbidden)
    progress = {}
    history.repair_gateway_conversation_messages(store, thread.thread_id, [], progress=progress, **budget)
    assert progress['visited_entries'] <= 1000
    assert progress['visited_bytes'] == 0
    assert not GatewayHistoryPreparationPending({}, thread.thread_id, progress).progressed
    assert not store.messages.recent(thread.thread_id, limit=10)
