"""通知以真实 V13 Store/RuntimeDB 往返验证；DNS 与传输在模块边界替换。"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import replace
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from agent_py_agent.agent.business_spaces import gap158_w6_notify as notify
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts.business_api import bind_business_context
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call


@pytest.fixture
def world(tmp_path, monkeypatch):
    raw_socket = socket.getaddrinfo, socket.create_connection
    home = tmp_path / 'root/owners/local/main'
    config = SimpleNamespace(business_spaces_enabled=True, notify_delivery_enabled=True,
        allowed_private_hosts=[], tool_http_timeout=7)
    paths = SimpleNamespace(owner_id='local/main', owner_home_dir=home, root=tmp_path / 'root',
        owner_provider='local', owner_kind='main')
    host = SimpleNamespace(home_paths=paths, config=config, business_v13_clock=lambda: 1000.0)
    context = bind_business_context(host, authorize=lambda _action, _facts: True, subject_id='tester')
    services = SimpleNamespace(owner_id=paths.owner_id)
    calls, sent = [], []

    def call(action, params=None, *, key=None, revision=None):
        calls.append(action)
        return V13Call(action, params or {}, 'request', paths.owner_id, 'tester', context, services, home,
            key or 'notify_key_' + str(len(calls)).zfill(16), revision, lambda: 1000.0)

    def send(url, **kwargs):
        sent.append((url, kwargs))
        return 204

    def no_network(*_args, **_kwargs):
        pytest.fail('测试不得访问真实网络')

    monkeypatch.setattr(socket, 'getaddrinfo', no_network)
    monkeypatch.setattr(socket, 'create_connection', no_network)
    monkeypatch.setattr(notify, '_default_network_resolver', lambda _host: ('93.184.216.34',))
    raw_send = notify._send_webhook
    monkeypatch.setattr(notify, '_send_webhook', send)
    raw_kick = notify._kick
    monkeypatch.setattr(notify, '_kick', lambda *_args: None)
    yield SimpleNamespace(home=home, config=config, agent=host, call=call, sent=sent,
        raw_send=raw_send, raw_kick=raw_kick, raw_socket=raw_socket)
    if notify._executor is not None:
        notify._executor.shutdown(wait=True)
        notify._executor = notify._worker_slots = None


def create(world, **changes):
    params = {'kind': 'webhook', 'name': '通知通道', 'url': 'https://hooks.example/path-token?key=url-token',
        'events': ['scheduler_delivery'], **changes}
    return notify.handle_notify(world.call('notify.channels.create', params))


def event(world, event_id='event-1', **changes):
    return notify.enqueue_event(world.agent, owner_id='local/main', event_kind='scheduler_delivery',
        event_id=event_id, payload={'value': 1}, **changes)


def drain(world):
    return notify._drain(notify._runtime(world.home), world.home, 'local/main', world.config, lambda: 1000.0)


def test_meta_switch_and_passive_lists_do_not_initialize_runtime(world):
    world.config.notify_delivery_enabled = False
    meta = notify.handle_notify(world.call('notify.meta'))
    assert meta == {'enabled': False, 'channel_kinds': ['webhook'],
        'max_channels': notify._limit('NOTIFY_CHANNELS_MAX_PER_OWNER'),
        'outbound_allowlist_configured': False, 'delivery_timeout_seconds': 7.0}
    assert notify.notify_ready(world.call('notify.channels.test')) is False
    assert event(world) == []
    assert notify.handle_notify(world.call('notify.channels.list')) == {
        'revision': 0, 'items': [], 'coverage': 'complete'}
    assert notify.handle_notify(world.call('notify.deliveries.list')) == {'items': [], 'coverage': 'complete'}
    assert not world.home.exists()


def test_channel_crud_collection_cas_and_secret_write_only(world, monkeypatch):
    monkeypatch.setenv('NOTIFY_TEST_SECRET', 'fake-signing-secret-not-a-credential')
    original = world.call('notify.channels.create', {'kind': 'webhook', 'name': '通道',
        'url': 'https://hooks.example/private-token?secret=url-token', 'events': ['scheduler_delivery'],
        'secret_env': 'NOTIFY_TEST_SECRET'}, key='fixed_create_key_0001')
    created = notify.handle_notify(original)
    channel_id = created['channel']['channel_id']
    assert created['revision'] == 1
    assert notify.handle_notify(original) == created
    assert set(created['channel']) == {'channel_id', 'kind', 'name', 'url_redacted', 'events',
        'enabled', 'secret_env', 'created_at'}
    assert 'private-token' not in json.dumps(created) and 'url-token' not in json.dumps(created)
    listed = notify.handle_notify(world.call('notify.channels.list'))
    assert listed['revision'] == created['revision'] and listed['items'] == [created['channel']]
    assert 'fake-signing-secret' not in (world.home / 'data/business_v13/notify_channels.json').read_text()
    update = world.call('notify.channels.update', {'channel_id': channel_id, 'name': '修改'}, revision=1)
    changed = notify.handle_notify(update)
    assert changed['revision'] == 2 and changed['channel']['name'] == '修改'
    assert notify.handle_notify(update) == changed
    with pytest.raises(BusinessError, match='REVISION_CONFLICT'):
        notify.handle_notify(world.call('notify.channels.delete', {'channel_id': channel_id}, revision=1))
    deleted = notify.handle_notify(world.call('notify.channels.delete', {'channel_id': channel_id}, revision=2))
    assert deleted == {'deleted': True, 'revision': 3}
    with pytest.raises(BusinessError, match='RESOURCE_NOT_FOUND'):
        notify.handle_notify(world.call('notify.channels.update', {'channel_id': channel_id}, revision=3))
    conflicting = replace(original, params={**original.params, 'name': '另一个名字'})
    with pytest.raises(BusinessError, match='IDEMPOTENCY_CONFLICT'):
        notify.handle_notify(conflicting)


def test_owner_channels_isolated_and_unknown_ids_match_foreign(world, tmp_path):
    channel_id = create(world)['channel']['channel_id']
    source = world.call('notify.channels.update', {'channel_id': channel_id}, revision=0)
    other_home = tmp_path / 'other'
    host = SimpleNamespace(home_paths=SimpleNamespace(owner_id='local/other', owner_home_dir=other_home),
        config=world.config)
    context = bind_business_context(host, authorize=lambda _a, _f: True)
    other = replace(source, owner_id='local/other', owner_home=other_home, context=context,
        subject_id='host', services=SimpleNamespace(owner_id='local/other'))
    for selected in (channel_id, 'absent'):
        with pytest.raises(BusinessError, match='RESOURCE_NOT_FOUND'):
            notify.handle_notify(replace(other, params={'channel_id': selected}))
    assert notify.handle_notify(replace(other, action='notify.channels.list', params={}))['items'] == []
    assert V13Store(world.home, owner_id='local/main').revision('notify_channels') == 1


def test_cas_concurrent_updates_allow_only_one_collection_commit(world):
    created = create(world)
    channel_id = created['channel']['channel_id']
    barrier = threading.Barrier(2)
    calls = [world.call('notify.channels.update', {'channel_id': channel_id, 'name': name}, revision=1)
        for name in ('甲', '乙')]

    def write(call):
        barrier.wait(timeout=5)
        try:
            return notify.handle_notify(call)['revision']
        except BusinessError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, calls))
    assert sorted(map(str, results)) == ['2', 'REVISION_CONFLICT']
    assert notify.handle_notify(world.call('notify.channels.list'))['revision'] == 2


@pytest.mark.parametrize('url', [
    'http://hooks.example/a', 'https://user:password@hooks.example/a', 'https://hooks.example/#fragment',
    'https://hooks.example/\\x', 'https://hooks.example/\n', 'https://127.0.0.1/a',
    'https://127.1/a', 'https://[::1]/a', 'https://10.0.0.1/a', 'https://169.254.169.254/a',
    'https://metadata.google.internal/a', 'https://hooks.example:0/a', 'https://hooks.example:99999/a'])
def test_outbound_url_rejections_do_not_persist(world, url):
    with pytest.raises(BusinessError, match='OUTBOUND_DOMAIN_DENIED'):
        create(world, url=url)
    assert not world.home.exists() and world.sent == []


def test_private_allowlist_and_permanent_metadata_boundary(world):
    world.config.allowed_private_hosts = ['10.0.0.1', '127.0.0.1', '169.254.169.254']
    assert create(world, url='https://10.0.0.1/private')['channel']['url_redacted'] == 'https://10.0.0.1/[redacted]'
    assert create(world, url='https://127.0.0.1/private')['channel']['kind'] == 'webhook'
    with pytest.raises(BusinessError, match='OUTBOUND_DOMAIN_DENIED'):
        create(world, url='https://169.254.169.254/a')
    with pytest.raises(BusinessError, match='OUTBOUND_DOMAIN_DENIED'):
        create(world, url='https://127.0.0.1:8420/a')


def test_secret_must_be_environment_variable_name(world):
    for secret in ('raw-secret-value!', 'ENV=secret', '${HOME}', 'a/b'):
        with pytest.raises(BusinessError, match='INVALID_SCHEMA'):
            create(world, secret_env=secret)


def test_channel_limit_and_cursor_revision_binding(world, monkeypatch):
    original_limit = notify._limit
    monkeypatch.setattr(notify, '_limit', lambda name: 2 if name == 'NOTIFY_CHANNELS_MAX_PER_OWNER' else original_limit(name))
    create(world, name='甲')
    create(world, name='乙')
    first = notify.handle_notify(world.call('notify.channels.list', {'limit': 1}))
    assert first['coverage'] == 'partial' and len(first['items']) == 1
    second = notify.handle_notify(world.call('notify.channels.list', {'limit': 1, 'cursor': first['next_cursor']}))
    assert second['coverage'] == 'complete' and second['items'] != first['items']
    with pytest.raises(BusinessError, match='LIMIT_EXCEEDED'):
        create(world, name='丙')
    notify.handle_notify(world.call('notify.channels.update', {'channel_id': first['items'][0]['channel_id'],
        'enabled': False}, revision=2))
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        notify.handle_notify(world.call('notify.channels.list', {'cursor': first['next_cursor']}))


def test_enqueue_dedup_signing_and_exact_sql_claim_scope(world, monkeypatch):
    channel_id = create(world, secret_env='NOTIFY_TEST_SECRET')['channel']['channel_id']
    monkeypatch.setenv('NOTIFY_TEST_SECRET', 'fake-signing-secret-not-a-credential')
    repo = notify._runtime(world.home)
    foreign = repo.enqueue_outbox(scope='notify', owner_id='local/foreign', effect_key='foreign')
    other_scope = repo.enqueue_outbox(scope='feishu', owner_id='local/main', effect_key='other-scope')
    first, second = event(world)[0], event(world)[0]
    assert first['created'] is True and second['created'] is False
    assert first['outbox_id'] == second['outbox_id']
    key = hashlib.sha256(f'local/main|{channel_id}|event-1'.encode()).hexdigest()
    assert first['effect_key'] == key
    assert len(drain(world)) == 1 and len(world.sent) == 1
    assert drain(world) == []
    row = repo.outbox_entry(first['outbox_id'])
    assert row['status'] == 'ACKED' and row['attempts'] == 1 and row['settled_at'] > 0
    assert repo.outbox_entry(foreign['outbox_id'])['status'] == 'PENDING'
    assert repo.outbox_entry(other_scope['outbox_id'])['status'] == 'PENDING'
    url, transport = world.sent[0]
    assert url.endswith('key=url-token') and 0 < transport['timeout'] <= 7.0
    assert transport['pin_ip'] == '93.184.216.34'
    assert transport['headers']['Idempotency-Key'] == key
    expected = hmac.digest(b'fake-signing-secret-not-a-credential', transport['body'], 'sha256').hex()
    assert transport['headers']['X-MyAgent-Signature'] == 'sha256=' + expected
    assert json.loads(transport['body']) == {
        'event': 'scheduler_delivery', 'event_id': 'event-1', 'payload': {'value': 1}}
    assert 'url-token' not in json.dumps(row) and 'fake-signing-secret' not in json.dumps(row)


def test_enqueue_only_matching_enabled_owner_channels_and_disabled_switch(world):
    create(world)
    create(world, enabled=False)
    create(world, events=['unrelated'])
    assert len(event(world)) == 1
    with pytest.raises(BusinessError, match='RESOURCE_NOT_FOUND'):
        notify.enqueue_event(world.agent, owner_id='local/foreign', event_kind='scheduler_delivery',
            event_id='event-2', payload={})
    world.config.notify_delivery_enabled = False
    assert event(world, 'event-3') == [] and drain(world) == []
    assert world.sent == []


def test_enqueue_to_real_module_worker_and_runtime_settlement_smoke(world, monkeypatch):
    create(world)
    futures = []

    def observe_kick(*args):
        future = world.raw_kick(*args)
        futures.append(future)
        return future

    monkeypatch.setattr(notify, '_kick', observe_kick)
    receipt = event(world)[0]
    assert len(futures) == 1 and futures[0] is not None
    rows = futures[0].result(timeout=5)
    assert len(rows) == 1 and rows[0]['outbox_id'] == receipt['outbox_id']
    assert rows[0]['status'] == 'ACKED' and rows[0]['attempts'] == 1 and len(world.sent) == 1
    assert notify._runtime(world.home).outbox_entry(receipt['outbox_id'])['status'] == 'ACKED'


def test_producer_owner_uses_trusted_canonical_home_not_payload(world):
    create(world)
    receipts = notify.enqueue_event(world.agent, owner_id='local/main', event_kind='scheduler_delivery',
        event_id='payload-cannot-select-owner', payload={'owner_id': 'local/foreign'})
    assert len(receipts) == 1
    repo = notify._runtime(world.home)
    assert repo.outbox_entry(receipts[0]['outbox_id'])['owner_id'] == 'local/main'
    paths = world.agent.home_paths
    wrong_paths = SimpleNamespace(**{**vars(paths), 'owner_home_dir': world.home.parent / 'other'})
    wrong_agent = SimpleNamespace(**{**vars(world.agent), 'home_paths': wrong_paths})
    with pytest.raises(BusinessError, match='RESOURCE_NOT_FOUND'):
        notify.enqueue_event(wrong_agent, owner_id='local/main', event_kind='scheduler_delivery',
            event_id='wrong-home', payload={})


def test_concurrent_drain_cannot_send_same_in_flight_row_twice(world, monkeypatch):
    create(world)
    event(world)
    entered, release = threading.Event(), threading.Event()
    attempts = []

    def transport(*_a, **_k):
        attempts.append(True)
        entered.set()
        assert release.wait(timeout=5)
        return 204

    monkeypatch.setattr(notify, '_send_webhook', transport)
    args = (notify._runtime(world.home), world.home, 'local/main', world.config, lambda: 1000.0)
    first = notify._submit(notify._drain, args)
    try:
        assert entered.wait(timeout=5)
        second = notify._submit(notify._drain, args)
        assert second is not None and second.result(timeout=5) == []
    finally:
        release.set()
    assert first.result(timeout=5)[0]['status'] == 'ACKED' and attempts == [True]


def test_dns_rebinding_before_attempt_is_blocked_even_with_private_network_allowlist(world, monkeypatch):
    channel_id = create(world)['channel']['channel_id']
    world.config.allowed_private_hosts = ['10.0.0.0/8']
    monkeypatch.setattr(notify, '_default_network_resolver', lambda _host: ('10.0.0.5',))
    with pytest.raises(BusinessError, match='OUTBOUND_DOMAIN_DENIED'):
        notify.handle_notify(world.call('notify.channels.test', {'channel_id': channel_id}))
    receipt = event(world)[0]
    row = drain(world)[0]
    assert row['outbox_id'] == receipt['outbox_id'] and row['status'] == 'FAILED'
    assert notify._evidence(row)['error_code'] == 'OUTBOUND_DOMAIN_DENIED' and world.sent == []


def test_failures_dead_letter_and_explicit_replay_keep_runtime_evidence(world, monkeypatch):
    create(world)
    receipt = event(world)[0]
    repo = notify._runtime(world.home)

    def fail(*_args, **_kwargs):
        raise TimeoutError('不允许回显 https://hooks.example/url-token')

    monkeypatch.setattr(notify, '_send_webhook', fail)
    from agent_py_agent.agent.runtime_db.operations import MAX_DELIVERY_ATTEMPT_COUNT
    for attempt in range(MAX_DELIVERY_ATTEMPT_COUNT):
        with repo.transaction() as connection:
            connection.execute('UPDATE outbox_entries SET next_retry_at=0 WHERE outbox_id=?', (receipt['outbox_id'],))
        row = drain(world)[0]
        assert row['attempts'] == attempt + 1
    assert row['status'] == 'DEAD_LETTER' and drain(world) == []
    assert len(row['provider_evidence']) == MAX_DELIVERY_ATTEMPT_COUNT
    assert notify._evidence(row)['error_code'] == 'TOOL_TIMEOUT'
    assert 'url-token' not in json.dumps(row)
    monkeypatch.setattr(notify, '_send_webhook', lambda *_a, **_k: 204)
    retry = world.call('notify.deliveries.retry', {'outbox_id': row['outbox_id']})
    result = notify.handle_notify(retry)
    assert result == {'outbox_id': row['outbox_id'], 'status': 'ACKED', 'attempts': 1}
    assert notify.handle_notify(retry) == result
    with pytest.raises(BusinessError, match='STATE_CONFLICT'):
        notify.handle_notify(world.call('notify.deliveries.retry', {'outbox_id': row['outbox_id']}))
    assert len(repo.outbox_entry(row['outbox_id'])['provider_evidence']) == MAX_DELIVERY_ATTEMPT_COUNT


def test_sync_test_and_retry_target_is_not_lost_behind_backlog(world):
    channel_id = create(world)['channel']['channel_id']
    for index in range(notify._limit('NOTIFY_DRAIN_BATCH') + 2):
        event(world, f'queued-{index}')
    request = world.call('notify.channels.test', {'channel_id': channel_id}, key='stable_test_key_0001')
    result = notify.handle_notify(request)
    assert set(result) == {'ok', 'http_status', 'latency_ms', 'error_code', 'outbox_id'}
    assert result['ok'] is True and result['http_status'] == 204
    assert notify.handle_notify(request) == result and len(world.sent) == 1
    rows, _ = notify._runtime(world.home).list_outbox_page(scope='notify', owner_id='local/main')
    assert sum(row['status'] == 'PENDING' for row in rows) == notify._limit('NOTIFY_DRAIN_BATCH') + 2
    target = next(row for row in rows if row['status'] == 'PENDING')
    result = notify.handle_notify(world.call('notify.deliveries.retry', {'outbox_id': target['outbox_id']}))
    assert result['outbox_id'] == target['outbox_id'] and result['status'] == 'ACKED'
    assert len(world.sent) == 2


def test_retry_foreign_scope_owner_and_missing_are_indistinguishable(world):
    create(world)
    repo = notify._runtime(world.home)
    targets = [repo.enqueue_outbox(scope=scope, owner_id=owner, effect_key=owner + scope)['outbox_id']
        for scope, owner in [('notify', 'local/foreign'), ('feishu', 'local/main')]]
    for outbox_id in [*targets, 'absent']:
        with pytest.raises(BusinessError, match='RESOURCE_NOT_FOUND'):
            notify.handle_notify(world.call('notify.deliveries.retry', {'outbox_id': outbox_id}))
    assert world.sent == []
    assert all(repo.outbox_entry(target)['status'] == 'PENDING' for target in targets)


def test_each_enqueue_and_explicit_retry_triggers_one_bounded_drain(world, monkeypatch):
    original_limit = notify._limit
    monkeypatch.setattr(notify, '_limit', lambda name: 3 if name == 'NOTIFY_DRAIN_BATCH' else original_limit(name))
    create(world)
    for index in range(8):
        event(world, str(index))
    assert len(drain(world)) == 3 and len(world.sent) == 3
    rows, _ = notify._runtime(world.home).list_outbox_page(scope='notify', owner_id='local/main')
    assert sum(row['status'] == 'PENDING' for row in rows) == 5
    kicks = []
    monkeypatch.setattr(notify, '_kick', lambda *_args: kicks.append(True))
    event(world, 'new')
    assert kicks == [True]
    target = next(row for row in rows if row['status'] == 'PENDING')
    notify.handle_notify(world.call('notify.deliveries.retry', {'outbox_id': target['outbox_id']}))
    assert kicks == [True, True]


def test_sync_capacity_exhaustion_keeps_durable_effect_and_reports_unknown(world, monkeypatch):
    channel_id = create(world)['channel']['channel_id']
    call = world.call('notify.channels.test', {'channel_id': channel_id}, key='capacity_test_key_0001')
    submit = notify._submit
    monkeypatch.setattr(notify, '_submit', lambda *_a, **_k: None)
    with pytest.raises(BusinessError, match='OUTCOME_UNKNOWN'):
        notify.handle_notify(call)
    repo = notify._runtime(world.home)
    rows, _ = repo.list_outbox_page(scope='notify', owner_id='local/main')
    assert len(rows) == 1 and rows[0]['status'] == 'PENDING' and world.sent == []
    monkeypatch.setattr(notify, '_submit', submit)
    result = notify.handle_notify(call)
    assert result['ok'] is True and result['outbox_id'] == rows[0]['outbox_id']
    assert len(world.sent) == 1


def test_module_worker_capacity_has_no_unbounded_memory_queue(world):
    workers = notify._limit('NOTIFY_WORKERS')
    started = threading.Barrier(workers + 1)
    release = threading.Event()

    def block():
        started.wait(timeout=5)
        assert release.wait(timeout=5)
        return threading.current_thread().name

    futures = []
    try:
        futures = [notify._submit(block) for _ in range(workers)]
        started.wait(timeout=5)
        assert all(future is not None for future in futures)
        assert notify._submit(lambda: None) is None
    finally:
        release.set()
    names = {future.result(timeout=5) for future in futures}
    assert len(names) == workers and all(name.startswith('notify') for name in names)


def test_transport_no_redirect_and_connection_uses_checked_ip(world, monkeypatch):
    # 不运行网络；直接消费实际 urllib handler 的连接工厂，确认不发生二次 DNS。
    handler = notify._PinnedHandler('https://hooks.example/a', '93.184.216.34')
    connections = []

    def do_open(factory, request):
        connection = factory('hooks.example', timeout=7)
        connections.append(connection)
        return 'checked'

    monkeypatch.setattr(handler, 'do_open', do_open)
    assert handler.https_open(SimpleNamespace()) == 'checked'
    assert connections[0]._pin_ip == '93.184.216.34' and connections[0].host == 'hooks.example'
    assert notify._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://10.0.0.1') is None
    # 模块真传输入口遇到 3xx 仅返回状态，不读取/重发到 Location。
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            raise HTTPError(request.full_url, 302, 'redirect', {'Location': 'https://10.0.0.1'}, None)

    handlers = []
    monkeypatch.setattr(notify, 'build_opener', lambda *items: handlers.extend(items) or Opener())
    assert world.raw_send('https://hooks.example/a', body=b'{}', headers={}, timeout=7,
        pin_ip='93.184.216.34') == 302
    assert len(requests) == 1 and requests[0][0].get_method() == 'POST'
    assert requests[0][1] == 7
    assert any(isinstance(item, notify._NoRedirect) for item in handlers)
    assert any(isinstance(item, notify.ProxyHandler) and item.proxies == {} for item in handlers)


def test_keyset_100k_rows_equal_timestamps_index_and_owner_filtering(world, monkeypatch):
    channel_id = create(world)['channel']['channel_id']
    repo = notify._runtime(world.home)
    payload = json.dumps({'channel_id': channel_id, 'event': 'scheduler_delivery'})
    with repo.transaction() as connection:
        connection.executemany('INSERT INTO outbox_entries(outbox_id,effect_key,owner_id,scope,'
            'payload_json,status,attempts,next_retry_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
            ((f'big-{i:06}', f'effect-{i}', 'local/main' if i % 3 else 'local/foreign',
              'notify' if i % 5 else 'feishu', payload, 'PENDING', 0, 0, i // 4, i // 4)
             for i in range(100000)))
    # 父级的 W0 未合入索引后备由写入口安装，后续公开查询保持只读。
    repo.list_outbox_page(scope='notify', owner_id='local/main', limit=1)
    with repo._runtime_connection() as connection:
        assert connection.execute('SELECT count(*) FROM outbox_entries').fetchone()[0] == 100000
        plan = connection.execute('EXPLAIN QUERY PLAN SELECT * FROM outbox_entries WHERE scope=? AND owner_id=? '
            'AND (created_at,outbox_id)>(?,?) ORDER BY created_at,outbox_id LIMIT 18',
            ('notify', 'local/main', 24000, 'big-096000')).fetchall()
        assert any('idx_outbox_scope_owner' in str(tuple(row)) for row in plan)

    def forbidden_scan(*_a, **_k):
        pytest.fail('不得退回全量 outbox 扫描')

    monkeypatch.setattr(notify.RuntimeRepository, 'list_outbox', forbidden_scan)
    first_call = world.call('notify.deliveries.list', {'limit': 17})
    first = notify.handle_notify(first_call)
    second = notify.handle_notify(world.call('notify.deliveries.list', {'limit': 17, 'cursor': first['next_cursor']}))
    assert len(first['items']) == len(second['items']) == 17
    first_ids = {row['outbox_id'] for row in first['items']}
    assert not first_ids.intersection(row['outbox_id'] for row in second['items'])
    assert first['coverage'] == second['coverage'] == 'partial'
    assert all(int(row['outbox_id'].split('-')[1]) % 3 and int(row['outbox_id'].split('-')[1]) % 5
        for row in first['items'] + second['items'])
    # 深页同样按 keyset 点位进入索引，不重扫历史 offset。
    cursor = notify._next_cursor(first_call, (24000, 'big-096000'))
    deep = notify.handle_notify(world.call('notify.deliveries.list', {'limit': 17, 'cursor': cursor}))
    assert len(deep['items']) == 17
    assert all((row['created_at'], row['outbox_id']) > (24000, 'big-096000') for row in deep['items'])


def test_delivery_cursor_scope_binding_restart_and_bounded_channel_filter(world, monkeypatch):
    channel_id = create(world)['channel']['channel_id']
    for i in range(4):
        event(world, f'event-{i}')
    repo = notify._runtime(world.home)
    repo.list_outbox_page(scope='notify', owner_id='local/main', limit=1)
    first = notify.handle_notify(world.call('notify.deliveries.list', {'limit': 2}))
    cursor = first['next_cursor']
    assert len(first['items']) == 2 and all(item['channel_id'] == channel_id for item in first['items'])
    for params in ({'status': 'FAILED', 'cursor': cursor}, {'channel_id': channel_id, 'cursor': cursor},
                   {'cursor': cursor[:-5] + 'nope'}, {'cursor': 'invalid'}):
        with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
            notify.handle_notify(world.call('notify.deliveries.list', params))
    other = replace(world.call('notify.deliveries.list', {'cursor': cursor}), subject_id='other')
    # 使用真实发行的同 owner 不同主体上下文，不伪造 subject 字段绕过身份门。
    context = bind_business_context(world.agent, authorize=lambda _a, _f: True, subject_id='other')
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        notify.handle_notify(replace(other, context=context))
    selected = notify.handle_notify(world.call('notify.deliveries.list', {'channel_id': 'missing', 'limit': 2}))
    assert selected['items'] == [] and selected['coverage'] == 'partial' and selected['next_cursor']
    monkeypatch.setattr(notify, '_cursor_key', b'another-process-key')
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        notify.handle_notify(world.call('notify.deliveries.list', {'cursor': cursor}))


@pytest.mark.parametrize('limit', [0, 101, True, -1])
def test_delivery_limit_checked_before_database_read(world, limit):
    with pytest.raises(BusinessError, match='INVALID_SCHEMA'):
        notify.handle_notify(world.call('notify.deliveries.list', {'limit': limit}))
    assert not world.home.exists()


@pytest.mark.parametrize('kind,status,frozen,expected', [
    ('external_delivery', 'sent', False, 1),
    ('canonical_record', 'suppressed', False, 1),
    ('outbox_pending', 'failed', True, 0),
    ('none', 'suppressed', False, 0),
])
def test_tool_producer_runs_after_primary_and_only_committed_receipts(world, monkeypatch,
        kind, status, frozen, expected):
    from agent_py_agent.agent.conversation import (
        background_delivery,
        background_im_progress,
        runtime,
    )
    from agent_py_agent.agent.conversation.models import BackgroundDeliveryCommit
    from agent_py_agent.agent.scheduler import execution

    channel_id = create(world)['channel']['channel_id']
    order = []
    run = {'job_id': 'job', 'run_id': 'run', 'thread_id': 'thread'}
    request = SimpleNamespace(wake_signal={'wake_signal_id': 'same-event'})
    thread = SimpleNamespace(thread_id='thread')
    store = SimpleNamespace(threads=SimpleNamespace(require=lambda _key: thread),
        tasks=SimpleNamespace(load=lambda _key: SimpleNamespace(status='completed')))
    world.agent.delivery_service = SimpleNamespace()
    service = SimpleNamespace(conversation_store=store)
    claim = SimpleNamespace(run=run, run_id='run')
    commit = BackgroundDeliveryCommit('not-parsed', status, kind != 'none',
        message_id='message' if kind != 'none' else '', commit_kind=kind, outbox_frozen=frozen)

    def primary(*_a, **_k):
        order.append('primary')
        return commit

    original_enqueue = notify.enqueue_event

    def enqueue(*args, **kwargs):
        assert order[-1] == 'primary'
        order.append('notify')
        return original_enqueue(*args, **kwargs)

    monkeypatch.setattr(runtime, '_run_request', lambda _kwargs: request)
    monkeypatch.setattr(execution, 'resolve_delivery', lambda *_a: (thread, 'internal', 'thread'))
    monkeypatch.setattr(execution, 'scheduled_local_delivery', lambda *_a: False)
    monkeypatch.setattr(background_im_progress, 'background_delivery_context', lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr(background_delivery, 'background_delivery_idempotency_key', lambda _r: 'same-key')
    monkeypatch.setattr(background_delivery, 'record_background_response', primary)
    monkeypatch.setattr(notify, 'enqueue_event', enqueue)
    assert execution._commit_target_result(world.agent, service, claim, {}, 'ignored') is commit
    assert execution._commit_target_result(world.agent, service, claim, {}, 'ignored') is commit
    rows = notify.handle_notify(world.call('notify.deliveries.list'))['items']
    assert len(rows) == expected
    if expected:
        assert rows[0]['channel_id'] == channel_id and rows[0]['event'] == 'scheduler_delivery'
        assert order == ['primary', 'notify', 'primary', 'notify']
    else:
        assert order == ['primary', 'primary']


def test_llm_producer_preserves_primary_return_and_notification_faults(world, monkeypatch):
    from agent_py_agent.agent.conversation import required_user_action
    from agent_py_agent.agent.conversation.background_claim import BackgroundClaimDependencies
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport
    from agent_py_agent.agent.scheduler import execution, precheck

    order = []
    report = BackgroundMainAgentReport(thread_id='thread', task_id='run', reason='scheduled_job',
        response='never parsed', route_channel='internal', route_target='thread', created_at=1,
        delivery_status='sent', wake_handled=True, message_id='message', commit_kind='external_delivery')

    def primary(_kwargs):
        order.append('primary')
        return report

    def broken_notify(*_a, **_k):
        order.append('notify')
        raise RuntimeError('transport-or-store-fault')

    monkeypatch.setattr(required_user_action, 'required_notice', lambda *_a: None)
    monkeypatch.setattr(precheck, 'admit_prechecked_work', lambda *_a, **_k: True)
    monkeypatch.setattr(notify, 'enqueue_event', broken_notify)
    service = SimpleNamespace(repository=SimpleNamespace(mark_run_running=lambda *_a, **_k: None))
    claim = SimpleNamespace(run_id='run', claim_id='claim',
        run={'job_id': 'job', 'run_id': 'run', 'thread_id': 'thread'})
    # 使用宿主真实依赖合同，确保调度包装同时保留执行与终态判定能力。
    dependencies = BackgroundClaimDependencies(
        claims=None, claim_scope_id=lambda *_: "scope", child_owns_task=lambda _: False,
        terminal_task=lambda _: False, recovery_block=lambda _: None,
        source_admission=lambda _: "", retire_source=lambda _: None, run_once=primary,
        runtime_facts=lambda: {}, record_policy_failure=lambda _: None,
        lease_seconds=30, heartbeat_interval_seconds=10,
    )
    bound = execution.with_scheduled_work(dependencies, world.agent, service, claim, None, now=1)
    kwargs = {'thread_id': 'thread', 'task_id': 'run', 'wake_signal': {'wake_signal_id': 'wake'},
        'reason': 'scheduled_job', 'now': 1}
    assert isinstance(bound, BackgroundClaimDependencies)
    assert bound.terminal_task(kwargs) is False
    assert bound.run_once(kwargs) is report
    assert order == ['primary', 'notify']
    world.config.notify_delivery_enabled = False
    assert bound.run_once(kwargs) is report
    assert order == ['primary', 'notify', 'primary']
    # 尚未提交的原报告不会把后续网络错误变成一条通知。
    world.config.notify_delivery_enabled = True
    report = replace(report, commit_kind='outbox_pending', wake_handled=False)
    assert bound.run_once(kwargs) is report
    assert order == ['primary', 'notify', 'primary', 'primary']


def test_v13_slice_real_dispatch_schema_cas_and_ready_gates(world, monkeypatch):
    from agent_py_agent.agent.gateway_parts import business_api_v13 as v13
    from agent_py_agent.agent.gateway_parts import business_api_v13_gap158_w6 as slice_module
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    assert 'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w6' in v13.V13_SLICE_MODULES
    registered = v13._load_registry()
    for name, value in zip(('_ACTIONS', 'ALIASES', 'V13_DEFS', 'JOB_KINDS', 'COLLECTIONS'), registered):
        monkeypatch.setattr(v13, name, value)
    services = SimpleNamespace(owner_id=world.agent.home_paths.owner_id,
        owner_home=world.home, host=world.agent)

    def invoke(action, params, key='', revision=None):
        return v13.v13_invoke(action, params, world.call(action).context, services,
            request_id='request', idempotency_key=key, expected_revision=revision)

    result = invoke('notify.channels.create', {'kind': 'webhook', 'name': 'wire',
        'url': 'https://hooks.example/token', 'events': ['scheduler_delivery']}, 'v13_create_key_0001')
    listed = invoke('notify.channels.list', {})
    assert listed['items'] == [result['channel']] and listed['revision'] == result['revision']
    changed = invoke('notify.channels.update', {'channel_id': result['channel']['channel_id'],
        'name': 'wire-update'}, 'v13_update_key_0001', listed['revision'])
    assert changed['revision'] > listed['revision']
    with pytest.raises(BusinessError, match='REVISION_CONFLICT'):
        invoke('notify.channels.delete', {'channel_id': result['channel']['channel_id']},
            'v13_delete_key_0001', listed['revision'])
    with pytest.raises(HostAPIError, match='INVALID_SCHEMA'):
        invoke('notify.channels.list', {'owner_id': 'foreign'})
    world.config.notify_delivery_enabled = False
    assert not v13.v13_ready('notify.channels.test', services, world.call('notify.meta').context)
    with pytest.raises(HostAPIError, match='HOST_NOT_READY'):
        invoke('notify.channels.test', {'channel_id': result['channel']['channel_id']}, 'v13_test_key_00001')
    assert not v13.v13_ready('ops.update.apply', services, world.call('notify.meta').context)
    assert {action.name for action in slice_module.V13_ACTIONS} == {
        'notify.meta', 'notify.channels.list', 'notify.channels.create', 'notify.channels.update',
        'notify.channels.delete', 'notify.channels.test', 'notify.deliveries.list', 'notify.deliveries.retry',
        'ops.update.check', 'ops.update.apply', 'ops.update.rollback', 'ops.update.stream', 'ops.gc.run'}


def test_settlement_failure_leaves_unstarted_rows_claimable_and_recovers_expired_claim(world, monkeypatch):
    import sqlite3

    create(world)
    for value in range(3):
        event(world, f'settlement-{value}')
    repo = notify._runtime(world.home)
    real_settle = repo.settle_outbox_for_claim

    def fail(*_a, **_k):
        raise sqlite3.OperationalError('synthetic settlement outage')

    monkeypatch.setattr(repo, 'settle_outbox_for_claim', fail)
    with pytest.raises(sqlite3.OperationalError, match='synthetic settlement outage'):
        notify._drain(repo, world.home, 'local/main', world.config, lambda: 1000.0)
    rows, _ = repo.list_outbox_page(scope='notify', owner_id='local/main')
    assert sum(row['status'] == 'IN_FLIGHT' for row in rows) == 1
    assert sum(row['status'] == 'PENDING' for row in rows) == 2
    monkeypatch.setattr(repo, 'settle_outbox_for_claim', real_settle)
    notify._drain(repo, world.home, 'local/main', world.config, lambda: 2000.0)
    rows, _ = repo.list_outbox_page(scope='notify', owner_id='local/main')
    assert all(row['status'] == 'ACKED' for row in rows)


def test_dns_timeout_returns_without_unbounded_resolver_queue(world, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    world.config.tool_http_timeout = 0.01
    errors = []

    def resolver(_host):
        entered.set()
        assert release.wait(timeout=5)
        return ('93.184.216.34',)

    def caller():
        try:
            notify._validate_url('https://hooks.example/path', world.config)
        except BusinessError as error:
            errors.append(error.code)
        finally:
            finished.set()

    monkeypatch.setattr(notify, '_default_network_resolver', resolver)
    thread = threading.Thread(target=caller)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        assert finished.wait(timeout=0.5), 'DNS must not keep the caller beyond its deadline'
        assert errors == ['HOST_NOT_READY']
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_expired_claim_recovery_is_scoped_bounded_and_fences_old_settlement(world):
    from agent_py_agent.agent.runtime_db.operations import RuntimeConflictError

    create(world)
    repo = notify._runtime(world.home)
    receipt = event(world)[0]
    old = repo.claim_outbox_for_scope(scope='notify', owner_id='local/main',
        claimed_by='old-worker', batch_size=1, now=1000.0)[0]
    foreign = repo.enqueue_outbox(scope='notify', owner_id='local/foreign', effect_key='foreign-lease')
    repo.claim_outbox_for_scope(scope='notify', owner_id='local/foreign',
        claimed_by='foreign-worker', batch_size=1, now=1000.0)
    assert repo.recover_outbox_for_scope(scope='notify', owner_id='local/main',
        now=1001.0, lease_seconds=14.0, limit=1) == 0
    assert repo.recover_outbox_for_scope(scope='notify', owner_id='local/main',
        now=2000.0, lease_seconds=14.0, limit=1) == 1
    recovered = repo.outbox_entry(receipt['outbox_id'])
    assert recovered['status'] == 'FAILED'
    assert recovered['provider_evidence']['attempt_1']['error_code'] == 'OUTCOME_UNKNOWN'
    assert repo.outbox_entry(foreign['outbox_id'])['status'] == 'IN_FLIGHT'
    current = repo.claim_outbox_for_scope(scope='notify', owner_id='local/main',
        claimed_by='new-worker', batch_size=1, now=2000.0)[0]
    assert current['attempts'] == 2
    with pytest.raises(RuntimeConflictError, match='no longer current'):
        repo.settle_outbox_for_claim(claim=old, status='ACKED')
    assert repo.outbox_entry(receipt['outbox_id'])['status'] == 'IN_FLIGHT'
    result = repo.settle_outbox_for_claim(claim=current, status='ACKED')
    assert result['status'] == 'ACKED' and result['attempts'] == 2


def test_explicit_retry_recovers_only_expired_notification_claim(world):
    create(world)
    repo = notify._runtime(world.home)
    receipt = event(world)[0]
    repo.claim_outbox_for_scope(scope='notify', owner_id='local/main',
        claimed_by='disconnected-worker', batch_size=1, now=900.0)
    result = notify.handle_notify(world.call('notify.deliveries.retry', {'outbox_id': receipt['outbox_id']}))
    assert result == {'outbox_id': receipt['outbox_id'], 'status': 'ACKED', 'attempts': 2}
    assert len(world.sent) == 1


def test_attempt_deducts_dns_elapsed_from_http_total_budget(world, monkeypatch):
    create(world)
    event(world)
    repo = notify._runtime(world.home)
    row = repo.claim_outbox_for_scope(scope='notify', owner_id='local/main',
        claimed_by='budget-worker', batch_size=1, now=1000.0)[0]
    clock = [100.0]

    def resolve(_host, timeout):
        assert timeout == 7.0
        clock[0] += 3.0
        return ('93.184.216.34',)

    monkeypatch.setattr(notify, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(notify, '_resolve_bounded', resolve)
    result = notify._attempt(repo, row, notify._read_channels(world.home, 'local/main'),
        world.config, lambda: 1000.0)
    assert result['status'] == 'ACKED'
    assert world.sent[0][1]['timeout'] == 4.0
    assert notify._evidence(result)['latency_ms'] == 3000


def test_expired_recovery_100k_inflight_uses_index_and_bounded_vm_work(world, monkeypatch):
    create(world)
    repo = notify._runtime(world.home)
    with repo.transaction() as connection:
        connection.executemany('INSERT INTO outbox_entries(outbox_id,effect_key,owner_id,scope,'
            'payload_json,status,attempts,next_retry_at,claimed_at,created_at,updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ((f'lease-{i:06}', f'lease-effect-{i}', 'local/main', 'notify', '{}', 'IN_FLIGHT',
              1, 0, 0 if i < 3 else 2000, i, i) for i in range(100000)))
    callbacks = [0]
    connect = repo._runtime_connect

    def bounded_connection():
        connection = connect()

        def progress():
            callbacks[0] += 1
            return int(callbacks[0] > 200)

        connection.set_progress_handler(progress, 100)
        return connection

    monkeypatch.setattr(repo, '_runtime_connect', bounded_connection)
    assert repo.recover_outbox_for_scope(scope='notify', owner_id='local/main',
        now=1000.0, lease_seconds=14.0, limit=2) == 2
    assert callbacks[0] <= 200
    monkeypatch.setattr(repo, '_runtime_connect', connect)
    with repo._runtime_connection() as connection:
        plans = connection.execute('EXPLAIN QUERY PLAN SELECT outbox_id,attempts,provider_evidence_json '
            'FROM outbox_entries WHERE scope=? AND owner_id=? AND status=\'IN_FLIGHT\' AND claimed_at<=? '
            'ORDER BY claimed_at,outbox_id LIMIT ?', ('notify', 'local/main', 986.0, 2)).fetchall()
        detail = ' '.join(str(tuple(row)) for row in plans)
        assert 'idx_outbox_scope_lease' in detail and 'TEMP B-TREE' not in detail
        assert connection.execute("SELECT count(*) FROM outbox_entries WHERE status='FAILED'").fetchone()[0] == 2


def _notification_agent(root, workspace):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    return SimpleAgent(AgentConfig(my_agent_home=str(root), model_backend='echo',
        business_spaces_enabled=True, notify_delivery_enabled=True, tool_http_timeout=7,
        memory_curator_enabled=False, enable_self_learning=False,
        owner_maintenance_scan_interval_seconds=0), workspace)


def _completed_one_shot(agent, now):
    from agent_py_agent.agent.scheduler.repository import (
        SchedulerJobCreateRequest,
        SchedulerRunFinish,
    )

    now = float(int(now))
    thread = agent.conversation_store.threads.get_or_create({
        'canonical_user_id': 'notify-regression', 'channel': 'internal',
        'channel_conversation_id': 'notify-regression'})
    repo = agent.scheduler_repository
    job, _ = repo.create_job(SchedulerJobCreateRequest(name='一次通知', prompt='检查合成材料',
        thread_id=thread.thread_id, schedule={'kind': 'at', 'at': now, 'timezone': 'UTC'}, now=now - 1))
    [run] = repo.reserve_due_runs(now=now)
    claim = repo.claim_run(run['run_id'], lease_seconds=60, now=now)
    completed = repo.finish_run(run['run_id'], claim['claim_id'], SchedulerRunFinish(status='done', now=now))
    assert completed['status'] == 'done' and repo.get_job(job['job_id'])['next_run_at'] == 0.0
    return completed


def _notification_channels(agent, count):
    context = bind_business_context(agent, authorize=lambda _action, _facts: True, subject_id='tester')
    for index in range(count):
        notify.handle_notify(V13Call('notify.channels.create', {'kind': 'webhook', 'name': f'通道-{index}',
            'url': 'https://hooks.example/continuation', 'events': ['scheduler_delivery']}, 'request',
            agent.home_paths.owner_id, 'tester', context, SimpleNamespace(owner_id=agent.home_paths.owner_id),
            agent.home_paths.owner_home_dir, f'continuation_channel_{index:04}', None, time.time))


def _publish_completed(agent, run):
    from agent_py_agent.agent.scheduler.notification_binding import enqueue_completed_notification

    enqueue_completed_notification(agent, owner_id=agent.home_paths.owner_id,
        event_kind='scheduler_delivery', event_id=run['run_id'],
        payload={'job_id': run['job_id'], 'run_id': run['run_id']})


def _notification_states(agent):
    rows, after = notify._runtime(agent.home_paths.owner_home_dir, read_only=True).list_outbox_page(
        scope='notify', owner_id=agent.home_paths.owner_id, limit=100)
    assert after is None
    return {state: sum(row['status'] == state for row in rows)
        for state in ('PENDING', 'FAILED', 'IN_FLIGHT', 'ACKED', 'DEAD_LETTER')}


def _notification_fd_count():
    return len(os.listdir('/dev/fd')) if os.path.isdir('/dev/fd') else None


@pytest.mark.parametrize('scenario', ['backlog', 'capacity', 'retry'])
def test_completed_one_shot_local_tick_continues_21_channels(world, tmp_path, monkeypatch, scenario):
    from agent_py_agent.agent.conversation.runtime import _BackgroundSchedulerTickMixin

    agent = _notification_agent(world.agent.home_paths.root, tmp_path / 'workspace')
    clock = [1000.0]
    agent.business_v13_clock = lambda: clock[0]
    assert (notify._limit('NOTIFY_CHANNELS_MAX_PER_OWNER'), notify._limit('NOTIFY_DRAIN_BATCH'),
        notify._limit('NOTIFY_WORKERS')) == (50, 20, 2)
    _notification_channels(agent, 21)
    completed = _completed_one_shot(agent, clock[0])
    attempts = []

    def send(*_args, **_kwargs):
        attempts.append(True)
        return 503 if scenario == 'retry' and len(attempts) == 1 else 204

    monkeypatch.setattr(notify, '_send_webhook', send)
    _publish_completed(agent, completed)
    repo = notify._runtime(agent.home_paths.owner_home_dir)
    if scenario != 'capacity':
        assert len(world.raw_kick(repo, agent.home_paths.owner_home_dir, 'local/main', agent.config,
            agent.business_v13_clock).result(timeout=5)) == 20
        states = _notification_states(agent)
        assert states['PENDING'] == 1 and states['ACKED'] == (19 if scenario == 'retry' else 20)
        assert states['FAILED'] == (1 if scenario == 'retry' else 0)
    release, entered = threading.Event(), threading.Barrier(3)
    occupied = []
    if scenario == 'capacity':
        def block():
            entered.wait(timeout=5)
            assert release.wait(timeout=5)
        occupied = [notify._submit(block) for _ in range(2)]
        entered.wait(timeout=5)
    futures = []

    def kick(*args):
        future = world.raw_kick(*args)
        futures.append(future)
        return future

    monkeypatch.setattr(notify, '_kick', kick)
    tick = SimpleNamespace(scheduler_service=agent.scheduler_service, runtime=SimpleNamespace(agent=agent))
    try:
        if scenario == 'capacity':
            _publish_completed(agent, completed)
            assert futures == [None] and _notification_states(agent)['PENDING'] == 21
            _BackgroundSchedulerTickMixin._enqueue_scheduler_runs(tick, now=clock[0])
            assert futures[-1] is None
    finally:
        release.set()
        for future in occupied:
            done = threading.Event()
            future.add_done_callback(lambda _future, event=done: event.set())
            future.result(timeout=5)
            assert done.wait(timeout=5)
    for _ in range(3):
        _BackgroundSchedulerTickMixin._enqueue_scheduler_runs(tick, now=clock[0])
        if futures[-1] is not None:
            futures[-1].result(timeout=5)
        if _notification_states(agent)['ACKED'] == 21:
            break
        assert scenario == 'retry' or scenario == 'capacity'
        clock[0] += agent.config.tool_http_timeout
    assert _notification_states(agent)['ACKED'] == 21
    assert len(attempts) == (22 if scenario == 'retry' else 21)
    assert notify.notification_deadline(agent.home_paths.owner_home_dir, agent.config) is None


def test_notification_probe_is_readonly_scoped_and_distinguishes_future_claims(world, monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery as discovery

    create(world)
    receipt = event(world)[0]
    repo = notify._runtime(world.home)
    claim = repo.claim_outbox_for_scope(scope='notify', owner_id='local/main', claimed_by='probe', now=1000.0)[0]
    assert notify.notification_deadline(world.home, world.config) == 1014.0
    repo.settle_outbox_for_claim(claim=claim, status='FAILED', next_retry_at=1007.0)
    before = {path.name: path.read_bytes() for path in world.home.glob('runtime.db*')}
    deadlines = []
    assert discovery._owner_has_hard_facts(world.home, deadline_out=deadlines)
    assert 1007.0 in deadlines
    assert notify.notification_deadline(world.home, world.config) == 1007.0
    assert before == {path.name: path.read_bytes() for path in world.home.glob('runtime.db*')}
    world.config.notify_delivery_enabled = False
    assert notify.notification_deadline(world.home, world.config) is None
    assert notify.resume_notifications(world.agent) is None
    world.config.notify_delivery_enabled = True
    current = repo.claim_outbox_for_scope(scope='notify', owner_id='local/main', claimed_by='current', now=1007.0)[0]
    repo.settle_outbox_for_claim(claim=current, status='ACKED')
    manual = repo.enqueue_outbox(scope='notify', owner_id='local/main', effect_key='manual-test',
        payload={'event': 'test', 'channel_id': 'manual', 'test': True})
    repo.enqueue_outbox(scope='notify', owner_id='local/foreign', effect_key='foreign-automatic',
        payload={'event': 'scheduler_delivery'})
    assert notify.notification_deadline(world.home, world.config) is None
    assert notify.resume_notifications(world.agent) is None
    assert repo.outbox_entry(manual['outbox_id'])['status'] == 'PENDING'
    assert repo.outbox_entry(receipt['outbox_id'])['status'] == 'ACKED'
    assert notify.notification_deadline(world.home / 'not-an-owner', world.config) is None


def test_initial_kick_preserves_other_structured_event_delivery(world, monkeypatch):
    create(world, events=['custom_event'])
    futures = []

    def kick(*args):
        future = world.raw_kick(*args)
        futures.append(future)
        return future

    monkeypatch.setattr(notify, '_kick', kick)
    receipt = notify.enqueue_event(world.agent, owner_id='local/main', event_kind='custom_event',
        event_id='custom-1', payload={})[0]
    assert futures[0].result(timeout=5)[0]['status'] == 'ACKED'
    assert notify._runtime(world.home).outbox_entry(receipt['outbox_id'])['status'] == 'ACKED'
    assert notify.notification_deadline(world.home, world.config) is None


def test_notification_probe_100k_acked_history_keeps_index_work_bounded(world, monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery as discovery

    create(world)
    repo = notify._runtime(world.home)
    payload = json.dumps({'event': 'scheduler_delivery'})
    with repo.transaction() as connection:
        connection.executemany('INSERT INTO outbox_entries(outbox_id,effect_key,owner_id,scope,'
            'payload_json,status,attempts,next_retry_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
            ((f'acked-{index:06}', f'acked-effect-{index}', 'local/main', 'notify', payload,
              'ACKED', 1, 0, index, index) for index in range(100000)))
    original, statements, callbacks = discovery._quiescent_probe_connection, [], [0]

    @contextmanager
    def bounded(home):
        with original(home) as connection:
            def progress():
                callbacks[0] += 1
                return int(callbacks[0] > 200)
            connection.set_progress_handler(progress, 100)
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(discovery, '_quiescent_probe_connection', bounded)
    fd_before = _notification_fd_count()
    assert notify.notification_deadline(world.home, world.config) is None
    repo.enqueue_outbox(scope='notify', owner_id='local/main', effect_key='future-automatic',
        payload={'event': 'scheduler_delivery'})
    claim = repo.claim_outbox_for_scope(scope='notify', owner_id='local/main', claimed_by='future', now=1000.0)[0]
    repo.settle_outbox_for_claim(claim=claim, status='FAILED', next_retry_at=2000.0)
    assert notify.notification_deadline(world.home, world.config) == 2000.0
    assert callbacks[0] <= 200
    with original(world.home) as connection:
        queries = [sql for sql in statements if 'FROM outbox_entries' in sql]
        assert len(queries) == 6
        for sql in queries:
            plan = ' '.join(row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + sql))
            assert 'idx_outbox_scope_' in plan and 'SCAN outbox_entries' not in plan
    if fd_before is not None:
        fd_delta = _notification_fd_count() - fd_before
        assert fd_delta == 0


@contextmanager
def _notification_gateway(tmp_path, monkeypatch, *, retain_claim=False):
    from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
    from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
        CoordinatorAdmissionScope,
        PersistentAdmission,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_background_discovery import (
        CoordinatorBackgroundRequests,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
    from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
        prepare_coordinator_takeover,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    monkeypatch.setattr(mode, '_WORKER_PARTITION', None)
    agent = _notification_agent(tmp_path / 'mp-home', tmp_path / 'mp-workspace')
    clock = [time.time()]
    agent.business_v13_clock = lambda: clock[0]
    _notification_channels(agent, 21)
    completed = _completed_one_shot(agent, clock[0])
    _publish_completed(agent, completed)
    old_claim = None
    if retain_claim:
        old_claim = notify._runtime(agent.home_paths.owner_home_dir).claim_outbox_for_scope(
            scope='notify', owner_id='local/main', claimed_by='disconnected', batch_size=1, now=clock[0])[0]
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    gate = PersistentAdmission(paths, agent, CoordinatorAdmissionScope(2, 'notify-C'))
    guard = coordinator_guard(paths.root, gate.count)
    guard.__enter__()
    closed = []
    producer = None

    def close():
        if not closed:
            closed.append(True)
            guard.__exit__(None, None, None)

    try:
        controller = prepare_coordinator_takeover(paths, gate.count, gate.c_run_id)
        gate._runtime_takeover, gate._runtime_takeover_required = controller, True
        control = GatewayControlPlane(agent.config, agent.home_paths.root, paths,
            gate.count, gate.c_run_id, agent_root=agent.root)
        runtime = GatewayDrainRuntime(control)
        runtime.retain_admission(gate)
        producer = CoordinatorBackgroundRequests(control, drain_runtime=runtime)
        fd_before = _notification_fd_count()
        target = SimpleNamespace(identity=OwnerIdentity.local_main(), home_dir=agent.home_paths.owner_home_dir)
        # 冷发现只用只读配置和仓库，不能重新构造执行宿主或发送通知。
        with monkeypatch.context() as readonly:
            readonly.setattr(type(agent), '__init__', lambda *_a, **_k: pytest.fail('冷发现不能构造 Agent'))
            refs = [ref for ref in producer._owner_sources(target) if ref['kind'] == 'scheduler']
        assert len(refs) == 1
        ref = refs[0]
        assert producer._submit(ref)
        yield SimpleNamespace(agent=agent, gate=gate, producer=producer, target=target,
            ref=ref, clock=clock, close=close, fd_before=fd_before, old_claim=old_claim)
    finally:
        for cursor in getattr(producer, '_source_cursors', {}).values():
            cursor.close()
        if notify._executor is not None:
            notify._executor.shutdown(wait=True)
            notify._executor = notify._worker_slots = None
        close()


def _admit_notification(case):
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import session_scope_for_request
    from agent_py_agent.agent.gateway_parts.multiprocess_background_request import source_digest
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_claim import (
        freeze_coordinator_claim,
    )
    from agent_py_agent.agent.gateway_parts.queue_service import claim_request
    from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    path = case.gate.paths.inbox / ('background-' + source_digest(case.ref) + '.json')
    request = read_json_file(path)
    delay = max(0, request.get('not_before_at', 0) - time.time())
    if delay:
        assert delay <= 1.0
        threading.Event().wait(delay + 0.01)
    decision = case.gate.admit(path, (OwnerIdentity.local_main(), session_scope_for_request(request)), AdmissionLimits(8, 8))
    assert decision['status'] == 'admitted', decision
    claim = claim_request(case.gate.paths, path, coordinator_snapshot=freeze_coordinator_claim(case.gate, decision))
    assert claim is not None
    return claim


@contextmanager
def _local_webhook(world, monkeypatch, *, fail_first=False):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((json.loads(self.rfile.read(int(self.headers['Content-Length']))),
                self.headers['Idempotency-Key']))
            self.send_response(503 if fail_first and len(received) == 1 else 204)
            self.end_headers()

        def log_message(self, *_args):
            pass

    monkeypatch.setattr(socket, 'getaddrinfo', world.raw_socket[0])
    monkeypatch.setattr(socket, 'create_connection', world.raw_socket[1])
    monkeypatch.setattr(socket, 'getfqdn', lambda host: host)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()

    def transport(_url, *, body, headers, timeout, pin_ip):
        # 只替换 HTTPS 外部 I/O 边界；请求正文、幂等键、路由与结算均由生产入口生成。
        assert pin_ip == '93.184.216.34'
        with closing(HTTPConnection('127.0.0.1', server.server_port, timeout=timeout)) as connection:
            connection.request('POST', '/hook', body=body, headers=headers)
            with connection.getresponse() as response:
                response.read()
                return response.status

    monkeypatch.setattr(notify, '_send_webhook', transport)
    try:
        yield received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize('scenario', ['backlog', 'retry', 'claim'])
def test_cold_mp_scheduler_source_stays_pending_until_real_webhook_acks(world, tmp_path, monkeypatch, scenario):
    from agent_py_agent.agent.gateway_parts import request_worker as worker
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
    from agent_py_agent.agent.gateway_parts.multiprocess_background_execution import (
        requeue_pending_background_request,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
        coordinator_execution_scope,
    )

    with _notification_gateway(tmp_path, monkeypatch, retain_claim=scenario == 'claim') as case:
        assert _notification_states(case.agent)['PENDING'] == (20 if scenario == 'claim' else 21)
        fd_before = case.fd_before
        responses = []
        original = worker._handle_gateway_request

        def observe(*args, **kwargs):
            response = original(*args, **kwargs)
            responses.append(response)
            if response.get('background_result', {}).get('outcome') == 'pending':
                wrong = {**response, 'background_result': {**response['background_result'], 'source_id': 'wrong-source'}}
                with pytest.raises(ValueError, match='background_source_identity_changed'):
                    requeue_pending_background_request(wrong, case.agent)
            return response

        monkeypatch.setattr(worker, '_handle_gateway_request', observe)
        monkeypatch.setattr(notify, '_kick', world.raw_kick)
        with _local_webhook(world, monkeypatch, fail_first=scenario == 'retry') as received:
            claims = []
            for turn in range(5):
                claim = _admit_notification(case)
                claims.append(claim)
                with coordinator_execution_scope(case.gate, claim, case.agent):
                    assert worker._process_claimed_gateway_request_path(case.agent, case.gate.paths,
                        claim.path, 'original-C', claim=claim)
                result = responses[-1]['background_result']
                assert result['source_kind'] == 'scheduler' and result['source_id'] == case.ref['source_id']
                if result['outcome'] == 'handled':
                    break
                assert result['outcome'] == 'pending'
                pending = read_json_file(case.gate.paths.inbox / claim.name)
                assert pending['not_before_at'] > time.time()
                assert reservation_file(case.gate.paths, claim.request_id).is_file()
                assert not case.producer._submit(case.ref), '原 pending 来源不能创建第二个请求'
                if scenario != 'backlog' and turn in (1, 2):
                    states = _notification_states(case.agent)
                    assert states['ACKED'] == 20
                    assert states['FAILED' if scenario == 'retry' else 'IN_FLIGHT'] == 1
                    assert len(received) == (21 if scenario == 'retry' else 20)
                    assert notify.notification_deadline(case.target.home_dir, case.agent.config) > case.clock[0]
                if turn == 2:
                    case.clock[0] += 2 * case.agent.config.tool_http_timeout
            assert result['outcome'] == 'handled'
            assert len(claims) >= 2 and len({claim.request_id for claim in claims}) == 1
            assert _notification_states(case.agent)['ACKED'] == 21
            assert len(received) == (22 if scenario == 'retry' else 21)
            assert len({key for _body, key in received}) == 21
            assert all(body['event'] == 'scheduler_delivery' for body, _key in received)
            assert all(response['background_result']['wake_count'] == 0 for response in responses)
            assert not [ref for ref in case.producer._owner_sources(case.target) if ref['kind'] == 'scheduler']
            assert not case.producer._submit(case.ref), '原终态来源不能因未变化的 digest 重建请求'
            if case.old_claim is not None:
                repo = notify._runtime(case.target.home_dir, read_only=True)
                recovered = repo.outbox_entry(case.old_claim['outbox_id'])
                assert recovered['attempts'] == 2
                assert recovered['provider_evidence']['attempt_1']['error_code'] == 'OUTCOME_UNKNOWN'
        notify._executor.shutdown(wait=True)
        notify._executor = notify._worker_slots = None
        for cursor in case.producer._source_cursors.values():
            cursor.close()
        case.producer._source_cursors.clear()
        if fd_before is not None:
            fd_delta = _notification_fd_count() - fd_before
            assert fd_delta == 0


@pytest.mark.parametrize('revocation', ['stop', 'generation'])
def test_mp_notify_rechecks_admission_after_dns_wait_without_new_http(world, tmp_path, monkeypatch, revocation):
    from agent_py_agent.agent.gateway_parts.io import (
        gateway_turn_transition,
        update_json_file_atomic,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
    from agent_py_agent.agent.gateway_parts.request_errors import (
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.tests.test_gateway_background_curator_transactions import run_original

    with _notification_gateway(tmp_path, monkeypatch) as case:
        claim = _admit_notification(case)
        proof = reservation_file(case.gate.paths, claim.request_id)
        before = proof.read_bytes()
        entered, release = threading.Event(), threading.Event()
        original = notify._validate_url

        def wait_for_dns(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            assert release.wait(timeout=5)
            return result

        monkeypatch.setattr(notify, '_validate_url', wait_for_dns)
        monkeypatch.setattr(notify, '_kick', world.raw_kick)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_original, case.agent, case.gate, claim)
            try:
                assert entered.wait(timeout=5)
                if revocation == 'stop':
                    with gateway_turn_transition(case.gate.paths, claim.request_id):
                        update_json_file_atomic(claim.path, lambda row: {**row,
                            'cancel_requested': True, 'turn_phase': 'closing'})
                else:
                    case.close()
            finally:
                release.set()
            with pytest.raises(RequestExecutionOutcomeUncertainError):
                future.result(timeout=5)
        assert not world.sent
        states = _notification_states(case.agent)
        assert states['IN_FLIGHT'] == 1 and states['PENDING'] == 20
        assert proof.read_bytes() == before
