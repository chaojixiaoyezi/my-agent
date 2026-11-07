"""隔离的真实 Git/Store 主链路；fake pip、fake lanes 和 Event 控制交错，不执行宿主维护。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.business_spaces import gap158_w6_ops as O
from agent_py_agent.agent.business_spaces.v13_store import v13_store_for
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts import business_api_v13 as V
from agent_py_agent.agent.gateway_parts.business_api import bind_business_context
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import validate_object
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from agent_py_agent.cli import update as U


def _shape(properties, required=None):
    return {'type': 'object', 'properties': properties,
        'required': list(properties) if required is None else required, 'additionalProperties': False}


def _result_schema(action):
    identifier = {'type': 'string', 'minLength': 1, 'maxLength': 128,
        'pattern': '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'}
    integer = {'type': 'integer', 'minimum': 0}
    if action == 'ops.update.check':
        return _shape({'source': {'type': 'string', 'enum': ['git', 'pip', 'offline', 'unknown']},
            'current': {'type': 'string', 'minLength': 1, 'maxLength': 64},
            'latest': {'type': 'string', 'maxLength': 64}, 'update_available': {'type': 'boolean'},
            'dirty': {'type': 'boolean'}, 'notes': {'type': 'array', 'maxItems': 8,
                'items': {'type': 'string', 'maxLength': 256}}}, ['source', 'current', 'update_available', 'dirty'])
    if action in {'ops.update.apply', 'ops.update.rollback'}:
        props = {'job_id': identifier, 'state': {'type': 'string', 'enum': ['queued', 'running']}}
        if action.endswith('rollback'):
            props['pre_commit'] = {'type': 'string', 'minLength': 1, 'maxLength': 64}
        return _shape(props)
    if action == 'ops.update.stream':
        line = _shape({'seq': integer, 'at': {'type': 'number', 'minimum': 0},
            'stream': {'type': 'string', 'enum': ['stdout', 'stderr', 'status']},
            'text': {'type': 'string', 'maxLength': 2000}})
        return _shape({'job_id': identifier, 'state': {'type': 'string',
            'enum': ['queued', 'running', 'succeeded', 'failed', 'rolled_back']},
            'lines': {'type': 'array', 'items': line, 'maxItems': 100},
            'next_seq': integer, 'overflow': {'type': 'boolean'}})
    receipt = _shape({'lane': {'type': 'string', 'minLength': 1, 'maxLength': 64},
        'state': {'type': 'string', 'enum': ['done', 'partial', 'skipped', 'failed']},
        'scanned': integer, 'removed': integer, 'bytes_freed': integer,
        'error_code': {'type': 'string', 'maxLength': 64}}, ['lane', 'state', 'scanned', 'removed', 'bytes_freed'])
    return _shape({'receipt': {'type': 'array', 'maxItems': 16, 'items': receipt}})


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path / 'agent-home'
    paths = home_paths_with_owner(home_paths(root), resolve_owner_home(root))
    host = SimpleNamespace(home_paths=paths,
        config=SimpleNamespace(business_spaces_enabled=True, ops_remote_update_enabled=True,
            retention_scan_slice_entries=7, retention_scan_slice_bytes=2048, retention_scan_slice_ms=40),
        business_v13_gateway_paths=gateway_paths_from_root(root / 'gateway'))
    context = bind_business_context(host, authorize=lambda action, facts: True, subject_id='admin')
    services = SimpleNamespace(owner_id=paths.owner_id, host=host)
    # 单独跑本模块时 W6 slice 可能尚未合入，只补齐派工已指定的集合 scope。
    if 'ops_update_jobs' in V.COLLECTIONS:
        assert V.COLLECTIONS['ops_update_jobs'] == 'main'
    monkeypatch.setitem(V.COLLECTIONS, 'ops_update_jobs', 'main')
    threads = []
    start = O._start_worker

    def worker(target):
        thread = start(target)
        threads.append(thread)
        return thread

    monkeypatch.setattr(O, '_start_worker', worker)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    result = SimpleNamespace(host=host, context=context, services=services, threads=threads, tmp=tmp_path)
    yield result
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive(), '测试必须收回自己启动的工作线程'


def _call(case, action, params=None, *, key=None):
    return V.V13Call(action=action, params={} if params is None else params,
        request_id='req-' + uuid4().hex, owner_id=case.host.home_paths.owner_id,
        subject_id=case.context.subject_id, context=case.context, services=case.services,
        owner_home=case.host.home_paths.owner_home_dir,
        idempotency_key=key or uuid4().hex, clock=lambda: 1234.0)


def _invoke(case, action, params=None, *, key=None):
    result = O.handle_ops(_call(case, action, params, key=key))
    assert validate_object(result, _result_schema(action)) == result
    return result


def _finish(case, job_id):
    case.threads[-1].join(timeout=5)
    assert not case.threads[-1].is_alive()
    return _invoke(case, 'ops.update.stream', {'job_id': job_id, 'limit': 100})


def _git(repo, args):
    result = subprocess.run(['git', '-C', str(repo), *args], text=True, capture_output=True,
        check=True, timeout=10)
    return result.stdout.strip()


@pytest.fixture
def checkout(case, monkeypatch):
    upstream, repo = case.tmp / 'upstream', case.tmp / 'checkout'
    upstream.mkdir()
    _git(upstream, ['init', '-b', 'main'])
    _git(upstream, ['config', 'user.email', 'ops-test@example.invalid'])
    _git(upstream, ['config', 'user.name', '运维测试'])
    (upstream / 'code.txt').write_text('before\n', encoding='utf-8')
    _git(upstream, ['add', 'code.txt'])
    _git(upstream, ['commit', '-m', 'before'])
    before = _git(upstream, ['rev-parse', 'HEAD'])
    _git(case.tmp, ['clone', str(upstream), str(repo)])
    (upstream / 'code.txt').write_text('after\n', encoding='utf-8')
    _git(upstream, ['commit', '-am', 'after'])
    after = _git(upstream, ['rev-parse', 'HEAD'])
    monkeypatch.setattr(U, 'detect_install_source', lambda: U.InstallSource('git', repo))
    installs = []

    def pip(selected):
        assert selected == repo
        installs.append(_git(selected, ['rev-parse', 'HEAD']))
        return 0, 'fake editable reinstall'

    monkeypatch.setattr(U, '_pip_install_editable', pip)
    return SimpleNamespace(repo=repo, before=before, after=after, installs=installs)


def test_real_git_check_apply_rollback_persisted_stream(case, checkout, monkeypatch):
    checked = _invoke(case, 'ops.update.check')
    assert checked == {'source': 'git', 'current': checkout.before[:7], 'latest': checkout.after[:7],
        'update_available': True, 'dirty': False, 'notes': []}
    commands = []
    git = U._git

    def record(repo, args):
        commands.append(list(args))
        return git(repo, args)

    monkeypatch.setattr(U, '_git', record)
    accepted = _invoke(case, 'ops.update.apply')
    assert accepted['state'] == 'queued'
    streamed = _finish(case, accepted['job_id'])
    assert streamed['state'] == 'succeeded' and streamed['lines']
    assert _git(checkout.repo, ['rev-parse', 'HEAD']) == checkout.after
    assert checkout.installs == [checkout.after]
    store = v13_store_for(_call(case, 'ops.update.stream'), 'main')
    persisted = store.get('ops_update_jobs', accepted['job_id'])
    assert persisted['pre_commit'] == checkout.before
    assert persisted['lines'] == streamed['lines']
    assert store.owner_id == 'local/main'
    rolled = _invoke(case, 'ops.update.rollback', {'job_id': accepted['job_id']})
    assert rolled['pre_commit'] == checkout.before
    assert _finish(case, rolled['job_id'])['state'] == 'rolled_back'
    assert ['reset', '--keep', checkout.before] in commands
    assert not any('--hard' in command for command in commands)
    assert _git(checkout.repo, ['rev-parse', 'HEAD']) == checkout.before
    assert checkout.installs == [checkout.after, checkout.before]
    assert _invoke(case, 'ops.update.stream', {'job_id': accepted['job_id']})['state'] == 'rolled_back'


@pytest.mark.parametrize('action', ['ops.update.apply', 'ops.update.rollback'])
def test_dirty_refusal_preserves_checkout_and_does_not_start(case, checkout, action):
    params = {}
    if action.endswith('rollback'):
        accepted = _invoke(case, 'ops.update.apply')
        assert _finish(case, accepted['job_id'])['state'] == 'succeeded'
        params['job_id'] = accepted['job_id']
    (checkout.repo / 'untracked.txt').write_text('must be preserved', encoding='utf-8')
    current, installs, workers = _git(checkout.repo, ['rev-parse', 'HEAD']), list(checkout.installs), len(case.threads)
    with pytest.raises(BusinessError) as error:
        _invoke(case, action, params)
    assert error.value.code == 'STATE_CONFLICT'
    assert error.value.details['reason'] == 'worktree_dirty'
    assert _git(checkout.repo, ['rev-parse', 'HEAD']) == current
    assert checkout.installs == installs and len(case.threads) == workers
    assert (checkout.repo / 'untracked.txt').read_text(encoding='utf-8') == 'must be preserved'


def test_event_contention_live_stream_and_idempotent_acceptance(case, checkout, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def pip(repo):
        entered.set()
        assert release.wait(timeout=5)
        return 0, 'fake pip done'

    monkeypatch.setattr(U, '_pip_install_editable', pip)
    key = uuid4().hex
    accepted = _invoke(case, 'ops.update.apply', key=key)
    try:
        assert entered.wait(timeout=5)
        assert _invoke(case, 'ops.update.stream', {'job_id': accepted['job_id']})['state'] == 'running'
        assert _invoke(case, 'ops.update.apply', key=key) == accepted
        assert len(case.threads) == 1
        for action, params in [('ops.update.apply', {}), ('ops.update.rollback', {'job_id': accepted['job_id']}),
                               ('ops.gc.run', {'lanes': ['local_events']})]:
            with pytest.raises(BusinessError) as error:
                _invoke(case, action, params)
            assert error.value.code == 'MAINTENANCE_BUSY'
    finally:
        release.set()
    assert _finish(case, accepted['job_id'])['state'] == 'succeeded'
    assert _invoke(case, 'ops.update.apply', key=key) == accepted


def test_ring_overflow_sequence_paging_and_store_reload(case, checkout, monkeypatch):
    monkeypatch.setattr(O, '_ring_limit', lambda: 3)

    def apply(repo, *, on_step, refuse_dirty, pre_commit):
        assert refuse_dirty and pre_commit == checkout.before
        for value in range(9):
            on_step({'stream': 'stdout', 'text': f'line {value}'})
        on_step({'stream': 'stderr', 'text': 'x' * 6001})
        return {'returncode': 0}

    monkeypatch.setattr(U, 'apply_update_steps', apply)
    accepted = _invoke(case, 'ops.update.apply')
    page = _finish(case, accepted['job_id'])
    assert page['overflow'] is True
    assert [line['seq'] for line in page['lines']] == [11, 12, 13]
    assert [len(line['text']) for line in page['lines']] == [2000, 2000, 1]
    first = _invoke(case, 'ops.update.stream', {'job_id': accepted['job_id'], 'limit': 1})
    assert first['next_seq'] == 11 and first['overflow'] is True
    rest = _invoke(case, 'ops.update.stream', {'job_id': accepted['job_id'], 'after_seq': 11})
    assert [line['seq'] for line in rest['lines']] == [12, 13] and not rest['overflow']
    assert _invoke(case, 'ops.update.stream', {'job_id': accepted['job_id'], 'after_seq': 13})['lines'] == []


def test_admin_and_actual_switch_are_required_without_git(case, monkeypatch):
    monkeypatch.setattr(U, 'detect_install_source', lambda: pytest.fail('拒绝应发生在安装探测前'))
    case.host.business_native_admin = lambda: False
    for action in ['ops.update.check', 'ops.update.apply', 'ops.update.rollback', 'ops.update.stream', 'ops.gc.run']:
        with pytest.raises(HostAPIError) as error:
            O.handle_ops(_call(case, action))
        assert error.value.code == 'BUSINESS_ACCESS_DENIED'
    case.host.business_native_admin = lambda: True
    case.host.config.ops_remote_update_enabled = False
    for action, params in [('ops.update.apply', {}), ('ops.update.rollback', {'job_id': 'missing'})]:
        with pytest.raises(HostAPIError) as error:
            _invoke(case, action, params)
        assert error.value.code == 'HOST_NOT_READY'


@pytest.mark.parametrize('kind', ['pip', 'offline', 'unknown'])
def test_non_git_status_and_remote_refusal(case, monkeypatch, kind):
    monkeypatch.setattr(U, 'detect_install_source', lambda: U.InstallSource(kind, None))
    result = _invoke(case, 'ops.update.check')
    assert result['source'] == kind and result['current'] == 'unknown' and not result['update_available']
    with pytest.raises(BusinessError) as error:
        _invoke(case, 'ops.update.apply')
    assert error.value.code == 'STATE_CONFLICT' and not case.threads


def test_failure_releases_run_lock_and_allows_safe_rollback(case, checkout, monkeypatch):
    monkeypatch.setattr(U, '_pip_install_editable', lambda repo: (1, 'fake pip failure'))
    first = _invoke(case, 'ops.update.apply')
    assert _finish(case, first['job_id'])['state'] == 'failed'
    assert _git(checkout.repo, ['rev-parse', 'HEAD']) == checkout.after
    monkeypatch.setattr(U, '_pip_install_editable', lambda repo: (0, 'fake pip recovery'))
    rollback = _invoke(case, 'ops.update.rollback', {'job_id': first['job_id']})
    assert _finish(case, rollback['job_id'])['state'] == 'rolled_back'
    assert _git(checkout.repo, ['rev-parse', 'HEAD']) == checkout.before


def test_restart_uses_existing_service_only_after_success(case, checkout, monkeypatch):
    observed = []

    def restart(paths, *, target_pid, requester, reason, cooldown_seconds, now):
        assert _git(checkout.repo, ['rev-parse', 'HEAD']) == checkout.after
        assert checkout.installs == [checkout.after]
        observed.append((paths, target_pid, requester, cooldown_seconds, now))
        return {'status': 'scheduled'}

    monkeypatch.setattr(O, 'submit_restart_request', restart)
    monkeypatch.setattr(O, 'hosting_gateway_pid', lambda: 2468)
    accepted = _invoke(case, 'ops.update.apply', {'restart': True})
    result = _finish(case, accepted['job_id'])
    assert result['state'] == 'succeeded'
    assert result['lines'][-1]['text'] == 'scheduled'
    assert len(observed) == 1
    paths, pid, requester, cooldown, now = observed[0]
    assert paths is case.host.business_v13_gateway_paths and pid == 2468
    assert requester['owner_id'] == case.context.owner_id and requester['subject_id'] == 'admin'
    assert cooldown == 30 and now == 1234.0


def test_missing_and_foreign_job_ids_are_indistinguishable(case, checkout):
    accepted = _invoke(case, 'ops.update.apply')
    assert _finish(case, accepted['job_id'])['state'] == 'succeeded'
    call = _call(case, 'ops.update.stream', {'job_id': accepted['job_id']})
    store = v13_store_for(call, 'main')

    def foreign(rows):
        rows[accepted['job_id']]['owner_id'] = 'providers/test/users/foreign'
        return {}

    store.transaction('ops_update_jobs', foreign, idempotency_key=uuid4().hex, params_digest='foreign-test')
    for job in [accepted['job_id'], 'missing']:
        for action in ['ops.update.stream', 'ops.update.rollback']:
            with pytest.raises(HostAPIError) as error:
                _invoke(case, action, {'job_id': job})
            assert error.value.code == 'RESOURCE_NOT_FOUND'


def test_gc_selected_real_binding_names_with_budgeted_fake_lanes_and_replay(case, monkeypatch):
    seen, writes = [], []
    selected = ['local_events', 'tool_outputs', 'asset_auxiliary']

    def lane(request):
        seen.append(request)
        assert request.kind in O.LANE_BINDINGS
        assert request.home is case.host.home_paths
        assert request.budget.max_items == 7 and request.budget.max_bytes == 2048
        assert 0 < request.budget.remaining_ms() <= 40
        assert not request.legal_hold and request.coordinator
        request.run_write(lambda: writes.append(request.kind))
        return {'cursor': {'after': 'one-slice'}, 'complete': request.kind != 'tool_outputs',
            'items': 3, 'bytes': 16, 'deleted_items': 2, 'freed_bytes': 8}

    monkeypatch.setattr(O, 'resolve_storage_lane', lambda kind: lane)
    key = uuid4().hex
    params = {'lanes': selected}
    result = _invoke(case, 'ops.gc.run', params, key=key)
    assert result == {'receipt': [{'lane': kind, 'state': 'partial' if kind == 'tool_outputs' else 'done',
        'scanned': 3, 'removed': 2, 'bytes_freed': 8} for kind in selected]}
    assert [request.kind for request in seen] == selected and writes == selected
    assert _invoke(case, 'ops.gc.run', params, key=key) == result
    assert len(seen) == 3
    with pytest.raises(BusinessError) as error:
        _invoke(case, 'ops.gc.run', {'lanes': ['local_events'], 'dry_run': True}, key=key)
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'


@pytest.mark.parametrize('hold', [False, True])
def test_gc_dry_run_and_actual_legal_hold_never_execute_write(case, monkeypatch, hold):
    paths = case.host.home_paths
    if hold:
        paths.owner_retention_json.parent.mkdir(parents=True, exist_ok=True)
        paths.owner_retention_json.write_text(json.dumps({'legal_hold': True}), encoding='utf-8')
    seen, writes = [], []

    def lane(request):
        seen.append(request)
        assert request.legal_hold is True
        request.run_write(lambda: writes.append('unexpected side effect'))
        pytest.fail('预演写门不能返回一个伪造成功值')

    monkeypatch.setattr(O, 'resolve_storage_lane', lambda kind: lane)
    result = _invoke(case, 'ops.gc.run', {'lanes': ['local_events'], 'dry_run': not hold})
    assert result == {'receipt': [{'lane': 'local_events', 'state': 'skipped',
        'scanned': 0, 'removed': 0, 'bytes_freed': 0}]}
    assert len(seen) == 1 and not writes


@pytest.mark.parametrize('params', [
    {'lanes': ['not_registered']}, {'lanes': ['local_events', 'local_events']},
    {'lanes': ['../home']}, {'lanes': 'local_events'}, {'lanes': [1]}, {'dry_run': 1},
    {'lanes': ['local_events'], 'unknown': True},
])
def test_gc_invalid_lanes_or_params_are_rejected_before_resolution(case, monkeypatch, params):
    monkeypatch.setattr(O, 'resolve_storage_lane', lambda kind: pytest.fail('非法请求不能执行通道'))
    with pytest.raises(BusinessError) as error:
        _invoke(case, 'ops.gc.run', params)
    assert error.value.code == 'INVALID_SCHEMA'


def test_gc_failures_are_per_lane_and_do_not_release_measured_bytes(case, monkeypatch):
    def resolve(kind):
        if kind == 'local_events':
            return None

        def lane(request):
            if kind == 'tool_outputs':
                raise OSError('synthetic failure')
            return {'cursor': {}, 'complete': True, 'items': 1, 'bytes': 10,
                'deleted_items': 0, 'freed_bytes': 0, 'deferred_reason': 'protected'}
        return lane

    monkeypatch.setattr(O, 'resolve_storage_lane', resolve)
    result = _invoke(case, 'ops.gc.run', {'lanes': ['local_events', 'tool_outputs', 'asset_auxiliary']})
    assert [receipt['state'] for receipt in result['receipt']] == ['skipped', 'failed', 'skipped']
    assert result['receipt'][1]['error_code'] == 'HOST_NOT_READY'
    assert all(receipt['removed'] == 0 and receipt['bytes_freed'] == 0 for receipt in result['receipt'])


def test_failed_worker_start_releases_lease_and_persists_failed_job(case, checkout, monkeypatch):
    def fail(target):
        raise RuntimeError('synthetic start failure')

    monkeypatch.setattr(O, '_start_worker', fail)
    with pytest.raises(HostAPIError) as error:
        _invoke(case, 'ops.update.apply')
    assert error.value.code == 'HOST_NOT_READY'
    store = v13_store_for(_call(case, 'ops.update.stream'), 'main')
    rows = store.list('ops_update_jobs')
    assert len(rows) == 1 and rows[0]['state'] == 'failed'
    lease = O._acquire_run(store)
    lease.close()
    assert checkout.installs == []


def test_job_ring_limit_uses_live_w0_limit_when_available(monkeypatch):
    real_import = O.importlib.import_module

    def limits(name):
        if name == O.__package__ + '.gap158_limits':
            return SimpleNamespace(OPS_UPDATE_LOG_RING=5)
        return real_import(name)

    monkeypatch.setattr(O.importlib, 'import_module', limits)
    assert O._ring_limit() == 5


@pytest.mark.parametrize('params', [
    {'job_id': 'missing', 'after_seq': True}, {'job_id': 'missing', 'limit': 0},
    {'job_id': 'missing', 'limit': 101}, {'job_id': 'missing', 'owner_id': 'local/main'},
])
def test_stream_closed_params_and_numeric_types(case, params):
    with pytest.raises(BusinessError) as error:
        _invoke(case, 'ops.update.stream', params)
    assert error.value.code == 'INVALID_SCHEMA'


@pytest.mark.parametrize('change', ['dirty', 'clean_commit'])
def test_worker_rechecks_checkout_after_acceptance_before_side_effects(case, checkout, monkeypatch, change):
    pending = []
    monkeypatch.setattr(O, '_start_worker', pending.append)
    accepted = _invoke(case, 'ops.update.apply')
    if change == 'dirty':
        (checkout.repo / 'late-untracked.txt').write_text('preserve', encoding='utf-8')
    else:
        _git(checkout.repo, ['fetch', '--quiet'])
        _git(checkout.repo, ['reset', '--keep', checkout.after])
    assert len(pending) == 1
    pending[0]()
    assert _invoke(case, 'ops.update.stream', {'job_id': accepted['job_id']})['state'] == 'failed'
    assert not checkout.installs


def test_maintenance_lock_is_nonblocking_across_processes(case):
    store = v13_store_for(_call(case, 'ops.gc.run'), 'main')
    lease = O._acquire_run(store)
    script = '''
import sys
from pathlib import Path
from types import SimpleNamespace
from agent_py_agent.agent.business_spaces import gap158_w6_ops as ops
from agent_py_agent.agent.conversation.business_access import BusinessError
try:
    lease = ops._acquire_run(SimpleNamespace(owner_home=Path(sys.argv[1])))
except BusinessError as exc:
    assert exc.code == 'MAINTENANCE_BUSY'
    print(exc.code)
else:
    lease.close()
    raise AssertionError('OS maintenance lock was not held')
'''
    try:
        child = subprocess.run([sys.executable, '-c', script, str(store.owner_home)],
            text=True, capture_output=True, check=True, timeout=10)
        assert child.stdout.strip() == 'MAINTENANCE_BUSY'
    finally:
        lease.close()
