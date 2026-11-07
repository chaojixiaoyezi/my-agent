"""真实签发的 V13Call/owner Store 与固定时钟、Event 交错验证协作边界；不触网。"""
from __future__ import annotations

import json
import socket
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.business_spaces import gap158_w9_collab_presence as presence
from agent_py_agent.agent.business_spaces import v13_store
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)


class _Harness:
    def __init__(self, root, clock, owner='alice'):
        paths = home_paths_with_owner(home_paths(root),
            resolve_owner_home(root, OwnerIdentity.local_user(owner)))
        self.host = SimpleNamespace(home_paths=paths,
            config=SimpleNamespace(business_spaces_enabled=True))
        self.root, self.clock = root, clock
        self.home, self.owner = paths.owner_home_dir, paths.owner_id
        self.allowed, self.sequence = True, 0
        # 直接 V13Call 只需可信 scope；完整 BusinessServices 会预建无关领域库，干扰零写入观察。
        # context 签发、实时授权和全部文档读写仍使用真实实现，不替换 Store。
        self.services = SimpleNamespace(owner_id=self.owner, owner_home=self.home, host=self.host)

    def make(self, action, params, *, subject='actor', key=None, expected=None):
        self.sequence += 1
        context = api.bind_business_context(self.host, subject_id=subject,
            authorize=lambda action, facts: self.allowed)
        return V13Call('collab_presence.' + action, params, f'request-{self.sequence}',
            self.owner, subject, context, self.services, self.home,
            key or f'w9-{self.sequence:020d}', expected, lambda: self.clock[0])

    def run(self, action, params, **options):
        handler = {'list': presence.list_members, 'doc_state.get': presence.doc_state_get,
                   'doc_state.propose': presence.doc_state_propose}.get(action)
        handler = handler or getattr(presence, action)
        return handler(self.make(action, params, **options))

    def join(self, member='driver', role='driver', doc='doc', **options):
        params = {'doc_id': doc, 'member_id': member, 'role': role}
        for name in ('takeover', 'expected_driver_member_id'):
            if name in options:
                params[name] = options.pop(name)
        return self.run('join', params, **options)

    def members(self, doc='doc'):
        return self.run('list', {'doc_id': doc})

    def get(self, doc='doc'):
        return self.run('doc_state.get', {'doc_id': doc})

    def propose(self, state, *, doc='doc', member='driver', expected=0, **options):
        return self.run('doc_state.propose', {'doc_id': doc, 'member_id': member, 'state': state},
                        expected=expected, **options)


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path / 'my-agent'
    monkeypatch.setenv('HOME', str(tmp_path / 'os-home'))
    monkeypatch.setenv('MY_AGENT_HOME', str(root))
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    monkeypatch.setattr(presence, '_presence', presence._PresenceRegistry())

    def no_network(*args, **kwargs):
        pytest.fail('协作 handler 测试不得请求网络')

    monkeypatch.setattr(socket.socket, 'connect', no_network)
    return _Harness(root, [1794000000.0])


def _error(code, function):
    with pytest.raises((BusinessError, HostAPIError)) as raised:
        function()
    assert raised.value.code == code
    return raised.value


def _member(row, member, role, joined, seen, expires):
    assert row == {'member_id': member, 'role': role, 'joined_at': joined,
                   'last_seen_at': seen, 'expires_at': expires}


def _worker(function, name):
    outcome = {}

    def execute():
        try:
            outcome['result'] = function()
        except BaseException as error:
            outcome['error'] = error

    thread = threading.Thread(target=execute, name=name, daemon=True)
    thread.start()
    return SimpleNamespace(thread=thread, outcome=outcome)


def _finish(worker):
    worker.thread.join(timeout=5)
    assert not worker.thread.is_alive(), 'Event 交错不能留下死锁线程'
    return worker.outcome


class _ObservedLock:
    """事件在真实 acquire 前后设置，用事实证明另一线程到达了同一把锁。"""

    def __init__(self, lock, target):
        self.lock, self.target = lock, target
        self.attempted, self.acquired = threading.Event(), threading.Event()

    def __enter__(self):
        observed = threading.current_thread().name == self.target
        if observed:
            self.attempted.set()
        self.lock.acquire()
        if observed:
            self.acquired.set()
        return self

    def __exit__(self, *exception):
        self.lock.release()


