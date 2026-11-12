"""完成帧生命周期、父主回执及只补计时恢复的真实反例。"""
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from test_acceptance_workflow_completion_clock import definition, mutate, service

from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.workflows.business_scope import workflow_state
from agent_py_agent.agent.workflows.engine import WorkflowEngine


def parent_id(actor, key='parent-run'):
    return hashlib.sha256((actor.owner_id + '\0workflows.run\0' + key).encode()).hexdigest()


def business_tree(home, children, analyze):
    api, actor = service(home, [time.time() - 60], analyze)
    steps = []
    for index in range(children):
        ref = f'child-clock-{index}'
        api.execute('workflows.create', {'definition': definition(ref, [
            {'step_id': f'leaf-{index}', 'name': ref, 'mode': 'llm_skill_analysis'}])},
            actor=actor, idempotency_key=f'create-{index}')
        steps.append({'step_id': f'child-{index}', 'name': ref, 'mode': 'subworkflow',
                      'subworkflow_ref': f'workflow:{ref}'})
    mutate(api, actor, 'workflows.update', {'definition': definition(steps=steps)}, 'parent')
    tool = WorkflowsTool(home, business_service=api, business_actor_provider=lambda: actor)
    return api, actor, tool


@pytest.mark.parametrize('children', [1, 2])
@pytest.mark.parametrize('fail_parent', [False, True])
def test_child_clock_failure_does_not_impersonate_public_parent(tmp_path, monkeypatch, children, fail_parent):
    calls = []
    api, actor, tool = business_tree(tmp_path, children,
        lambda spec, context, skills: calls.append(spec['step_id']) or {'done': True})
    original = WorkflowStore._record_completed_run

    def unavailable(store, record, snapshot):
        if record.workflow_id.startswith('child-clock-') or fail_parent:
            raise OSError('clock unavailable: ' + record.workflow_id)
        return original(store, record, snapshot)

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', unavailable)
    outcome = tool.execute({'action': 'workflows.run', 'workflow_ref': 'completion-clock',
                            'idempotency_key': 'parent-run'})
    assert not outcome.ok and outcome.effect_outcome == 'unknown'
    saved = api._run_store().load_run(parent_id(actor))
    assert saved.status == 'succeeded' and len(calls) == children
    receipt = outcome.result_envelope
    assert receipt['workflow_run_id'] == saved.workflow_run_id
    assert receipt['status'] == saved.status and receipt['terminal_committed'] is True
    failures = receipt['completion_clock_failures']
    expected = {step.subworkflow_run_id for step in saved.steps}
    if fail_parent:
        expected.add(saved.workflow_run_id)
    assert {item['workflow_run_id'] for item in failures} == expected
    assert all(api._run_store().load_run(item['workflow_run_id']).status == 'succeeded'
               for item in failures)


@pytest.mark.parametrize('children', [1, 2])
def test_parent_same_key_repairs_children_without_reexecution(tmp_path, monkeypatch, children):
    calls = []
    api, actor, tool = business_tree(tmp_path, children,
        lambda spec, context, skills: calls.append(spec['step_id']) or {'done': True})
    original = WorkflowStore._record_completed_run

    def unavailable(store, record, snapshot):
        if record.workflow_id.startswith('child-clock-'):
            raise PermissionError('child clock unavailable')
        return original(store, record, snapshot)

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', unavailable)
    params = {'action': 'workflows.run', 'workflow_ref': 'completion-clock', 'idempotency_key': 'parent-run'}
    failed = tool.execute(params)
    assert not failed.ok
    saved = api._run_store().load_run(parent_id(actor))
    child_runs = [api._run_store().load_run(step.subworkflow_run_id) for step in saved.steps]
    assert all(workflow_state(api._get(child.workflow_id))['last_run_at'] is None for child in child_runs)
    monkeypatch.setattr(WorkflowStore, '_record_completed_run', original)
    repaired = tool.execute(params)
    assert repaired.ok and repaired.result_envelope['workflow_run_id'] == saved.workflow_run_id
    assert repaired.result_envelope['state'] == 'completed'
    assert calls == [f'leaf-{index}' for index in range(children)]
    for child in child_runs:
        assert workflow_state(api._get(child.workflow_id))['last_run_at'] == child.ended_at
        after = api._run_store().load_run(child.workflow_run_id)
        assert after.status == child.status and after.revision == child.revision


