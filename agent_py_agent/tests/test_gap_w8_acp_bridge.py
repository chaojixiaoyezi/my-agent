"""W8 会话/控制回归：真实 stdio 假代理、真实 owner Store、显式时钟，无网络。"""
import hashlib
import io
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.business_spaces.gap158_w8_acp_bridge import bridge_for
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts.business_api import bind_business_context
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call


class Host:
    pass


@pytest.fixture
def environment(tmp_path):
    hosts, bridges = [], []

    def make(owner='alice', flags=()):
        host = Host()
        home = tmp_path / owner
        root = home / 'workspace'
        (root / 'project').mkdir(parents=True, exist_ok=True)
        host.home_paths = SimpleNamespace(root=tmp_path, owner_id=owner, owner_home_dir=home)
        deployment_config = tmp_path / 'config' / 'capability_config.yaml'
        deployment_config.parent.mkdir(exist_ok=True)
        deployment_config.write_text('tool_approval_timeout_seconds: 5\n')
        host.root, host.workspace_roots, host.authorized = root, [root], True
        host.effective_workspace_root, host.effective_workspace_roots = root, [root]
        fixture = Path(__file__).parent / 'fixtures' / 'gap_w8_fake_acp_agent.py'
        host.config = SimpleNamespace(business_spaces_enabled=True, acp_bridge_enabled=True,
            acp_bridge_agents={'fake': {'command': [sys.executable, str(fixture), *flags],
                'env_allowlist': [], 'display_name': '测试代理'}},
            tool_read_max_chars=131072, mcp_max_line_chars=1048576)
        host.capability_config = SimpleNamespace(tool_approval_timeout_seconds=5)
        clock = [1000.0]
        context = bind_business_context(host, authorize=lambda action, facts: host.authorized, subject_id=owner)
        services = SimpleNamespace(host=host, owner_id=owner, owner_home=home)

        def call(action, params=None, *, key=None):
            invocation = V13Call('acp_bridge.' + action, params or {}, uuid4().hex, owner, owner,
                context, services, home, key or uuid4().hex, clock=lambda: clock[0])
            bridge = bridge_for(invocation)
            if bridge not in bridges:
                bridges.append(bridge)
            return bridge.invoke(invocation)

        hosts.append(host)
        return call, host, clock

    yield make
    failures = []
    for bridge in bridges:
        for receipt in bridge.shutdown().values():
            if not receipt['process_group_released']:
                failures.append(receipt)
    assert not failures, failures


def create(call, **extra):
    return call('create', {'agent_id': 'fake', 'project_root': 'project', **extra})['session']['session_id']


def wait_event(call, session, kind, *, turn_id=None):
    # 测试只以有界条件等待观察已发表事件，失败保留整个有限页。
    deadline = threading.Event()
    for _ in range(400):
        page = call('events', {'session_id': session, 'after_seq': 0, 'limit': 100})
        matches = [event for event in page['events'] if event['kind'] == kind
                   and (turn_id is None or event.get('turn_id') == turn_id)]
        if matches:
            return matches[-1]
        deadline.wait(0.005)
    pytest.fail(f'{kind} not observed: {page}')


def prompt(call, session, text):
    return call('prompt', {'session_id': session, 'content': [{'type': 'text', 'text': text}]})


def test_catalog_probe_and_ensure_identity(environment):
    call, host, _ = environment()
    assert call('catalog')['agents'][0]['probe_state'] == 'unknown'
    assert call('probe', {'agent_id': 'fake'})['ok'] is True
    assert call('catalog')['agents'][0]['protocol_version'] == '1'
    first = call('create', {'agent_id': 'fake', 'project_root': 'project', 'client_session_key': 'stable'})
    second = call('create', {'agent_id': 'fake', 'project_root': 'project', 'client_session_key': 'stable'})
    assert first['created'] and not second['created']
    assert first['session']['session_id'] == second['session']['session_id']
    row = V13Store(host.home_paths.owner_home_dir, owner_id='alice').get('acp_sessions', first['session']['session_id'])
    assert (row['owner_id'], row['project_root'], row['runtime_id'], row['acp_session_id']) == (
        'alice', 'project', 'fake@1', 'fake-session')


def test_owner_isolation_for_session_ensure_and_events(environment):
    alice, _, _ = environment('alice')
    bob, _, _ = environment('bob')
    sid = create(alice, client_session_key='same')
    assert create(bob, client_session_key='same') != sid
    for action, params in [('events', {'after_seq': 0}), ('close', {}), ('permission.decide', {
            'permission_request_id': 'missing', 'decision': 'approved'})]:
        with pytest.raises(BusinessError) as error:
            bob(action, {'session_id': sid, **params})
        assert error.value.code == 'RESOURCE_NOT_FOUND'


@pytest.mark.parametrize('path', ['../outside', '/absolute', '-option', 'a/../project', 'project\x00'])
def test_project_traversal_rejected(environment, path):
    call, _, _ = environment()
    with pytest.raises(BusinessError) as error:
        call('create', {'agent_id': 'fake', 'project_root': path})
    assert error.value.code == 'PATH_OUTSIDE_WORKSPACE'