def test_join_heartbeat_rejoin_and_leave(case):
    start, ttl = case.clock[0], presence._bounds()[0]
    first = case.join()
    _member(first['member'], 'driver', 'driver', start, start, start + ttl)
    assert first['driver_member_id'] == 'driver'
    case.clock[0] += 4
    beat = case.run('heartbeat', {'doc_id': 'doc', 'member_id': 'driver'})
    _member(beat['member'], 'driver', 'driver', start, start + 4, start + 4 + ttl)
    case.clock[0] += 2
    rejoined = case.join(role='observer')
    _member(rejoined['member'], 'driver', 'observer', start, start + 6, start + 6 + ttl)
    assert rejoined['driver_member_id'] == ''
    assert case.members() == {'members': [rejoined['member']], 'driver_member_id': ''}
    key = 'leave-stable-key-00000001'
    assert case.run('leave', {'doc_id': 'doc', 'member_id': 'driver'}, key=key) == {'left': True}
    assert case.run('leave', {'doc_id': 'doc', 'member_id': 'driver'}, key=key) == {'left': True}
    assert case.run('leave', {'doc_id': 'doc', 'member_id': 'driver'}) == {'left': False}
    assert case.members() == {'members': [], 'driver_member_id': ''}
    assert presence._presence.owners == {} and presence._presence.total_members == 0


def test_expiry_boundary_requires_rejoin_and_reclaims_buckets(case):
    first = case.join()['member']
    case.clock[0] = first['expires_at']
    assert case.members() == {'members': [], 'driver_member_id': ''}
    assert presence._presence.owners == {} and presence._presence.receipts == {}
    _error('RESOURCE_NOT_FOUND', lambda: case.run('heartbeat',
        {'doc_id': 'doc', 'member_id': 'driver'}))
    _error('VERIFICATION_FAILED', lambda: case.propose({'late': True}))
    again = case.join()['member']
    assert again['joined_at'] == first['expires_at']
    assert again['joined_at'] > first['joined_at']


def test_single_driver_takeover_cas_and_demotion(case):
    case.join('old')
    case.join('new', 'navigator')
    before = case.members()
    _error('STATE_CONFLICT', lambda: case.join('new'))
    _error('REVISION_CONFLICT', lambda: case.join('new', takeover=True,
        expected_driver_member_id='stale'))
    assert case.members() == before
    takeover_key = 'takeover-stable-key-00000001'
    result = case.join('new', takeover=True, expected_driver_member_id='old', key=takeover_key)
    assert result['driver_member_id'] == 'new'
    assert case.join('new', takeover=True, expected_driver_member_id='old', key=takeover_key) == result
    roles = {member['member_id']: member['role'] for member in case.members()['members']}
    assert roles == {'new': 'driver', 'old': 'navigator'}
    _error('REVISION_CONFLICT', lambda: case.join('third', takeover=True,
        expected_driver_member_id='old'))
    _error('VERIFICATION_FAILED', lambda: case.propose({}, member='old'))
    assert case.propose({'driver': 'new'}, member='new') == {'doc_id': 'doc', 'revision': 1}


def test_expired_takeover_must_compare_against_no_driver(case):
    old = case.join('old')['member']
    case.clock[0] = old['expires_at']
    _error('REVISION_CONFLICT', lambda: case.join('new', takeover=True,
        expected_driver_member_id='old'))
    assert case.members()['members'] == []
    assert case.join('new', takeover=True, expected_driver_member_id='')['driver_member_id'] == 'new'


