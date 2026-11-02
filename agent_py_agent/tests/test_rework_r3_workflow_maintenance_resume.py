"""宿主节拍恢复原清理事务；故障只注入写叶，不修删状态或放宽默认策略。"""
from __future__ import annotations

import errno
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due
from agent_py_agent.agent.user_space.owner_resolver import ensure_owner_home, home_paths_with_owner
from agent_py_agent.agent.workflows import orphan_maintenance as maintenance
from agent_py_agent.agent.workflows.business_scope import WorkflowScopeService, workflow_state

DAY = 86_400


def _counts(**changes):
    return {'recycled': 0, 'skipped': 0, 'failed': 0, 'unknown': 0, **changes}


@pytest.fixture
def owner(tmp_path, monkeypatch):
    # pytest 必须消费本工作树，而非解释器 editable 安装指向的另一份 main。
    assert Path(maintenance.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])
    base = ensure_my_agent_home(tmp_path / 'home')
    home = home_paths_with_owner(base, ensure_owner_home(base.root))
    clock = [1_700_000_000.0]
    access = {'denied': set()}
    references = {}
    api = WorkflowScopeService(home.owner_home_dir, owner_id=home.owner_id,
        authorize=lambda actor, action, ref: actor.owner_id == home.owner_id
            and (action, ref) not in access['denied'],
        reference_reader=lambda ref: references.get(ref, {'coverage': 'complete', 'blockers': []}),
        clock=lambda: clock[0])
    actor = SimpleNamespace(owner_id=home.owner_id)
    writes = []
    save = api.store._save

    def observe_recycle(definition):
        saved = save(definition)
        if workflow_state(saved)['lifecycle'] == 'recycled':
            writes.append(saved.descriptor.id)
        return saved

    monkeypatch.setattr(api.store, '_save', observe_recycle)
    return SimpleNamespace(home=home, api=api, actor=actor, clock=clock, access=access,
        references=references, writes=writes)


def _create(owner, ref='original'):
    return owner.api.execute('workflows.create', {'definition': {
        'descriptor': {'id': ref, 'kind': 'workflow', 'name': ref, 'access_targets': [],
            'data_sources': [], 'credential_refs': [], 'io_contract': {}}, 'steps': []},
        'scope': {'scope_mode': 'unassigned', 'space_ids': []}},
        actor=owner.actor, idempotency_key='create-' + ref)


def _record(owner):
    return maintenance._record(owner.api.store)


def _tick(owner):
    result = run_owner_retention_if_due(owner.home, now=owner.clock[0], workflow_service=owner.api)
    assert result.ran and result.apply_outcome == 'applied'
    return json.loads((owner.home.owner_data_dir / 'maintenance.json').read_text())


def _fail_once(monkeypatch, *, phase='final'):
    save = maintenance._save
    failures = []

    def fail(store, record):
        pending = record['inflight']
        final = pending is None and any(
            item['result'].get('state') == 'completed' for item in record['operations'].values())
        progress = pending is not None and pending['position'] > 0
        if not failures and (final if phase == 'final' else progress):
            failures.append(maintenance._record(store))
            raise OSError(errno.EIO, 'one simulated workflow maintenance write failure')
        return save(store, record)

    monkeypatch.setattr(maintenance, '_save', fail)
    return failures