def test_prompt_idempotency_cancel_and_completed_race(environment):
    call, _, _ = environment()
    sid = create(call)
    params = {'session_id': sid, 'content': [{'type': 'text', 'text': 'wait'}]}
    turn = call('prompt', params, key='same-idempotency-key')
    assert call('prompt', params, key='same-idempotency-key') == turn
    wait_event(call, sid, 'agent_message')
    with pytest.raises(BusinessError) as error:
        prompt(call, sid, 'another')
    assert error.value.code == 'STATE_CONFLICT'
    result = call('cancel_turn', {'session_id': sid, 'turn_id': turn['turn_id']})
    assert result['state'] in ('cancelling', 'cancelled')
    wait_event(call, sid, 'turn_completed')
    turn = prompt(call, sid, 'hello')
    wait_event(call, sid, 'turn_completed', turn_id=turn['turn_id'])
    result = call('cancel_turn', {'session_id': sid, 'turn_id': turn['turn_id']})
    assert result['state'] == 'completed'


@pytest.mark.parametrize('decision,option', [('approved', 'allow'), ('denied', 'deny'), ('expired', None)])
def test_permissions_approve_deny_and_expiry(environment, decision, option):
    call, _, clock = environment()
    sid = create(call)
    prompt(call, sid, 'permission')
    event = wait_event(call, sid, 'permission_requested')
    identifier = event['data']['permission_request_id']
    if decision == 'expired':
        clock[0] += 6
    result = call('permission.decide', {'session_id': sid, 'permission_request_id': identifier,
        'decision': 'denied' if decision == 'expired' else decision,
        **({'option_id': option} if option else {})})
    assert result['state'] == decision
    terminal = wait_event(call, sid, 'turn_completed')
    assert terminal['data']['stop_reason'] == 'end_turn'
    repeated = {'session_id': sid, 'permission_request_id': identifier,
                'decision': 'approved', 'option_id': 'allow'}
    if decision == 'expired':
        # 合同明确允许返回expired；再次批准必须仍是expired，不能转回approved。
        assert call('permission.decide', repeated)['state'] == 'expired'
    else:
        with pytest.raises(BusinessError):
            call('permission.decide', repeated)


def test_question_and_config_states_are_separate(environment):
    call, _, _ = environment()
    sid = create(call)
    assert call('config.set', {'session_id': sid, 'key': 'mode', 'value': 'code'})['state'] == 'pending_next_turn'
    prompt(call, sid, 'config')
    wait_event(call, sid, 'turn_completed')
    states = [event['data']['state'] for event in call('events', {'session_id': sid,
        'after_seq': 0})['events'] if event['kind'] == 'config_state' and 'state' in event.get('data', {})]
    assert states == ['pending_next_turn', 'command_ack', 'observed']
    prompt(call, sid, 'question')
    event = wait_event(call, sid, 'question_asked')
    assert call('answer', {'session_id': sid, 'question_id': event['data']['question_id'], 'answer': 'label'})['state'] == 'answered'
    wait_event(call, sid, 'question_answered')


def test_fs_confinement_sha_cas_and_unknown_method(environment):
    call, host, _ = environment()
    sid = create(call)
    target = host.root / 'project' / 'file.txt'
    target.write_text('original')

    def rpc(method, **params):
        turn = prompt(call, sid, 'request:' + json.dumps({'method': method,
            'params': {'sessionId': 'fake-session', **params}}))
        for _ in range(400):
            page = call('events', {'session_id': sid, 'after_seq': 0, 'limit': 100})
            rows = [event for event in page['events'] if event.get('turn_id') == turn['turn_id']]
            if any(event['kind'] == 'turn_completed' for event in rows):
                return json.loads(next(event for event in rows if event['kind'] == 'agent_message')['data']['content']['text'])
            threading.Event().wait(0.005)
        pytest.fail(str(page))

    assert rpc('fs/read_text_file', path=str(target))['result']['content'] == 'original'
    target.write_text('external')
    assert rpc('fs/write_text_file', path=str(target), content='stale')['error']['code'] == -32602
    assert target.read_text() == 'external'
    read = rpc('fs/read_text_file', path=str(target))
    assert rpc('fs/write_text_file', path=str(target), content='new',
        _meta={'expected_sha256': read['result']['_meta']['sha256']})['result'] == {}
    assert target.read_text() == 'new'
    assert rpc('fs/read_text_file', path=str(host.root / 'outside.txt'))['error']['code'] == -32602
    assert rpc('unknown/client')['error']['code'] == -32601