def test_owner_doc_and_authenticated_call_isolation(case):
    other = _Harness(case.root, case.clock, 'bob')
    case.join(key='shared-owner-key-00000001')
    assert other.members()['members'] == []
    _error('RESOURCE_NOT_FOUND', lambda: other.run('heartbeat',
        {'doc_id': 'doc', 'member_id': 'driver'}))
    assert other.run('leave', {'doc_id': 'doc', 'member_id': 'driver'}) == {'left': False}
    _error('VERIFICATION_FAILED', lambda: other.propose('foreign'))
    other.join(key='shared-owner-key-00000001')
    assert case.propose('alice')['revision'] == 1
    assert other.get() == {'doc_id': 'doc', 'revision': 0}
    assert other.propose('bob')['revision'] == 1
    assert case.get()['state'] == 'alice' and other.get()['state'] == 'bob'
    assert case.members('other-doc')['members'] == []
    _error('VERIFICATION_FAILED', lambda: case.propose('wrong-doc', doc='other-doc', expected=1))
    forged = replace(case.make('list', {'doc_id': 'doc'}), owner_id=other.owner)
    _error('RESOURCE_NOT_FOUND', lambda: presence.list_members(forged))


def test_member_subject_binding_prevents_driver_impersonation(case):
    case.join(subject='alice')
    _error('STATE_CONFLICT', lambda: case.join(subject='bob'))
    _error('RESOURCE_NOT_FOUND', lambda: case.run('heartbeat',
        {'doc_id': 'doc', 'member_id': 'driver'}, subject='bob'))
    assert case.run('leave', {'doc_id': 'doc', 'member_id': 'driver'}, subject='bob') == {'left': False}
    _error('VERIFICATION_FAILED', lambda: case.propose({}, subject='bob'))
    assert case.propose({}, subject='alice')['revision'] == 1


def test_member_limit_rejoin_and_failed_takeover_are_atomic(case):
    maximum = presence._bounds()[1]
    case.join()
    for index in range(maximum - 1):
        case.join(f'observer-{index}', 'observer')
    assert len(case.members()['members']) == maximum
    case.join(role='driver')
    _error('LIMIT_EXCEEDED', lambda: case.join('extra', 'observer'))
    _error('LIMIT_EXCEEDED', lambda: case.join('extra', takeover=True,
        expected_driver_member_id='driver'))
    assert case.members()['driver_member_id'] == 'driver'
    assert len(case.members()['members']) == maximum
    assert presence._presence.total_members == maximum


@pytest.mark.parametrize('dimension', ['owners', 'docs', 'members'])
def test_all_active_dimensions_bounded_and_expired_capacity_reusable(case, monkeypatch, dimension):
    registry = presence._PresenceRegistry(max_members=2)
    monkeypatch.setattr(presence, '_presence', registry)
    bob = _Harness(case.root, case.clock, 'bob')
    charlie = _Harness(case.root, case.clock, 'charlie')
    if dimension == 'owners':
        case.join()
        bob.join()
        error = _error('LIMIT_EXCEEDED', lambda: charlie.join())
        assert error.details['limit'] == 'presence_active_owners'
    elif dimension == 'docs':
        case.join(doc='a')
        case.join(doc='b')
        error = _error('LIMIT_EXCEEDED', lambda: case.join(doc='c'))
        assert error.details['limit'] == 'presence_docs_per_owner'
    else:
        case.join()
        case.join('observer', 'observer')
        bob.join()
        bob.join('observer', 'observer')
        error = _error('LIMIT_EXCEEDED', lambda: case.join(doc='next'))
        assert error.details['limit'] == 'presence_total_members'
    case.clock[0] += registry.ttl_seconds
    charlie.join()
    assert set(registry.owners) == {charlie.owner}
    assert registry.total_members == 1
    assert len(registry.receipts) == 1
    for _ in range(registry.max_total_members - 1):
        charlie.run('heartbeat', {'doc_id': 'doc', 'member_id': 'driver'})
    _error('HOST_NOT_READY', lambda: charlie.run('heartbeat', {'doc_id': 'doc', 'member_id': 'driver'}))
    assert len(registry.receipts) == registry.max_total_members
    case.clock[0] += registry.ttl_seconds
    assert charlie.members()['members'] == []
    assert charlie.join()['member']['member_id'] == 'driver'