@pytest.mark.parametrize('error_type', [ToolCancelled, InterruptedError, RuntimeError])
def test_clock_failure_does_not_replace_primary_exit(tmp_path, monkeypatch, error_type):
    api, actor = service(tmp_path, [time.time() - 60])
    primary = error_type('original parent failure')

    def unavailable(store, record, snapshot):
        raise OSError('secondary child clock failure')

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', unavailable)
    with pytest.raises(error_type) as raised:
        with api._run_store().execution_lock('parent-exception'):
            result = api.execute('workflows.run', {'workflow_ref': 'completion-clock'},
                                 actor=actor, idempotency_key='completed-child')
            assert result['state'] == 'completed'
            raise primary
    assert raised.value is primary
    assert 'secondary child clock failure' in '\n'.join(getattr(primary, '__notes__', ()))


@pytest.mark.parametrize('finish_order', ['after_root', 'during_root'])
def test_native_queue_child_publishes_after_root_handoff_closes(tmp_path, monkeypatch, finish_order):
    from agent_py_agent.agent.workflows import concurrency_policy
    from agent_py_agent.agent.workflows import engine as engine_module

    holder_entered, release_holder = Event(), Event()
    queued, parent_continue, worker_finished = Event(), Event(), Event()
    calls = []
    api, actor = service(tmp_path, [time.time() - 60],
        lambda spec, context, skills: calls.append(spec['step_id']) or {'done': True})
    api.execute('workflows.create', {'definition': definition('late-clock', [
        {'step_id': 'late-leaf', 'name': 'late', 'mode': 'llm_skill_analysis'}])},
        actor=actor, idempotency_key='create-late')
    mutate(api, actor, 'workflows.update', {'definition': definition(steps=[
        {'step_id': 'queued-child', 'name': 'queued', 'mode': 'subworkflow',
         'subworkflow_ref': 'workflow:late-clock'}])}, 'parent')

    def factory(root, **options):
        value = WorkflowEngine(root, analysis_handler=lambda spec, context, skills:
            calls.append(spec['step_id']) or {'done': True}, **options)
        value.default_concurrency_policy = 'queue'
        return value

    api.engine_factory = factory
    native_dispatch, native_worker = concurrency_policy.dispatch, concurrency_policy._queue_worker

    def observed_dispatch(home, kind, asset_id, run_id, policy, callback, **options):
        result = native_dispatch(home, kind, asset_id, run_id, policy, callback, **options)
        if asset_id == 'late-clock' and isinstance(result, dict) and result.get('status') == 'queued':
            queued.set()
            assert parent_continue.wait(10), 'parent queue observation was not released'
        return result

    def observed_worker(*args):
        try:
            return native_worker(*args)
        finally:
            worker_finished.set()

    def holder():
        holder_entered.set()
        assert release_holder.wait(10), 'holder was not released'

    monkeypatch.setattr(engine_module, 'dispatch', observed_dispatch)
    monkeypatch.setattr(concurrency_policy, '_queue_worker', observed_worker)
    with ThreadPoolExecutor(max_workers=2) as pool:
        holding = pool.submit(native_dispatch, tmp_path, 'workflow', 'late-clock', 'holder', 'queue', holder)
        assert holder_entered.wait(5)
        parent = pool.submit(api.execute, 'workflows.run', {'workflow_ref': 'completion-clock'},
                             actor=actor, idempotency_key='parent-run')
        try:
            assert queued.wait(5), 'actual child was not accepted by native queue'
            if finish_order == 'after_root':
                parent_continue.set()
                parent.result(timeout=5)
                release_holder.set()
                assert worker_finished.wait(5), 'native queued worker did not exit'
            else:
                release_holder.set()
                assert worker_finished.wait(5), 'native queued worker did not exit'
                parent_continue.set()
                parent.result(timeout=5)
        finally:
            parent_continue.set()
            release_holder.set()
        holding.result(timeout=5)
    assert worker_finished.wait(5)
    saved = api._run_store().load_run(parent_id(actor))
    child = api._run_store().load_run(saved.steps[0].subworkflow_run_id)
    assert child.status == 'succeeded' and calls == ['late-leaf']
    assert workflow_state(api._get('late-clock'))['last_run_at'] == child.ended_at