def test_outer_default_tick_resumes_original_final_write_then_fresh_and_manual(owner, monkeypatch):
    _create(owner)
    retention = owner.home.owner_retention_json.read_bytes()
    quota = owner.home.owner_quota_json.read_bytes()
    assert _record(owner)['policy']['orphan_after_days'] == 30
    owner.clock[0] += 31 * DAY
    failures = _fail_once(monkeypatch)
    attempts = []
    maintain = maintenance.maintain_owner_workflows

    def observe_key(*args, **kwargs):
        attempts.append(kwargs['idempotency_key'])
        return maintain(*args, **kwargs)

    monkeypatch.setattr(maintenance, 'maintain_owner_workflows', observe_key)
    failed = _tick(owner)
    pending = _record(owner)['inflight']
    identity = pending['identity']
    assert len(failures) == 1 and pending['position'] == 1
    assert pending['counts'] == _counts(recycled=1)
    assert failed['workflow_maintenance']['reason'] == 'workflow_maintenance_unavailable'
    assert failed['last_attempt_at'] == owner.clock[0]
    assert owner.writes == ['original']

    owner.clock[0] += DAY
    recovered = _tick(owner)
    assert attempts[0] != attempts[1]
    assert recovered['workflow_maintenance']['executed'] is True, recovered['workflow_maintenance']
    assert recovered['workflow_maintenance'] == {'executed': True,
        **_record(owner)['operations'][identity]['result']}
    assert recovered['workflow_maintenance']['operation_id'] == identity
    assert recovered['workflow_maintenance']['counts'] == _counts(recycled=1)
    assert _record(owner)['inflight'] is None and owner.writes == ['original']
    assert failures[0]['inflight']['identity'] == identity

    _create(owner, 'later')
    owner.clock[0] += 31 * DAY
    fresh = _tick(owner)
    assert fresh['workflow_maintenance']['operation_id'] != identity
    assert fresh['workflow_maintenance']['counts'] == _counts(recycled=1)
    _create(owner, 'manual')
    owner.clock[0] += 31 * DAY
    manual = owner.api.execute('workflows.cleanup.run', {}, actor=owner.actor,
        idempotency_key='manual-after-recovery')
    assert manual['counts'] == _counts(recycled=1)
    assert owner.api.execute('workflows.cleanup.run', {}, actor=owner.actor,
        idempotency_key='manual-after-recovery') == manual
    assert owner.writes == ['original', 'later', 'manual']
    assert owner.home.owner_retention_json.read_bytes() == retention
    assert owner.home.owner_quota_json.read_bytes() == quota
    print(json.dumps({'original_operation_id': identity,
        'recovered_operation_id': recovered['workflow_maintenance']['operation_id'],
        'outer_last_attempt_at': [failed['last_attempt_at'], recovered['last_attempt_at'], fresh['last_attempt_at']],
        'recycle_writes': owner.writes, 'original_recycle_count': owner.writes.count('original')}, sort_keys=True))


def test_public_cleanup_cannot_adopt_inflight_or_inject_private_flag(owner, monkeypatch):
    _create(owner)
    owner.clock[0] += 31 * DAY
    _fail_once(monkeypatch)
    with pytest.raises(OSError, match='simulated'):
        owner.api.execute('workflows.cleanup.run', {}, actor=owner.actor, idempotency_key='manual-original')
    before = _record(owner)
    with pytest.raises(BlockingIOError, match='WORKFLOW_MAINTENANCE_PENDING'):
        owner.api.execute('workflows.cleanup.run', {}, actor=owner.actor, idempotency_key='different-manual')
    with pytest.raises(ValueError, match='INVALID_WORKFLOW_PARAMETERS'):
        owner.api.execute('workflows.cleanup.run', {'resume_inflight': True}, actor=owner.actor,
            idempotency_key='injected-private-option')
    assert _record(owner) == before
    result = owner.api.execute('workflows.cleanup.run', {}, actor=owner.actor, idempotency_key='manual-original')
    assert result['operation_id'] == before['inflight']['identity']
    assert result['counts'] == _counts(recycled=1) and owner.writes == ['original']


def test_host_pending_identity_precedes_cached_fresh_key(owner, monkeypatch):
    _create(owner)
    owner.clock[0] += 31 * DAY
    saved = maintenance.maintain_owner_workflows(owner.api, actor=owner.actor, idempotency_key='cached-key')
    _create(owner, 'second')
    owner.clock[0] += 31 * DAY
    _fail_once(monkeypatch)
    with pytest.raises(OSError, match='simulated'):
        maintenance.maintain_owner_workflows(owner.api, actor=owner.actor, idempotency_key='original-inflight')
    pending = _record(owner)['inflight']
    result = maintenance.maintain_owner_workflows(owner.api, actor=owner.actor,
        idempotency_key='cached-key', resume_inflight=True)
    assert result['operation_id'] == pending['identity'] != saved['operation_id']
    assert _record(owner)['inflight'] is None and owner.writes == ['original', 'second']
    assert maintenance.maintain_owner_workflows(owner.api, actor=owner.actor, idempotency_key='cached-key') == saved


def test_stale_resumer_reloads_progress_instead_of_rewinding_or_double_counting(owner, monkeypatch):
    for ref in ('a-first', 'b-second', 'c-third'):
        _create(owner, ref)
    owner.clock[0] += 31 * DAY
    paused, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    execute, save = owner.api.execute, maintenance._save
    positions = []
    interrupted = []

    def pause_after_first_effect(action, request, **kwargs):
        result = execute(action, request, **kwargs)
        if (threading.get_ident() != main_thread and action == 'workflows.recycle'
                and request['workflow_ref'] == 'a-first' and not paused.is_set()):
            paused.set()
            assert release.wait(10), 'resumer not released'
        return result

    def observe_progress(store, record):
        result = save(store, record)
        pending = record['inflight']
        if pending is not None:
            positions.append((pending['position'], dict(pending['counts'])))
            if threading.get_ident() == main_thread and pending['position'] == 2 and not interrupted:
                interrupted.append(True)
                raise OSError(errno.EIO, 'competitor stops after durable position two')
        return result

    monkeypatch.setattr(owner.api, 'execute', pause_after_first_effect)
    monkeypatch.setattr(maintenance, '_save', observe_progress)
    with ThreadPoolExecutor(max_workers=1) as executor:
        stale = executor.submit(maintenance.maintain_owner_workflows, owner.api,
            actor=owner.actor, idempotency_key='shared-operation')
        try:
            assert paused.wait(10), 'first resumer never reached effect'
            with pytest.raises(OSError, match='durable position two'):
                maintenance.maintain_owner_workflows(owner.api, actor=owner.actor,
                    idempotency_key='shared-operation')
            assert _record(owner)['inflight']['position'] == 2
        finally:
            release.set()
        result = stale.result(timeout=10)
    assert [position for position, _ in positions] == sorted(position for position, _ in positions)
    assert all(counts == _counts(recycled=position) for position, counts in positions)
    assert result['counts'] == _counts(recycled=3)
    assert owner.writes == ['a-first', 'b-second', 'c-third']
    assert _record(owner)['inflight'] is None