def test_collection_envelope_cas_durable_replay_and_restart(case, monkeypatch):
    case.join()
    state = {'b': [True, None, 2.5], 'a': {'nested': '正文'}}
    key = 'durable-propose-key-00000001'
    assert case.propose(state, key=key) == {'doc_id': 'doc', 'revision': 1}
    assert case.propose(state, key=key) == {'doc_id': 'doc', 'revision': 1}
    _error('IDEMPOTENCY_CONFLICT', lambda: case.propose({'changed': True}, key=key))
    case.join(doc='second')
    _error('REVISION_CONFLICT', lambda: case.propose('stale', doc='second', expected=0))
    assert case.get('second') == {'doc_id': 'second', 'revision': 1}
    assert case.propose('second', doc='second', expected=1)['revision'] == 2
    assert case.get()['revision'] == 2
    _error('REVISION_CONFLICT', lambda: case.propose('stale', expected=1))
    expected = {'doc_id': 'doc', 'revision': 2, 'state': json.loads(json.dumps(state)),
                'updated_by': 'driver', 'updated_at': case.clock[0]}
    assert case.get() == expected
    state['b'].append('调用方后来修改')
    assert case.get()['state']['b'] == [True, None, 2.5]
    store = V13Store(case.home, owner_id=case.owner)
    assert store.revision('collab_doc_state') == 2
    assert len(store.list('audit_log')) == 2
    monkeypatch.setattr(presence, '_presence', presence._PresenceRegistry())
    assert case.get()['revision'] == 2
    _error('VERIFICATION_FAILED', lambda: case.propose('no-live-lease', expected=2))
    case.join()
    # 同一个持久键重放返回原响应，不增加集合修订；新租约不能把回执误当新提交。
    assert case.propose(expected['state'], key=key)['revision'] == 1
    assert case.get()['revision'] == 2
    assert case.propose('after-rejoin', expected=2)['revision'] == 3


def test_persistent_document_count_is_bounded_too(case, monkeypatch):
    monkeypatch.setattr(presence, '_presence', presence._PresenceRegistry(max_members=2))
    for revision, doc_id in enumerate(('first', 'second')):
        case.join(doc=doc_id)
        assert case.propose(doc_id, doc=doc_id, expected=revision)['revision'] == revision + 1
    case.clock[0] += presence._presence.ttl_seconds
    case.join(doc='third')
    _error('LIMIT_EXCEEDED', lambda: case.propose('third', doc='third', expected=2))
    assert case.get('third') == {'doc_id': 'third', 'revision': 2}
    case.join(doc='first')
    assert case.propose('updated', doc='first', expected=2)['revision'] == 3