@pytest.mark.parametrize('fault', ['owner', 'parent', 'identity', 'workflow', 'step_ref', 'receipt_ref', 'revoked',
                                  'snapshot_identity', 'snapshot_owner', 'snapshot_source'])
def test_same_key_recovery_validates_all_links_before_any_clock_write(tmp_path, monkeypatch, fault):
    import json

    calls = []
    api, actor, tool = business_tree(tmp_path, 2,
        lambda spec, context, skills: calls.append(spec['step_id']) or {'done': True})
    original = WorkflowStore._record_completed_run

    def unavailable(store, record, snapshot):
        if record.workflow_id.startswith('child-clock-'):
            raise OSError('clock temporarily unavailable')
        return original(store, record, snapshot)

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', unavailable)
    params = {'action': 'workflows.run', 'workflow_ref': 'completion-clock', 'idempotency_key': 'parent-run'}
    assert not tool.execute(params).ok
    store = api._run_store()
    parent = store.load_run(parent_id(actor))
    child = store.load_run(parent.steps[-1].subworkflow_run_id)
    if fault == 'revoked':
        api.authorize = lambda current, action, ref: current is actor and ref != child.workflow_id
    else:
        target = parent if fault in ('step_ref', 'receipt_ref') else child
        path = store.root / target.workflow_run_id / 'run.json'
        raw = json.loads(path.read_text())
        if fault == 'step_ref':
            raw['steps'][-1]['subworkflow_run_id'] = parent.steps[0].subworkflow_run_id
        elif fault == 'receipt_ref':
            raw['steps'][-1]['escalation_result']['subworkflow_receipt']['child_workflow_id'] = 'unrelated'
        elif fault.startswith('snapshot_'):
            field = {'snapshot_identity': 'id', 'snapshot_owner': 'owner_id',
                     'snapshot_source': 'source_scope'}[fault]
            raw['workflow_snapshot']['descriptor'][field] = 'unrelated'
        else:
            field = {'owner': 'owner_id', 'parent': 'parent_workflow_run_id',
                     'identity': 'workflow_run_id', 'workflow': 'workflow_id'}[fault]
            raw[field] = 'unrelated'
        path.write_text(json.dumps(raw, ensure_ascii=False))
    writes = []

    def observed(store, record, snapshot):
        writes.append(record.workflow_run_id)
        return original(store, record, snapshot)

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', observed)
    before = {path: path.read_bytes() for path in store.root.glob('*/run.json')}
    result = tool.execute(params)
    assert not result.ok, 'broken or revoked child association must not report repaired'
    assert writes == [], 'validate every descendant before publishing even the first clock'
    assert calls == ['leaf-0', 'leaf-1']
    assert {path: path.read_bytes() for path in before} == before