def test_overflow_pagination_limit_and_no_transcript(environment):
    call, host, _ = environment()
    sid = create(call)
    prompt(call, sid, 'burst:2100')
    # 从尾部窗口观察完成，不让 overflow 重复掩盖最后事件。
    for _ in range(400):
        page = call('events', {'session_id': sid, 'after_seq': 2090, 'limit': 100})
        if any(event['kind'] == 'turn_completed' for event in page['events']):
            break
        threading.Event().wait(0.005)
    assert any(event['kind'] == 'turn_completed' for event in page['events'])
    page = call('events', {'session_id': sid, 'after_seq': 0, 'limit': 2})
    assert page['overflow'] and page['events'][0]['kind'] == 'overflow' and len(page['events']) == 2
    create(call, client_session_key='two')
    page = call('list', {'limit': 1})
    assert page['coverage'] == 'partial' and len(page['items']) == 1
    assert len(call('list', {'limit': 1, 'cursor': page['next_cursor']})['items']) == 1
    data = V13Store(host.home_paths.owner_home_dir, owner_id='alice').list('acp_sessions')
    assert all('events' not in row and 'content' not in row for row in data)


def test_exit_mid_turn_and_restart_close_receipts(environment):
    call, _, _ = environment(flags=('--child',))
    sid = create(call)
    restarted = call('restart', {'session_id': sid})
    assert restarted['cleanup']['process_group_released']
    prompt(call, sid, 'exit')
    wait_event(call, sid, 'turn_failed')
    wait_event(call, sid, 'process_exited')
    session = call('list')['items'][0]
    assert session['state'] == 'failed' and not session['process_alive']
    closed = call('close', {'session_id': sid})
    assert closed['closed'] and closed['cleanup']['process_group_released']


@pytest.mark.parametrize('flags,supported', [((), True), (('--no-load',), False)])
def test_resume_with_and_without_load_session(environment, flags, supported):
    call, _, _ = environment(flags=flags)
    sid = create(call)
    call('close', {'session_id': sid})
    if supported:
        assert call('resume', {'session_id': sid})['session']['state'] == 'ready'
    else:
        with pytest.raises(BusinessError) as error:
            call('resume', {'session_id': sid})
        assert error.value.code == 'STATE_CONFLICT'


def test_runtime_revocation_closes_and_keeps_receipt(environment):
    call, host, _ = environment()
    sid = create(call)
    bridge = host._gap158_acp_bridge
    host.authorized = False
    assert bridge.sessions[sid].stopped.wait(3)
    assert bridge.sessions[sid].cleanup['process_group_released']


def test_switch_off_catalog_and_input_argv_rejected(environment):
    call, host, _ = environment()
    host.config.acp_bridge_enabled = False
    assert call('catalog')['enabled'] is False
    with pytest.raises(BusinessError) as error:
        create(call)
    assert error.value.code == 'HOST_NOT_READY'


def test_slice_declares_exact_actions_and_rejects_argv(monkeypatch, environment):
    from agent_py_agent.agent.gateway_parts import business_api_v13 as v13
    from agent_py_agent.agent.gateway_parts import business_api_v13_gap158_w8 as slice_module
    expected = {'catalog', 'probe', 'create', 'resume', 'restart', 'prompt', 'cancel_turn', 'events',
                'permission.decide', 'answer', 'config.set', 'close', 'list'}
    assert {action.name.removeprefix('acp_bridge.') for action in slice_module.V13_ACTIONS} == expected
    actions, aliases, definitions, kinds, collections = v13._load_registry([*v13.V13_SLICE_MODULES, slice_module.__name__])
    monkeypatch.setattr(v13, '_ACTIONS', actions)
    monkeypatch.setattr(v13, 'ALIASES', aliases)
    monkeypatch.setattr(v13, 'V13_DEFS', definitions)
    monkeypatch.setattr(v13, 'COLLECTIONS', collections)
    call, host, _ = environment()
    assert v13.v13_validate_params('acp_bridge.create', {'agent_id': 'fake', 'project_root': 'project'})
    with pytest.raises(Exception) as error:
        v13.v13_validate_params('acp_bridge.create', {'agent_id': 'fake', 'project_root': 'project', 'command': ['bad']})
    assert getattr(error.value, 'code', '') == 'INVALID_SCHEMA'
    definition = actions['acp_bridge.create']
    assert definition.ready(SimpleNamespace(services=SimpleNamespace(host=host)))
    host.config.acp_bridge_enabled = False
    assert not definition.ready(SimpleNamespace(services=SimpleNamespace(host=host)))
    assert actions['acp_bridge.catalog'].ready is None
    v13.v13_validate_result('acp_bridge.catalog', call('catalog'))


class MemoryTransport:
    """仅用于状态机交错，不作为真实进程清理的证明。"""
    def __init__(self, *, confirmed=True):
        self.confirmed, self.stops, self.replies = confirmed, 0, []

    def is_running(self):
        return self.stops == 0

    def terminate(self, *, grace_seconds):
        from agent_py_agent.agent.tooling.process_registry import ProcessTerminationReceipt
        self.stops += 1
        return ProcessTerminationReceipt('fixture', self.confirmed, 0 if self.confirmed else None, 1)

    def reply(self, identifier, **value):
        self.replies.append((identifier, value))