def test_canonical_utf8_size_inclusive_boundary_and_escaping(case):
    case.join()
    max_bytes = presence._bounds()[2]
    characters = (max_bytes - 2) // 3
    state = '汉' * characters + 'x' * (max_bytes - 2 - characters * 3)
    raw = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    assert len(raw) == max_bytes
    assert case.propose(state)['revision'] == 1
    _error('LIMIT_EXCEEDED', lambda: case.propose(state + 'x', expected=1))
    _error('LIMIT_EXCEEDED', lambda: case.propose('\\' * (max_bytes // 2), expected=1))
    assert case.get()['state'] == state and case.get()['revision'] == 1


@pytest.mark.parametrize('state', [None, True, False, 123, 1.25, 'text', ['a', None], {'z': 0}])
def test_all_json_root_types_roundtrip(case, state):
    case.join()
    assert case.propose(state)['revision'] == 1
    assert case.get()['state'] == state


@pytest.mark.parametrize('kind', ['nan', 'inf', 'negative-inf', 'nested-nan', 'key', 'tuple',
                                  'set', 'bytes', 'object', 'surrogate', 'cycle', 'deep'])
def test_non_json_state_rejected_without_commit(case, kind):
    states = {'nan': float('nan'), 'inf': float('inf'), 'negative-inf': -float('inf'),
              'nested-nan': {'x': float('nan')}, 'key': {1: 'not-a-JSON-key'},
              'tuple': ('tuple',), 'set': {'set'}, 'bytes': b'bytes', 'object': object(),
              'surrogate': '\ud800'}
    if kind == 'cycle':
        state = []
        state.append(state)
    elif kind == 'deep':
        state = None
        for _ in range(1500):
            state = [state]
    else:
        state = states[kind]
    case.join()
    _error('INVALID_SCHEMA', lambda: case.propose(state))
    assert case.get() == {'doc_id': 'doc', 'revision': 0}
    assert not case.home.exists()


@pytest.mark.parametrize('revision', [None, True, -1, '0', 0.0])
def test_propose_requires_envelope_integer_revision(case, revision):
    case.join()
    _error('INVALID_SCHEMA', lambda: case.propose({}, expected=revision))
    assert case.get()['revision'] == 0


def test_presence_and_missing_reads_do_not_create_owner_storage(case):
    assert not case.home.exists()
    assert case.members() == {'members': [], 'driver_member_id': ''}
    assert case.get() == {'doc_id': 'doc', 'revision': 0}
    case.join()
    case.run('heartbeat', {'doc_id': 'doc', 'member_id': 'driver'})
    case.members()
    case.run('leave', {'doc_id': 'doc', 'member_id': 'driver'})
    assert not case.home.exists()


def test_existing_read_paths_never_admit_lock_write_or_recover(case, monkeypatch):
    case.join()
    case.propose({'stable': True})
    base = case.home / 'data' / 'business_v13'
    paths = [base / name for name in ('collab_doc_state.json', 'audit_log.json', '.transaction.json')]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

    def forbidden(*args, **kwargs):
        pytest.fail('只读入口不能准入、加写锁、恢复或落盘')

    monkeypatch.setattr(v13_store, 'write_bytes_atomic_beneath', forbidden)
    monkeypatch.setattr(v13_store, 'locked_private_directory', forbidden)
    monkeypatch.setattr(v13_store, 'owner_quota_enforcer_from_policy', forbidden)
    monkeypatch.setattr(V13Store, '_recover', forbidden)
    assert case.get()['state'] == {'stable': True}
    assert case.get('absent') == {'doc_id': 'absent', 'revision': 1}
    case.clock[0] += presence._presence.ttl_seconds
    assert case.members()['members'] == []
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths} == before


@pytest.mark.parametrize(('action', 'params'), [
    ('join', {'doc_id': 'doc', 'member_id': 'driver', 'role': 'driver'}),
    ('heartbeat', {'doc_id': 'doc', 'member_id': 'driver'}),
    ('leave', {'doc_id': 'doc', 'member_id': 'driver'}),
    ('list', {'doc_id': 'doc'}), ('doc_state.get', {'doc_id': 'doc'}),
    ('doc_state.propose', {'doc_id': 'doc', 'member_id': 'driver', 'state': {}}),
])
def test_every_handler_requires_live_access(case, action, params):
    case.allowed = False
    _error('CAPABILITY_DENIED', lambda: case.run(action, params, expected=0))
    assert not case.home.exists() and presence._presence.owners == {}


def test_unissued_context_and_owner_param_cannot_grant_access(case):
    call = case.make('list', {'doc_id': 'doc'})
    unissued = api.BusinessCallContext(case.host, case.owner, 'actor', lambda action, facts: True)
    _error('CAPABILITY_DENIED', lambda: presence.list_members(replace(call, context=unissued)))
    _error('RESOURCE_NOT_FOUND', lambda: case.run('list', {'doc_id': 'doc', 'owner_id': 'local/bob'}))


def test_presence_idempotency_conflict_is_scoped_and_not_an_authorization(case):
    key = 'join-idempotency-key-0000001'
    first = case.join(key=key)
    assert case.join(key=key) == first
    _error('IDEMPOTENCY_CONFLICT', lambda: case.join(role='observer', key=key))
    case.join('new', takeover=True, expected_driver_member_id='driver')
    assert case.join(key=key) == first
    assert case.members()['driver_member_id'] == 'new'
    _error('VERIFICATION_FAILED', lambda: case.propose('old-receipt-is-not-a-lease'))
    case.allowed = False
    _error('CAPABILITY_DENIED', lambda: case.join(key=key))


def test_propose_holds_driver_lease_through_real_store_commit(case, monkeypatch):
    case.join('old')
    proposal = case.make('doc_state.propose',
        {'doc_id': 'doc', 'member_id': 'old', 'state': 'before-takeover'}, expected=0)
    takeover = case.make('join', {'doc_id': 'doc', 'member_id': 'new', 'role': 'driver',
        'takeover': True, 'expected_driver_member_id': 'old'})
    entered, release = threading.Event(), threading.Event()
    original = V13Store._materialized

    def pause_commit(store, pending):
        if store.action == 'collab_presence.doc_state.propose':
            entered.set()
            assert release.wait(5), '测试必须明确释放真实 Store 提交窗口'
        return original(store, pending)

    monkeypatch.setattr(V13Store, '_materialized', pause_commit)
    lock = _ObservedLock(presence._presence.lock, 'takeover-second')
    monkeypatch.setattr(presence._presence, 'lock', lock)
    first = _worker(lambda: presence.doc_state_propose(proposal), 'propose-first')
    second = None
    try:
        assert entered.wait(5)
        second = _worker(lambda: presence.join(takeover), 'takeover-second')
        assert lock.attempted.wait(5)
        assert not lock.acquired.is_set(), '接管不能穿过 driver 已授权但尚未提交的窗口'
    finally:
        release.set()
        first_result = _finish(first)
        if second is not None:
            second_result = _finish(second)
    assert first_result == {'result': {'doc_id': 'doc', 'revision': 1}}
    assert second_result['result']['driver_member_id'] == 'new'
    assert case.get()['state'] == 'before-takeover'
    _error('VERIFICATION_FAILED', lambda: case.propose('old-driver', member='old', expected=1))
    assert case.propose('new-driver', member='new', expected=1)['revision'] == 2


def test_takeover_wins_before_queued_propose_no_old_driver_write(case, monkeypatch):
    case.join('old')
    proposal = case.make('doc_state.propose',
        {'doc_id': 'doc', 'member_id': 'old', 'state': 'must-not-write'}, expected=0)
    takeover = case.make('join', {'doc_id': 'doc', 'member_id': 'new', 'role': 'driver',
        'takeover': True, 'expected_driver_member_id': 'old'})
    entered, release = threading.Event(), threading.Event()
    original = presence._Member.view

    def pause_takeover(member):
        if member.member_id == 'new' and threading.current_thread().name == 'takeover-first':
            entered.set()
            assert release.wait(5)
        return original(member)

    monkeypatch.setattr(presence._Member, 'view', pause_takeover)
    lock = _ObservedLock(presence._presence.lock, 'propose-second')
    monkeypatch.setattr(presence._presence, 'lock', lock)
    first = _worker(lambda: presence.join(takeover), 'takeover-first')
    second = None
    try:
        assert entered.wait(5)
        second = _worker(lambda: presence.doc_state_propose(proposal), 'propose-second')
        assert lock.attempted.wait(5)
        assert not lock.acquired.is_set()
    finally:
        release.set()
        first_result = _finish(first)
        if second is not None:
            second_result = _finish(second)
    assert first_result['result']['driver_member_id'] == 'new'
    assert second_result['error'].code == 'VERIFICATION_FAILED'
    assert case.get() == {'doc_id': 'doc', 'revision': 0}
    assert not case.home.exists()
    assert case.propose('current-driver', member='new')['revision'] == 1


def test_two_competing_takeovers_compare_inside_same_lock(case, monkeypatch):
    case.join('old')
    calls = [case.make('join', {'doc_id': 'doc', 'member_id': member, 'role': 'driver',
             'takeover': True, 'expected_driver_member_id': 'old'}) for member in ('first', 'second')]
    entered, release = threading.Event(), threading.Event()
    original = presence._Member.view

    def pause_first(member):
        if member.member_id == 'first' and threading.current_thread().name == 'first-takeover':
            entered.set()
            assert release.wait(5)
        return original(member)

    monkeypatch.setattr(presence._Member, 'view', pause_first)
    lock = _ObservedLock(presence._presence.lock, 'second-takeover')
    monkeypatch.setattr(presence._presence, 'lock', lock)
    first = _worker(lambda: presence.join(calls[0]), 'first-takeover')
    second = None
    try:
        assert entered.wait(5)
        second = _worker(lambda: presence.join(calls[1]), 'second-takeover')
        assert lock.attempted.wait(5)
        assert not lock.acquired.is_set()
    finally:
        release.set()
        first_result = _finish(first)
        if second is not None:
            second_result = _finish(second)
    assert first_result['result']['driver_member_id'] == 'first'
    assert second_result['error'].code == 'REVISION_CONFLICT'
    assert {row['member_id']: row['role'] for row in case.members()['members']} == {
        'old': 'navigator', 'first': 'driver'}


@pytest.mark.parametrize(('change', 'code'), [
    ('expire', 'VERIFICATION_FAILED'), ('revoke', 'CAPABILITY_DENIED')])
def test_commit_rechecks_ttl_and_live_authorization_after_store_work(case, monkeypatch, change, code):
    first = case.join()['member']
    entered, release = threading.Event(), threading.Event()
    original = V13Store._materialized

    def pause_commit(store, pending):
        if store.action == 'collab_presence.doc_state.propose':
            entered.set()
            assert release.wait(5)
        return original(store, pending)

    monkeypatch.setattr(V13Store, '_materialized', pause_commit)
    proposal = case.make('doc_state.propose',
        {'doc_id': 'doc', 'member_id': 'driver', 'state': 'must-not-commit'}, expected=0)
    worker = _worker(lambda: presence.doc_state_propose(proposal), 'commit-guard')
    try:
        assert entered.wait(5)
        if change == 'expire':
            case.clock[0] = first['expires_at']
        else:
            case.allowed = False
    finally:
        release.set()
        outcome = _finish(worker)
    assert outcome['error'].code == code
    case.allowed = True
    assert case.get() == {'doc_id': 'doc', 'revision': 0}
    store = V13Store(case.home, owner_id=case.owner)
    assert store.list('audit_log') == []
    case.join()
    assert case.propose('fresh-authorization')['revision'] == 1


def test_get_keeps_state_and_collection_revision_from_one_observation(case, monkeypatch):
    case.join()
    case.propose('original')
    case.join(doc='second')
    entered, release = threading.Event(), threading.Event()
    original = V13Store._record_view

    def pause_read(store, collection, record_id, **options):
        result = original(store, collection, record_id, **options)
        if threading.current_thread().name == 'snapshot-reader' and collection == 'collab_doc_state':
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(V13Store, '_record_view', pause_read)
    worker = _worker(case.get, 'snapshot-reader')
    try:
        assert entered.wait(5)
        assert case.propose('newer-collection', doc='second', expected=1)['revision'] == 2
    finally:
        release.set()
        outcome = _finish(worker)
    assert outcome['result'] == {'doc_id': 'doc', 'state': 'original', 'revision': 1,
        'updated_by': 'driver', 'updated_at': case.clock[0]}
    assert case.get()['revision'] == 2


def test_shared_limits_are_authoritative_when_available(case, monkeypatch):
    monkeypatch.setattr(presence, 'limits', SimpleNamespace(
        PRESENCE_TTL_SECONDS=7, PRESENCE_MAX_MEMBERS_PER_DOC=3, DOC_STATE_MAX_BYTES=32))
    monkeypatch.setattr(presence, '_presence', presence._PresenceRegistry())
    member = case.join()['member']
    assert member['expires_at'] - member['last_seen_at'] == 7
    assert presence._presence.max_members == 3
    assert case.propose('x' * 30)['revision'] == 1
    _error('LIMIT_EXCEEDED', lambda: case.propose('x' * 31, expected=1))


def test_live_receipt_capacity_cannot_reexecute_old_leave_on_new_lease(case, monkeypatch):
    registry = presence._PresenceRegistry(max_members=2)
    monkeypatch.setattr(presence, '_presence', registry)
    case.join()
    leave = case.make('leave', {'doc_id': 'doc', 'member_id': 'driver'})
    assert presence.leave(leave) == {'left': True}
    case.join()
    case.run('heartbeat', {'doc_id': 'doc', 'member_id': 'driver'})
    assert len(registry.receipts) == registry.max_total_members
    _error('HOST_NOT_READY', lambda: case.run('heartbeat', {'doc_id': 'doc', 'member_id': 'driver'}))
    assert presence.leave(leave) == {'left': True}
    assert case.members()['driver_member_id'] == 'driver'