@pytest.mark.parametrize('pause_point', ['after_effect', 'before_final'])
@pytest.mark.parametrize('another_pending', [False, True])
def test_stale_resumer_returns_completed_receipt_without_overwriting_operation(
        owner, monkeypatch, pause_point, another_pending):
    _create(owner)
    owner.clock[0] += 31 * DAY
    paused, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    execute, save, locked = owner.api.execute, maintenance._save, owner.api.store._locked
    armed = []
    resurrected = []

    def pause():
        paused.set()
        assert release.wait(10), 'stale resumer not released'

    def observe_effect(action, request, **kwargs):
        result = execute(action, request, **kwargs)
        if (threading.get_ident() != main_thread and action == 'workflows.recycle'
                and pause_point == 'after_effect' and not paused.is_set()):
            pause()
        return result

    def observe_save(store, record):
        pending = record['inflight']
        if pending is not None and pending['identity'] in record['operations']:
            resurrected.append(pending['identity'])
        result = save(store, record)
        if (threading.get_ident() != main_thread and pause_point == 'before_final'
                and pending is not None and pending['position'] == len(pending['rows'])):
            armed.append(True)
        return result

    @contextmanager
    def pause_final_lock():
        if threading.get_ident() != main_thread and armed and not paused.is_set():
            pause()
        with locked():
            yield

    monkeypatch.setattr(owner.api, 'execute', observe_effect)
    monkeypatch.setattr(maintenance, '_save', observe_save)
    monkeypatch.setattr(owner.api.store, '_locked', pause_final_lock)
    with ThreadPoolExecutor(max_workers=1) as executor:
        stale = executor.submit(maintenance.maintain_owner_workflows, owner.api,
            actor=owner.actor, idempotency_key='shared-operation')
        try:
            assert paused.wait(10), 'stale resumer never reached requested window'
            completed = maintenance.maintain_owner_workflows(owner.api, actor=owner.actor,
                idempotency_key='shared-operation')
            if another_pending:
                with monkeypatch.context() as patch:
                    _fail_once(patch)
                    with pytest.raises(OSError, match='simulated'):
                        maintenance.maintain_owner_workflows(owner.api, actor=owner.actor,
                            idempotency_key='other-operation')
            before = _record(owner)
        finally:
            release.set()
        assert stale.result(timeout=10) == completed
    assert not resurrected
    assert _record(owner) == before
    assert owner.writes == ['original']


@pytest.mark.parametrize('guard', ['scope_cas', 'definition_cas', 'unknown', 'active_reference',
    'protected', 'recycle_permission', 'content_edit'])