def memory_session(call, host):
    from agent_py_agent.agent.business_spaces.gap158_w8_acp_bridge import AcpSession
    call('catalog')
    bridge = host._gap158_acp_bridge
    record = {'session_id': 'memory-session', 'owner_id': host.home_paths.owner_id,
              'agent_id': 'fake', 'project_root': 'project', 'canonical_project_root': str(host.root / 'project'),
              'runtime_id': 'fake@1', 'acp_session_id': 'fake-session', 'state': 'running',
              'turn_id': 'old-turn', 'created_at': 1000}
    session = AcpSession(record, None, lambda: True, lambda: 1000, host.root / 'project')
    session.transport, session.generation = MemoryTransport(), 'old-generation'
    bridge.sessions[record['session_id']] = session
    return bridge, session


def test_effective_workspace_is_the_only_launch_scope(environment):
    call, host, _ = environment()
    effective = host.root / 'restricted'
    effective.mkdir()
    host.effective_workspace_root, host.effective_workspace_roots = effective, [effective]
    call('catalog')
    bridge = host._gap158_acp_bridge
    assert bridge.root == effective
    with pytest.raises(BusinessError):
        bridge._project('../project')
    # 有效根在宿主收窄之后，已缓存的旧根也不能继续授权。
    host.effective_workspace_root, host.effective_workspace_roots = host.root / 'other', [host.root / 'other']
    with pytest.raises(BusinessError):
        bridge._project('.')