def test_same_key_retry_keeps_parent_and_all_auxiliary_failures(tmp_path, monkeypatch):
    calls = []
    api, actor, tool = business_tree(tmp_path, 2,
        lambda spec, context, skills: calls.append(spec['step_id']) or {'done': True})
    original = WorkflowStore._record_completed_run

    def unavailable(store, record, snapshot):
        if record.workflow_id.startswith('child-clock-'):
            raise OSError('children still unavailable')
        return original(store, record, snapshot)

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', unavailable)
    params = {'action': 'workflows.run', 'workflow_ref': 'completion-clock', 'idempotency_key': 'parent-run'}
    assert not tool.execute(params).ok
    parent = api._run_store().load_run(parent_id(actor))
    result = tool.execute(params)
    assert not result.ok and result.effect_outcome == 'unknown'
    assert result.result_envelope['workflow_run_id'] == parent.workflow_run_id
    assert {row['workflow_run_id'] for row in result.result_envelope['completion_clock_failures']} == {
        step.subworkflow_run_id for step in parent.steps}
    assert calls == ['leaf-0', 'leaf-1']


@pytest.mark.parametrize('shape', ['nested', 'foreach'])
def test_same_key_recovery_uses_frozen_nested_and_batch_attempts(tmp_path, monkeypatch, shape):
    import uuid

    calls = []
    api, actor, tool = business_tree(tmp_path, 1,
        lambda spec, context, skills: calls.append(spec['step_id']) or {'done': True})
    if shape == 'nested':
        grand = 'child-clock-grand'
        api.execute('workflows.create', {'definition': definition(grand, [
            {'step_id': 'grand-leaf', 'name': grand, 'mode': 'llm_skill_analysis'}])},
            actor=actor, idempotency_key='create-grand')
        api.execute('workflows.update', {'workflow_ref': 'child-clock-0',
            'definition': definition('child-clock-0', [{'step_id': 'grand', 'name': grand,
                'mode': 'subworkflow', 'subworkflow_ref': 'workflow:' + grand}])}, actor=actor,
            expected_revision=api._get('child-clock-0').descriptor.revision, idempotency_key='nested')
    else:
        mutate(api, actor, 'workflows.update', {'definition': definition(steps=[
            {'step_id': 'batch', 'name': 'batch', 'mode': 'subworkflow',
             'subworkflow_ref': 'workflow:child-clock-0', 'foreach': {
                 'from': 'inputs.items', 'batch_size': 1, 'max_parallel': 2}}])}, 'batch-parent')
    original = WorkflowStore._record_completed_run

    def unavailable(store, record, snapshot):
        if record.workflow_id.startswith('child-clock-'):
            raise OSError('child completion clock unavailable')
        return original(store, record, snapshot)

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', unavailable)
    params = {'action': 'workflows.run', 'workflow_ref': 'completion-clock',
              'idempotency_key': 'parent-run', 'inputs': {'items': [1, 2]}}
    assert not tool.execute(params).ok
    store = api._run_store()
    parent = store.load_run(parent_id(actor))
    if shape == 'nested':
        child = store.load_run(parent.steps[0].subworkflow_run_id)
        descendants = [child, store.load_run(child.steps[0].subworkflow_run_id)]
        expected_calls = ['grand-leaf']
    else:
        descendants = [store.load_run(uuid.uuid5(uuid.NAMESPACE_URL,
            f"subworkflow:{parent.workflow_run_id}:batch:{row['attempt_id']}").hex)
            for row in parent.steps[0].batches]
        expected_calls = ['leaf-0', 'leaf-0']
    assert all(workflow_state(api._get(child.workflow_id))['last_run_at'] is None for child in descendants)
    before = {child.workflow_run_id: store._read(child.workflow_run_id, 'run.json') for child in descendants}
    monkeypatch.setattr(WorkflowStore, '_record_completed_run', original)
    result = tool.execute(params)
    assert result.ok and result.result_envelope['workflow_run_id'] == parent.workflow_run_id
    assert calls == expected_calls
    for child in descendants:
        assert workflow_state(api._get(child.workflow_id))['last_run_at'] >= child.ended_at
        assert store._read(child.workflow_run_id, 'run.json') == before[child.workflow_run_id]