def test_resume_keeps_original_row_authorization_cas_reference_and_age_guards(owner, monkeypatch, guard):
    _create(owner, 'a-first')
    _create(owner, 'b-guarded')
    owner.clock[0] += 31 * DAY
    _fail_once(monkeypatch, phase='progress')
    failed = _tick(owner)
    pending = _record(owner)['inflight']
    identity = pending['identity']
    assert failed['workflow_maintenance']['executed'] is False and pending['position'] == 0
    assert owner.writes == ['a-first']
    ref = {'workflow_ref': 'b-guarded'}
    if guard == 'scope_cas':
        owner.api.execute('workflows.scope.set', {**ref, 'scope': {'scope_mode': 'spaces', 'space_ids': ['space']}},
            actor=owner.actor, expected_revision=1, idempotency_key='reassociate')
    elif guard == 'definition_cas':
        owner.api.execute('workflows.disable', ref, actor=owner.actor,
            expected_revision=1, idempotency_key='new-definition-revision')
    elif guard == 'unknown':
        owner.references['b-guarded'] = {'coverage': 'unknown', 'blockers': []}
    elif guard == 'active_reference':
        owner.references['b-guarded'] = {'coverage': 'complete', 'blockers': [
            {'ref': 'run-still-active', 'kind': 'active_run', 'reason_code': 'IN_USE'}]}
    elif guard == 'protected':
        owner.api.execute('workflows.protection.set', {**ref, 'protected': True}, actor=owner.actor,
            expected_revision=1, idempotency_key='protect')
    elif guard == 'recycle_permission':
        owner.access['denied'].add(('workflows.recycle', 'b-guarded'))
    else:
        definition = owner.api._get('b-guarded').to_dict()
        owner.api.store.save_workflow({**definition, 'body_markdown': '真正更新内容，重新起算孤立期限'})
    owner.clock[0] += DAY
    recovered = _tick(owner)['workflow_maintenance']
    assert recovered['executed'] is True and recovered['operation_id'] == identity
    assert recovered['counts'] == (_counts(recycled=1, unknown=1) if guard == 'unknown'
        else _counts(recycled=1, skipped=1))
    assert workflow_state(owner.api._get('b-guarded'))['lifecycle'] == 'active'
    assert owner.writes == ['a-first'] and _record(owner)['inflight'] is None


def test_wrong_owner_and_revoked_row_permission_cannot_consume_pending(owner, monkeypatch):
    _create(owner, 'a-first')
    _create(owner, 'b-guarded')
    owner.clock[0] += 31 * DAY
    _fail_once(monkeypatch, phase='progress')
    _tick(owner)
    pending = _record(owner)
    actual_owner = owner.api.owner_id
    owner.api.owner_id = 'local/different-owner'
    owner.clock[0] += DAY
    assert _tick(owner)['workflow_maintenance']['reason'] == 'workflow_owner_mismatch'
    assert _record(owner) == pending
    owner.api.owner_id = actual_owner
    with pytest.raises(PermissionError, match='ACCESS_DENIED'):
        owner.api.execute('workflows.cleanup.run', {}, actor=SimpleNamespace(owner_id='local/other'),
            idempotency_key='foreign-public')
    assert _record(owner) == pending
    owner.access['denied'].add(('workflows.cleanup.run', 'b-guarded'))
    owner.clock[0] += DAY
    assert _tick(owner)['workflow_maintenance']['reason'] == 'workflow_maintenance_unavailable'
    assert _record(owner)['inflight']['position'] == 1
    assert _record(owner)['inflight']['identity'] == pending['inflight']['identity']
    owner.access['denied'].clear()
    owner.clock[0] += DAY
    assert _tick(owner)['workflow_maintenance']['counts'] == _counts(recycled=2)
    assert owner.writes == ['a-first', 'b-guarded']


def test_default_full_batch_recovers_original_more_and_cursor_before_next_tick(owner, monkeypatch):
    for index in range(130):
        _create(owner, f'w{index:03}')
    owner.clock[0] += 31 * DAY
    _fail_once(monkeypatch)
    failed = _tick(owner)
    pending = _record(owner)['inflight']
    assert failed['workflow_maintenance']['executed'] is False
    assert len(pending['rows']) == pending['position'] == 128
    assert pending['more'] is True and pending['counts'] == _counts(recycled=128)
    assert _record(owner)['cursor'] is None
    owner.clock[0] += DAY
    recovered = _tick(owner)['workflow_maintenance']
    assert recovered['operation_id'] == pending['identity']
    assert recovered['counts'] == _counts(recycled=128)
    cursor = [pending['rows'][-1]['orphan_since'], 'w127']
    assert json.loads(recovered['next_cursor']) == _record(owner)['cursor'] == cursor
    assert owner.writes == [f'w{index:03}' for index in range(128)]
    owner.clock[0] += DAY
    fresh = _tick(owner)['workflow_maintenance']
    assert fresh['operation_id'] != pending['identity']
    assert fresh['counts'] == _counts(recycled=2)
    assert fresh['next_cursor'] is None and _record(owner)['cursor'] is None
    assert owner.writes == [f'w{index:03}' for index in range(130)]


def test_public_native_schema_rejects_private_resume_flag_in_params_and_envelope():
    from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.plugin_host.schemas import validate_object

    schema = business_action_spec('workflows.cleanup.run').native_request_schema
    request = {'action': 'workflows.cleanup.run', 'request_id': 'public-cleanup-request',
        'idempotency_key': 'public-cleanup-key-0001', 'params': {}}
    assert validate_object(request, schema)['params'] == {}
    for injected in ({**request, 'resume_inflight': True},
            {**request, 'params': {'resume_inflight': True}}):
        with pytest.raises(HostAPIError, match='INVALID_SCHEMA'):
            validate_object(injected, schema)