def test_old_turn_cannot_fail_or_stop_new_transport(environment, monkeypatch):
    from agent_py_agent.agent.tooling.mcp_protocol import MCPError
    call, host, _ = environment()
    bridge, session = memory_session(call, host)
    entered, release = threading.Event(), threading.Event()

    def request(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        raise MCPError('old connection ended')

    session.transport.request = request
    monkeypatch.setattr(bridge, '_save', lambda item, **kwargs: None)
    worker = threading.Thread(target=bridge._turn, args=(session, []))
    worker.start()
    assert entered.wait(2)
    fresh = MemoryTransport()
    session.transport, session.generation = fresh, 'new-generation'
    session.record.update(state='ready', turn_id='new-turn')
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert fresh.stops == 0 and session.record['state'] == 'ready'
    assert session.record['turn_id'] == 'new-turn'


def test_pending_permissions_expire_on_original_connection(environment):
    call, host, _ = environment()
    bridge, session = memory_session(call, host)
    old = session.transport
    bridge._request(session, session.generation, 'session/request_permission', {
        'sessionId': 'fake-session', 'toolCall': {'toolCallId': 't'},
        'options': [{'optionId': 'allow', 'kind': 'allow_once'}]}, 'reused-rpc')
    identifier = next(iter(session.permissions))
    bridge._stop(session)
    fresh = MemoryTransport()
    session.transport, session.generation, session.closing = fresh, 'new', False
    result = bridge._decide(session, {'permission_request_id': identifier, 'decision': 'approved', 'option_id': 'allow'})
    assert result['state'] == 'expired'
    assert fresh.replies == []
    assert old.replies[0][1]['result'] == {'outcome': {'outcome': 'cancelled'}}


def test_unknown_persisted_cleanup_never_becomes_success(environment):
    call, host, _ = environment()
    bridge, session = memory_session(call, host)
    session.transport = None
    session.record['cleanup'] = {'terminated': False, 'process_group_released': False, 'duration_ms': 0}
    session.cleanup = dict(session.record['cleanup'])
    result = bridge._stop(session)
    assert not result['process_group_released'] and session.record['state'] == 'failed'
    # 纯内存单元对象没有真实资源，断言后不交给真实资源fixture反复清理。
    bridge.sessions.pop(session.record['session_id'])


def test_unknown_probe_is_bounded_and_retained_for_shutdown(environment, monkeypatch):
    call, host, _ = environment()
    call('catalog')
    bridge, launches = host._gap158_acp_bridge, []

    def launch(*args, **kwargs):
        transport = MemoryTransport(confirmed=False)
        transport.initialize = lambda: {'protocolVersion': 1, 'agentCapabilities': {}}
        launches.append(transport)
        return transport

    monkeypatch.setattr(bridge, '_launch', launch)
    for _ in range(6):
        with pytest.raises(BusinessError):
            call('probe', {'agent_id': 'fake'})
    assert len(launches) == 1
    receipts = bridge.shutdown()
    assert receipts and not all(row['process_group_released'] for row in receipts.values())
    assert launches[0].stops >= 2
    # 原未知资源保持未知；fixture不额外声称合成进程已被回收。
    launches[0].confirmed = True


def test_shutdown_fences_inflight_creation(environment, monkeypatch):
    call, host, _ = environment()
    call('catalog')
    bridge = host._gap158_acp_bridge
    entered, release = threading.Event(), threading.Event()
    transport = MemoryTransport()

    def start(session, **kwargs):
        entered.set()
        assert release.wait(2)
        session.transport = transport
        session.record.update(state='ready', acp_session_id='fake-session')

    monkeypatch.setattr(bridge, '_start', start)
    result = []
    creator = threading.Thread(target=lambda: result.append(create(call)))
    creator.start()
    assert entered.wait(2)
    closed = []
    stopper = threading.Thread(target=lambda: closed.append(bridge.shutdown()))
    stopper.start()
    release.set()
    creator.join(2)
    stopper.join(2)
    assert not creator.is_alive() and not stopper.is_alive()
    assert result and closed and transport.stops >= 1


def test_file_cas_uses_one_owner_lock_for_overlapping_projects(environment, monkeypatch):
    from agent_py_agent.agent.business_spaces import gap158_w8_acp_bridge as module
    call, host, _ = environment()
    bridge, left = memory_session(call, host)
    child = left.root / 'subdir'
    child.mkdir()
    target = child / 'shared.txt'
    target.write_text('before')
    _, right = memory_session(call, host)
    right.root = child
    original = module.read_bytes_beneath
    entered, second, release = threading.Event(), threading.Event(), threading.Event()

    def read(root, parts, **kwargs):
        raw = original(root, parts, **kwargs)
        if threading.current_thread().name == 'first-cas':
            entered.set()
            assert release.wait(2)
        else:
            second.set()
        return raw

    monkeypatch.setattr(module, 'read_bytes_beneath', read)
    import hashlib
    expected = hashlib.sha256(b'before').hexdigest()
    outcomes = []

    def write(session, value):
        try:
            bridge._file(session, 'fs/write_text_file', {'path': str(target), 'content': value,
                '_meta': {'expected_sha256': expected}})
            outcomes.append('written')
        except BusinessError as error:
            outcomes.append(error.code)

    first = threading.Thread(target=write, args=(left, 'left'), name='first-cas')
    later = threading.Thread(target=write, args=(right, 'right'), name='second-cas')
    first.start()
    assert entered.wait(2)
    later.start()
    second.wait(0.05)
    release.set()
    first.join(2)
    later.join(2)
    assert sorted(outcomes) == ['STATE_CONFLICT', 'written']


def test_restored_identity_rejects_retargeted_project_symlink(environment):
    call, host, _ = environment()
    call('catalog')
    bridge = host._gap158_acp_bridge
    original, changed = host.root / 'project', host.root / 'changed'
    changed.mkdir()
    alias = host.root / 'alias'
    alias.symlink_to(changed, target_is_directory=True)
    record = {'session_id': 'restored', 'owner_id': host.home_paths.owner_id,
        'project_root': 'alias', 'canonical_project_root': str(original), 'agent_id': 'fake',
        'runtime_id': 'fake@1', 'acp_session_id': 'fake-session', 'state': 'closed',
        'created_at': 1000, 'cleanup': {'terminated': True, 'process_group_released': True, 'duration_ms': 0}}
    context = bind_business_context(host, authorize=lambda action, facts: True, subject_id='alice')
    invocation = V13Call('acp_bridge.resume', {'session_id': 'restored'}, 'r', host.home_paths.owner_id,
        'alice', context, SimpleNamespace(owner_id=host.home_paths.owner_id), host.home_paths.owner_home_dir)
    with pytest.raises(BusinessError) as error:
        bridge._owned(invocation, {'restored': record})
    assert error.value.code == 'STATE_CONFLICT'


def test_initialization_does_not_hold_owner_store_transaction(environment, monkeypatch):
    call, host, _ = environment()
    call('catalog')
    bridge = host._gap158_acp_bridge
    entered, release, written = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def start(session, **kwargs):
        session.transport = MemoryTransport()
        entered.set()
        assert release.wait(3)
        session.record.update(state='ready', acp_session_id='fake-session')

    def create_in_thread():
        try:
            create(call)
        except Exception as error:
            errors.append(error)

    def unrelated_write():
        store = V13Store(host.home_paths.owner_home_dir, owner_id=host.home_paths.owner_id)
        store.transaction('unrelated', lambda records: {}, idempotency_key=uuid4().hex, params_digest='other')
        written.set()

    monkeypatch.setattr(bridge, '_start', start)
    creator = threading.Thread(target=create_in_thread)
    writer = threading.Thread(target=unrelated_write)
    creator.start()
    assert entered.wait(2)
    writer.start()
    completed_during_initialize = written.wait(0.3)
    release.set()
    creator.join(3)
    writer.join(3)
    assert not creator.is_alive() and not writer.is_alive() and not errors
    assert completed_during_initialize, 'ACP 握手不能占住整个 owner 的 Store 事务'


class LoopbackTransport(MemoryTransport):
    """额外验证业务状态机；没有 OS 进程，不替代上面的真实 stdio/进程组测试。"""

    def __init__(self, callbacks, *, load=True):
        super().__init__()
        self.callbacks, self.agent_capabilities = callbacks, {'loadSession': load}
        self.completed = threading.Event()
        self.stop_reason, self.mode, self.requests = 'end_turn', 'ask', []

    def initialize(self):
        return {'protocolVersion': 1, 'agentCapabilities': self.agent_capabilities}

    def request(self, method, params, *, timeout, authority_check=None):
        if authority_check:
            authority_check()
        self.requests.append((method, params))
        if method in ('session/new', 'session/load'):
            return {'sessionId': 'fake-session'}
        if method == 'session/set_mode':
            self.mode = params['modeId']
            return {}
        if method == 'session/prompt':
            self.completed.clear()
            self.stop_reason = 'end_turn'
            text = ''.join(block.get('text', '') for block in params['prompt'])
            if text == 'permission':
                self.callbacks['on_request']('session/request_permission', {
                    'sessionId': 'fake-session', 'toolCall': {'toolCallId': 't'},
                    'options': [{'optionId': 'allow', 'kind': 'allow_once'},
                                {'optionId': 'deny', 'kind': 'reject_once'}]}, 'rpc')
                assert self.completed.wait(2)
            elif text == 'question':
                self.callbacks['on_request']('elicitation/create', {
                    'sessionId': 'fake-session', 'mode': 'form', 'message': 'label',
                    'requestedSchema': {'type': 'object', 'properties': {'answer': {'type': 'string'}},
                                        'required': ['answer']}}, 'rpc')
                assert self.completed.wait(2)
            else:
                self.callbacks['on_update']({'sessionId': 'fake-session', 'update': {
                    'sessionUpdate': 'current_mode_update', 'currentModeId': self.mode}})
            return {'stopReason': self.stop_reason}
        raise AssertionError(method)

    def reply(self, identifier, **value):
        super().reply(identifier, **value)
        self.completed.set()

    def notify(self, method, params):
        assert method == 'session/cancel'
        self.stop_reason = 'cancelled'
        self.completed.set()


def test_in_memory_bridge_sequence_retains_idempotency_and_live_ensure(environment, monkeypatch):
    call, host, _ = environment()
    call('catalog')
    bridge = host._gap158_acp_bridge
    launches = []

    def launch(*args, **kwargs):
        transport = LoopbackTransport(kwargs)
        launches.append(transport)
        return transport

    monkeypatch.setattr(bridge, '_launch', launch)
    params = {'agent_id': 'fake', 'project_root': 'project'}
    first = call('create', params, key='stable-create-key')
    assert call('create', params, key='stable-create-key') == first
    assert not call('create', params)['created'] and len(launches) == 1
    sid = first['session']['session_id']
    assert call('config.set', {'session_id': sid, 'key': 'mode', 'value': 'code'})['state'] == 'pending_next_turn'
    turn = prompt(call, sid, 'hello')
    wait_event(call, sid, 'turn_completed', turn_id=turn['turn_id'])
    states = [item['data']['state'] for item in call('events', {'session_id': sid, 'after_seq': 0})['events']
              if item['kind'] == 'config_state' and 'state' in item['data']]
    assert states == ['pending_next_turn', 'command_ack', 'observed']
    turn = prompt(call, sid, 'permission')
    event = wait_event(call, sid, 'permission_requested', turn_id=turn['turn_id'])
    identifier = event['data']['permission_request_id']
    assert call('permission.decide', {'session_id': sid, 'permission_request_id': identifier,
        'decision': 'approved', 'option_id': 'allow'})['state'] == 'approved'
    wait_event(call, sid, 'turn_completed', turn_id=turn['turn_id'])
    turn = prompt(call, sid, 'question')
    question = wait_event(call, sid, 'question_asked', turn_id=turn['turn_id'])
    assert call('answer', {'session_id': sid, 'question_id': question['data']['question_id'],
        'answer': 'chosen'})['state'] == 'answered'
    wait_event(call, sid, 'turn_completed', turn_id=turn['turn_id'])
    assert call('close', {'session_id': sid})['closed']
    replacement = call('create', params)
    assert replacement['created'] and replacement['session']['session_id'] != sid
    assert len(launches) == 2


def test_failed_create_preserves_unknown_resource_and_original_reservation(environment, monkeypatch):
    from agent_py_agent.agent.tooling.mcp_protocol import MCPError
    call, host, _ = environment()
    call('catalog')
    bridge = host._gap158_acp_bridge
    original = MemoryTransport(confirmed=False)

    def launch(*args, **kwargs):
        def initialize():
            raise MCPError('失败夹具')
        original.initialize = initialize
        return original

    monkeypatch.setattr(bridge, '_launch', launch)
    params = {'agent_id': 'fake', 'project_root': 'project'}
    with pytest.raises(BusinessError) as error:
        call('create', params, key='uncertain-create-key')
    assert error.value.code == 'EXTERNAL_PROCESS_FAILED'
    rows = V13Store(host.home_paths.owner_home_dir, owner_id='alice').list('acp_sessions')
    assert len(rows) == 1 and rows[0]['state'] == 'failed'
    assert not rows[0]['cleanup']['process_group_released']
    with pytest.raises(BusinessError) as replay:
        call('create', params, key='uncertain-create-key')
    assert replay.value.code == 'OUTCOME_UNKNOWN'
    with pytest.raises(BusinessError) as retry:
        call('create', params)
    assert retry.value.code == 'EXTERNAL_PROCESS_FAILED'
    assert len(bridge.sessions) == 1
    original.confirmed = True


@pytest.mark.parametrize('method', ['fs/read_text_file', 'fs/write_text_file'])
@pytest.mark.parametrize('during_io', [False, True])
def test_fs_rejects_replaced_project_ancestor(environment, tmp_path, monkeypatch, method, during_io):
    from agent_py_agent.agent.business_spaces import gap158_w8_acp_bridge as module

    call, host, _ = environment()
    bridge, session = memory_session(call, host)
    container = host.root / 'container'
    project = container / 'project'
    project.mkdir(parents=True)
    session.root = project
    target = project / 'file.txt'
    target.write_text('before')
    outside = tmp_path / 'outside'
    (outside / 'project').mkdir(parents=True)
    outside_target = outside / 'project' / 'file.txt'
    outside_target.write_text('before')
    entered, release = threading.Event(), threading.Event()
    original_read = module.read_bytes_beneath

    def read(root, parts, **kwargs):
        entered.set()
        assert release.wait(2)
        return original_read(root, parts, **kwargs)

    def replace_ancestor():
        container.rename(host.root / 'original-container')
        container.symlink_to(outside, target_is_directory=True)

    outcomes = []

    def operation():
        try:
            bridge._file(session, method, {'path': str(target), 'content': 'changed',
                '_meta': {'expected_sha256': hashlib.sha256(b'before').hexdigest()}})
            outcomes.append('escaped')
        except (BusinessError, ValueError, OSError):
            outcomes.append('rejected')

    if during_io:
        monkeypatch.setattr(module, 'read_bytes_beneath', read)
        worker = threading.Thread(target=operation)
        worker.start()
        assert entered.wait(2)
        replace_ancestor()
        release.set()
        worker.join(2)
        assert not worker.is_alive()
    else:
        replace_ancestor()
        operation()
    assert outcomes == ['rejected']
    assert outside_target.read_text() == 'before'


def test_approval_timeout_uses_deployment_source_and_hot_changes(environment):
    call, host, _ = environment()
    bridge, session = memory_session(call, host)
    config = host.home_paths.root / 'config' / 'capability_config.yaml'
    for timeout in (7, 11):
        config.write_text(f'tool_approval_timeout_seconds: {timeout}\n')
        bridge._request(session, session.generation, 'session/request_permission', {
            'sessionId': 'fake-session', 'toolCall': {'toolCallId': str(timeout)},
            'options': [{'optionId': 'allow', 'kind': 'allow_once'}]}, f'rpc-{timeout}')
        item = list(session.permissions.values())[-1]
        assert item['expires_at'] == session.clock() + timeout


def test_broken_deployment_approval_config_is_not_replaced_by_defaults(environment):
    call, host, _ = environment()
    bridge, session = memory_session(call, host)
    config = host.home_paths.root / 'config' / 'capability_config.yaml'
    config.write_text('tool_approval_timeout_seconds: invalid\n')
    with pytest.raises(BusinessError) as error:
        bridge._request(session, session.generation, 'session/request_permission', {
            'sessionId': 'fake-session', 'toolCall': {'toolCallId': 't'},
            'options': [{'optionId': 'allow', 'kind': 'allow_once'}]}, 'rpc')
    assert error.value.code == 'HOST_NOT_READY'
    assert session.permissions == {} and session.questions == {}


def test_malformed_wire_error_cannot_acknowledge_pending_config(environment, monkeypatch):
    from agent_py_agent.agent.business_spaces.gap158_w8_acp_protocol import AcpTransport, read_frame
    from agent_py_agent.agent.tooling.mcp_protocol import MCPError, MCPInbox

    call, host, _ = environment()
    bridge, session = memory_session(call, host)
    connection = AcpTransport.__new__(AcpTransport)
    connection.inbox, connection._next_id = MCPInbox('acp'), 1

    def receive(message, **kwargs):
        # 只替换字节管道，保留生产封套解析、响应箱和配置状态机。
        raw = json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'error': None}).encode() + b'\n'
        connection._dispatch(read_frame(io.BytesIO(raw), 4096))

    monkeypatch.setattr(connection, 'send', receive)
    session.configs['mode'] = {'state': 'pending_next_turn', 'value': 'code'}
    with pytest.raises(MCPError) as error:
        bridge._apply_config(session, connection, session.generation, session.authority)
    assert error.value.code == 'MCP_PROTOCOL_ERROR'
    assert session.configs['mode']['state'] == 'pending_next_turn'
    assert not any(event['data'].get('state') == 'command_ack' for event in session.events)
    assert not connection.inbox.pending and not connection.inbox.responses