def test_replay_copied_closed_context_cannot_discard_completion(tmp_path, monkeypatch):
    from contextvars import copy_context

    from agent_py_agent.agent.workflows.run_store import _COMPLETION_HANDOFF

    captured = []

    def analyze(spec, context, skills):
        captured.append(copy_context())
        return {'done': True}

    api, actor, tool = business_tree(tmp_path, 1, analyze)
    original = WorkflowStore._record_completed_run

    def unavailable(store, record, snapshot):
        if record.workflow_id.startswith('child-clock-'):
            raise OSError('clock unavailable while original frame closes')
        return original(store, record, snapshot)

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', unavailable)
    params = {'action': 'workflows.run', 'workflow_ref': 'completion-clock', 'idempotency_key': 'parent-run'}
    assert not tool.execute(params).ok
    parent = api._run_store().load_run(parent_id(actor))
    child = api._run_store().load_run(parent.steps[0].subworkflow_run_id)
    assert len(captured) == 1 and _COMPLETION_HANDOFF.get() is None
    assert workflow_state(api._get(child.workflow_id))['last_run_at'] is None
    monkeypatch.setattr(WorkflowStore, '_record_completed_run', original)
    replay = captured[0].run(tool.execute, params)
    assert replay.ok and replay.result_envelope['workflow_run_id'] == parent.workflow_run_id
    assert workflow_state(api._get(child.workflow_id))['last_run_at'] == child.ended_at
    assert len(captured) == 1 and _COMPLETION_HANDOFF.get() is None