@pytest.mark.parametrize('reader_first', [False, True])
def test_exit_and_turn_failure_publish_once_in_either_order(environment, monkeypatch, reader_first):
    from agent_py_agent.agent.tooling.mcp_protocol import MCPError

    call, host, _ = environment()
    bridge, session = memory_session(call, host)
    session.turn_state = 'running'
    generation = session.generation
    transport = session.transport
    monkeypatch.setattr(bridge, '_save', lambda item, **kwargs: None)

    def request(*args, **kwargs):
        # 两个顺序对应读线程关闭响应箱后，退出回调与被唤醒回合的确定性交错。
        if reader_first:
            bridge._exited(session, generation)
        raise MCPError('原连接已关闭', code='MCP_CONNECTION_CLOSED')

    transport.request = request
    bridge._turn(session, [])
    bridge._exited(session, generation)
    bridge._exited(session, generation)
    kinds = [event['kind'] for event in session.events]
    assert kinds.count('turn_failed') == 1
    assert kinds.count('process_exited') == 1
    assert session.record['state'] == session.turn_state == 'failed'
    assert session.cleanup['process_group_released']
    assert transport.stops == 1


def test_exit_during_failure_cleanup_does_not_reenter_termination(environment, monkeypatch):
    from agent_py_agent.agent.tooling.mcp_protocol import MCPError

    call, host, _ = environment()
    bridge, session = memory_session(call, host)
    session.turn_state = 'running'
    transport = session.transport
    generation = session.generation
    original_terminate = transport.terminate
    monkeypatch.setattr(bridge, '_save', lambda item, **kwargs: None)

    def request(*args, **kwargs):
        raise MCPError('原连接已关闭', code='MCP_CONNECTION_CLOSED')

    def terminate(*, grace_seconds):
        receipt = original_terminate(grace_seconds=grace_seconds)
        # 清理已在进行，迟到退出回调只补事实，不能再抢同一清理责任。
        bridge._exited(session, generation)
        return receipt

    transport.request, transport.terminate = request, terminate
    bridge._turn(session, [])
    assert [event['kind'] for event in session.events] == ['turn_failed', 'process_exited']
    assert transport.stops == 1 and session.cleanup['process_group_released']


def test_explicit_close_ignores_late_exit_failure_callback(environment):
    call, host, _ = environment()
    bridge, session = memory_session(call, host)
    session.record['state'], session.turn_state = 'ready', 'completed'
    generation = session.generation
    assert bridge._stop(session)['process_group_released']
    bridge._exited(session, generation)
    assert session.record['state'] == 'closed'
    assert not session.events and session.transport.stops == 1


def test_old_exit_callback_cannot_publish_into_restarted_generation(environment):
    call, host, _ = environment()
    bridge, session = memory_session(call, host)
    old_generation = session.generation
    fresh = MemoryTransport()
    session.transport, session.generation = fresh, 'new-generation'
    session.record['state'], session.turn_state = 'ready', 'completed'
    bridge._exited(session, old_generation)
    assert session.record['state'] == 'ready'
    assert not session.events and fresh.stops == 0


def test_turn_timeout_without_exit_callback_does_not_invent_process_exit(environment, monkeypatch):
    from agent_py_agent.agent.tooling.mcp_protocol import MCPError

    call, host, _ = environment()
    bridge, session = memory_session(call, host)
    monkeypatch.setattr(bridge, '_save', lambda item, **kwargs: None)

    def request(*args, **kwargs):
        raise MCPError('本次请求超时', code='MCP_TIMEOUT')

    session.transport.request = request
    bridge._turn(session, [])
    assert [event['kind'] for event in session.events] == ['turn_failed']
    assert session.record['state'] == session.turn_state == 'failed'