def test_clock_only_recovery_does_not_request_review_of_nonexistent_run_step(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows.governance import WorkflowGovernance

    calls, reviews = [], []
    api, actor, tool = business_tree(tmp_path, 1,
        lambda spec, context, skills: calls.append(spec['step_id']) or {'done': True})
    original = WorkflowStore._record_completed_run

    def unavailable(store, record, snapshot):
        raise OSError('completion clock unavailable')

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', unavailable)
    params = {'action': 'workflows.run', 'workflow_ref': 'completion-clock', 'idempotency_key': 'parent-run'}
    assert not tool.execute(params).ok
    store = api._run_store()
    parent = store.load_run(parent_id(actor))
    child = store.load_run(parent.steps[0].subworkflow_run_id)
    before = {record.workflow_run_id: store._read(record.workflow_run_id, 'run.json')
              for record in (parent, child)}

    def policy(record, step_id):
        reviews.append(step_id)
        raise AssertionError('clock recovery must not request new step effect approval')

    api.governance = WorkflowGovernance(store, policy_provider=policy,
        receipt_reader=lambda *args: None, authorization_check=lambda *args: True)
    monkeypatch.setattr(WorkflowStore, '_record_completed_run', original)
    result = tool.execute(params)
    assert result.ok and result.result_envelope['workflow_run_id'] == parent.workflow_run_id
    assert calls == ['leaf-0'] and reviews == []
    for record in (parent, child):
        assert workflow_state(api._get(record.workflow_id))['last_run_at'] == record.ended_at
        assert store._read(record.workflow_run_id, 'run.json') == before[record.workflow_run_id]


@pytest.mark.parametrize('error_type', [ToolCancelled, InterruptedError, RuntimeError])
def test_keyerror_clock_keeps_primary_object_and_attempts_other_completions(tmp_path, monkeypatch, error_type):
    api, actor, tool = business_tree(tmp_path, 2, lambda *args: {'done': True})
    attempted = []
    primary = error_type('original exit')
    original_cause = ValueError('original cause')
    primary.__cause__ = original_cause

    def unavailable(store, record, snapshot):
        attempted.append(record.workflow_id)
        raise KeyError('scope:' + record.workflow_id)

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', unavailable)
    with pytest.raises(error_type) as raised:
        with api._run_store().execution_lock('outer-error'):
            receipt = api.execute('workflows.run', {'workflow_ref': 'completion-clock'},
                                  actor=actor, idempotency_key='primary-exit')
            assert receipt['state'] == 'completed'
            raise primary
    assert raised.value is primary and primary.__cause__ is original_cause
    assert set(attempted) == {'completion-clock', 'child-clock-0', 'child-clock-1'}
    notes = '\n'.join(primary.__notes__)
    assert 'KeyError' in notes and all('scope:' + ref in notes for ref in attempted)


def test_keyerror_clock_preserves_concrete_cause_without_primary(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows.run_store import WorkflowExecutionError

    api, actor = service(tmp_path, [time.time() - 60])
    cause = KeyError('scope')

    def unavailable(*args):
        raise cause

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', unavailable)
    with pytest.raises(WorkflowExecutionError) as raised:
        api.execute('workflows.run', {'workflow_ref': 'completion-clock'},
                    actor=actor, idempotency_key='keyerror')
    assert raised.value.cause is cause
    assert raised.value.receipt['error_type'] == 'KeyError'
    assert raised.value.receipt['terminal_committed'] is True
    assert raised.value.receipt['completion_clock_failures'][0]['error_type'] == 'KeyError'


def test_unexecuted_subworkflow_without_target_replays_successfully(tmp_path):
    calls = []
    api, actor = service(tmp_path, [time.time() - 60], lambda *args: calls.append('child'))
    mutate(api, actor, 'workflows.update', {'definition': definition(steps=[{
        'step_id': 'not-called', 'name': 'not-called', 'mode': 'subworkflow',
        'condition': {'step_id': '$inputs', 'key': 'execute', 'equals': True}}])}, 'conditional')
    tool = WorkflowsTool(tmp_path, business_service=api, business_actor_provider=lambda: actor)
    params = {'action': 'workflows.run', 'workflow_ref': 'completion-clock',
              'inputs': {'execute': False}, 'idempotency_key': 'not-called'}
    first = tool.execute(params)
    assert first.ok and first.result_envelope['state'] == 'completed'
    run = api._run_store().load_run(first.result_envelope['workflow_run_id'])
    assert run.status == 'succeeded' and run.steps[0].status == 'skipped'
    assert not run.steps[0].subworkflow_run_id and not run.steps[0].escalation_result.get('effect_started')
    before = (api._run_store().root / run.workflow_run_id / 'run.json').read_bytes()
    replay = tool.execute(params)
    assert replay.ok and replay.result_envelope == first.result_envelope
    assert calls == [] and api._run_store().list_runs(limit=10) == [run]
    assert (api._run_store().root / run.workflow_run_id / 'run.json').read_bytes() == before


@pytest.mark.parametrize('nested', [False, True])
def test_closed_context_replayed_under_real_lock_defers_writer(tmp_path, monkeypatch, nested):
    from contextvars import copy_context

    from agent_py_agent.agent.workflows.run_store import _COMPLETION_HANDOFF, _HELD_LOCKS

    captured, writes = [], []
    api, actor, tool = business_tree(tmp_path, 1,
        lambda *args: captured.append(copy_context()) or {'done': True})
    params = {'action': 'workflows.run', 'workflow_ref': 'completion-clock', 'idempotency_key': 'parent-run'}
    assert tool.execute(params).ok
    original = WorkflowStore._record_completed_run

    def observed(store, record, snapshot):
        writes.append(record.workflow_id)
        assert not getattr(_HELD_LOCKS, 'paths', set()), 'writer entered under a real run lock'
        return original(store, record, snapshot)

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', observed)
    store = api._run_store()
    with store.execution_lock('live-outer'):
        if nested:
            with store.execution_lock('live-inner'):
                result = captured[0].run(tool.execute, params)
        else:
            result = captured[0].run(tool.execute, params)
        assert result.ok and writes == []
    assert set(writes) == {'completion-clock', 'child-clock-0'}
    assert not _HELD_LOCKS.paths and _COMPLETION_HANDOFF.get() is None
    assert len(captured) == 1


@pytest.mark.parametrize('exit_kind', ['normal', 'body_error'])
def test_closed_context_real_lock_event_interleaving_and_busy_cleanup(tmp_path, monkeypatch, exit_kind):
    from contextvars import copy_context

    from agent_py_agent.agent.assets import asset_root_lock
    from agent_py_agent.agent.workflows.run_store import _COMPLETION_HANDOFF, _HELD_LOCKS

    captured, writes = [], []
    replayed, release, publishing = Event(), Event(), Event()
    api, actor, tool = business_tree(tmp_path, 1,
        lambda *args: captured.append(copy_context()) or {'done': True})
    params = {'action': 'workflows.run', 'workflow_ref': 'completion-clock', 'idempotency_key': 'parent-run'}
    assert tool.execute(params).ok
    store = api._run_store()
    original = WorkflowStore._record_completed_run
    primary = RuntimeError('body exit')

    def observed(asset_store, record, snapshot):
        assert not getattr(_HELD_LOCKS, 'paths', set())
        assert getattr(_HELD_LOCKS, 'handoff', None) is None
        writes.append(record.workflow_id)
        publishing.set()
        return original(asset_store, record, snapshot)

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', observed)

    def nested_replay():
        # 真正嵌套锁在关闭Context已覆盖当前帧后才进入。
        with store.execution_lock('inner-event'):
            assert tool.execute(params).ok
        assert writes == []
        replayed.set()
        assert release.wait(10)
        if exit_kind == 'body_error':
            raise primary

    def worker():
        try:
            with store.execution_lock('outer-event'):
                captured[0].run(nested_replay)
        except RuntimeError as error:
            assert exit_kind == 'body_error' and error is primary
        finally:
            assert not _HELD_LOCKS.paths and _COMPLETION_HANDOFF.get() is None
            assert _HELD_LOCKS.handoff is None

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        try:
            assert replayed.wait(10)
            # 复制的Context不授予其它线程真实持锁资格；同run仍严格互斥。
            with pytest.raises(RuntimeError, match='运行锁繁忙'):
                with store.execution_lock('outer-event'):
                    pytest.fail('busy execution lock was acquired')
            assert not getattr(_HELD_LOCKS, 'paths', set()) and _COMPLETION_HANDOFF.get() is None
            with asset_root_lock(tmp_path):
                release.set()
                assert publishing.wait(10)
                assert store.load_run(parent_id(actor)).status == 'succeeded'
        finally:
            release.set()
        future.result(timeout=10)
    assert set(writes) == {'completion-clock', 'child-clock-0'}
    assert not _HELD_LOCKS.paths and _COMPLETION_HANDOFF.get() is None


def test_completion_writer_baseexception_is_not_swallowed(tmp_path, monkeypatch):
    class ControlExit(BaseException):
        pass

    api, actor = service(tmp_path, [time.time() - 60])
    signal = ControlExit('control exit')

    def interrupted(*args):
        raise signal

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', interrupted)
    with pytest.raises(ControlExit) as raised:
        api.execute('workflows.run', {'workflow_ref': 'completion-clock'},
                    actor=actor, idempotency_key='control-exit')
    assert raised.value is signal


def test_recovery_accepts_legitimate_nonbusiness_child_source(tmp_path, monkeypatch):
    from agent_py_agent.agent.assets.workflow_store import BUSINESS_WORKFLOW_METADATA

    calls, writes = [], []
    api, actor, tool = business_tree(tmp_path, 0, lambda *args: calls.append('leaf') or {'done': True})
    # 原资产存储允许无业务元数据的普通Owner工作流；不是损坏业务来源的no-op。
    api.store.save_workflow(definition('plain-child', [
        {'step_id': 'leaf', 'name': 'leaf', 'mode': 'llm_skill_analysis'}]))
    assert BUSINESS_WORKFLOW_METADATA not in api._get('plain-child').descriptor.metadata
    mutate(api, actor, 'workflows.update', {'definition': definition(steps=[{
        'step_id': 'plain', 'name': 'plain', 'mode': 'subworkflow',
        'subworkflow_ref': 'workflow:plain-child'}])}, 'plain-parent')
    params = {'action': 'workflows.run', 'workflow_ref': 'completion-clock', 'idempotency_key': 'parent-run'}
    assert tool.execute(params).ok
    original = WorkflowStore._record_completed_run

    def observed(store, record, snapshot):
        writes.append(record.workflow_id)
        return original(store, record, snapshot)

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', observed)
    result = tool.execute(params)
    assert result.ok and result.result_envelope['state'] == 'completed'
    assert writes == ['completion-clock'] and calls == ['leaf']


@pytest.mark.parametrize('fault', ['source_scope', 'scope_layer', 'owner_id', 'empty_owner', 'matching_owner'])
def test_nonbusiness_frozen_source_recovery_validates_before_parent_clock(tmp_path, monkeypatch, fault):
    import json

    from agent_py_agent.agent.assets.workflow_store import BUSINESS_WORKFLOW_METADATA

    calls, writes = [], []
    api, actor, tool = business_tree(tmp_path, 0, lambda *args: calls.append('leaf') or {'done': True})
    plain = definition('plain-child', [
        {'step_id': 'leaf', 'name': 'leaf', 'mode': 'llm_skill_analysis'}])
    plain['descriptor']['owner_id'] = actor.owner_id if fault == 'matching_owner' else ''
    api.store.save_workflow(plain)
    mutate(api, actor, 'workflows.update', {'definition': definition(steps=[{
        'step_id': 'plain', 'name': 'plain', 'mode': 'subworkflow',
        'subworkflow_ref': 'workflow:plain-child'}])}, 'plain-parent')
    original = WorkflowStore._record_completed_run

    def unavailable(store, record, snapshot):
        assert record.workflow_id == 'completion-clock'
        raise OSError('parent clock temporarily unavailable')

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', unavailable)
    params = {'action': 'workflows.run', 'workflow_ref': 'completion-clock', 'idempotency_key': 'parent-run'}
    first = tool.execute(params)
    assert not first.ok and first.result_envelope['terminal_committed'] is True
    store = api._run_store()
    parent = store.load_run(parent_id(actor))
    child = store.load_run(parent.steps[0].subworkflow_run_id)
    assert parent.status == child.status == 'succeeded' and calls == ['leaf']
    assert workflow_state(api._get('completion-clock'))['last_run_at'] is None
    assert workflow_state(api._get('plain-child'))['last_run_at'] is None
    path = store.root / child.workflow_run_id / 'run.json'
    raw = json.loads(path.read_text())
    source = raw['workflow_snapshot']['descriptor']
    assert BUSINESS_WORKFLOW_METADATA not in source['metadata']
    assert source['owner_id'] == plain['descriptor']['owner_id']
    broken = fault in ('source_scope', 'scope_layer', 'owner_id')
    if broken:
        source[fault] = 'another-owner' if fault == 'owner_id' else 'unrelated'
        path.write_text(json.dumps(raw, ensure_ascii=False))
    before = {item: item.read_bytes() for item in store.root.glob('*/run.json')}
    assert len(before) == 2

    def observed(asset_store, record, snapshot):
        writes.append(record.workflow_id)
        return original(asset_store, record, snapshot)

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', observed)
    replay = tool.execute(params)
    # 先收集副作用再裁定回执，使修复前JUnit留下真实writer/clock观察。
    after = {item: item.read_bytes() for item in before}
    parent_clock = workflow_state(api._get('completion-clock'))['last_run_at']
    assert calls == ['leaf'] and after == before
    assert workflow_state(api._get('plain-child'))['last_run_at'] is None
    if broken:
        assert not replay.ok, (fault, replay.result_envelope, writes, parent_clock)
        expected = ('WORKFLOW_COMPLETION_SNAPSHOT_SOURCE_MISMATCH' if fault == 'owner_id'
                    else '资产来源层无效')
        assert replay.result_envelope['error'] == expected
        assert writes == [] and parent_clock is None
    else:
        assert replay.ok and replay.result_envelope['state'] == 'completed'
        assert writes == ['completion-clock'] and parent_clock == parent.ended_at