def test_failed_turn_restart_then_explicit_close_does_not_republish_old_failure(environment, monkeypatch):
    from agent_py_agent.agent.tooling.mcp_protocol import MCPError

    call, host, _ = environment()
    call('catalog')
    bridge, launches = host._gap158_acp_bridge, []

    def launch(*args, **callbacks):
        transport = LoopbackTransport(callbacks)
        original_terminate = transport.terminate

        def terminate(*, grace_seconds):
            receipt = original_terminate(grace_seconds=grace_seconds)
            callbacks['on_exit']()
            return receipt

        transport.terminate = terminate
        launches.append(transport)
        return transport

    monkeypatch.setattr(bridge, '_launch', launch)
    sid = create(call)
    session = bridge.sessions[sid]
    old_generation = session.generation
    original_request = launches[0].request

    def request(method, params, *, timeout, authority_check=None):
        if method == 'session/prompt':
            raise MCPError('原连接已关闭', code='MCP_CONNECTION_CLOSED')
        return original_request(method, params, timeout=timeout, authority_check=authority_check)

    launches[0].request = request
    turn = prompt(call, sid, 'fail-current-turn')
    assert session.stopped.wait(2)
    session.worker.join(2)
    assert not session.worker.is_alive()
    wait_event(call, sid, 'turn_failed', turn_id=turn['turn_id'])
    assert [event['kind'] for event in session.events] == ['turn_failed', 'process_exited']

    # 走真实控制入口换代，故意不重写旧回合状态；新代主动关闭不能继承旧失败的补报资格。
    assert call('restart', {'session_id': sid})['session']['state'] == 'ready'
    assert session.generation != old_generation and session.turn_state == 'failed'
    assert call('close', {'session_id': sid})['closed']
    assert session.record['state'] == 'closed'
    assert [event['kind'] for event in session.events] == ['turn_failed', 'process_exited']
