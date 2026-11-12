"""真实跨线程子流程与公开工具的完成计时后置失败合同。"""
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from test_acceptance_workflow_completion_clock import definition, mutate, service

from agent_py_agent.agent.assets import asset_root_lock
from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.workflows.business_scope import workflow_state


@pytest.mark.parametrize('scheduler', ['dag', 'foreach'])
def test_business_subworkflow_hands_completion_to_parent_after_all_run_locks(tmp_path, monkeypatch, scheduler):
    entered, release, publishing = Event(), Event(), Event()

    def analyze(spec, context, skills):
        if spec['step_id'] == 'leaf':
            entered.set()
            assert release.wait(10)
        return {'done': True}

    api, actor = service(tmp_path, [time.time() - 60], analyze)
    api.execute('workflows.create', {'definition': definition('child-clock', [
        {'step_id': 'leaf', 'name': 'leaf', 'mode': 'llm_skill_analysis'}])},
        actor=actor, idempotency_key='create-child')
    child = {'step_id': 'child', 'name': 'child', 'mode': 'subworkflow',
             'subworkflow_ref': 'workflow:child-clock'}
    parent = definition(steps=[child])
    if scheduler == 'dag':
        parent['descriptor']['metadata'] = {'execution_mode': 'dag', 'max_parallel_steps': 2}
        parent['steps'].insert(0, {'step_id': 'prep', 'name': 'prep', 'mode': 'llm_skill_analysis'})
        child['depends_on'] = ['prep']
    else:
        child['foreach'] = {'from': 'inputs.rows', 'batch_size': 1, 'max_parallel': 2}
    mutate(api, actor, 'workflows.update', {'definition': parent}, 'parent')
    identity = hashlib.sha256((actor.owner_id + '\0workflows.run\0run').encode()).hexdigest()
    original = WorkflowStore._record_completed_run
    observed = []

    def observe(store, record, snapshot):
        observed.append(record.workflow_run_id)
        publishing.set()
        return original(store, record, snapshot)

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', observe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(api.execute, 'workflows.run',
            {'workflow_ref': 'completion-clock', 'inputs': {'rows': [1]}}, actor=actor, idempotency_key='run')
        try:
            assert entered.wait(10), '未进入真实子流程'
            with asset_root_lock(tmp_path):
                release.set()
                assert publishing.wait(10), '未发表完成计时'
                # 超时后先退出资产锁，避免测试自身永久挂起。
                parent_run = pool.submit(api._run_store().load_run, identity).result(timeout=2)
                assert parent_run.status == 'succeeded'
        finally:
            release.set()
        assert future.result(timeout=10)['state'] == 'completed'
    runs = api._run_store().list_runs(limit=10)
    children = [run for run in runs if run.workflow_id == 'child-clock']
    assert children and all(run.status == 'succeeded' for run in children)
    assert identity in observed and all(run.workflow_run_id in observed for run in children)
    for ref in ('completion-clock', 'child-clock'):
        assert workflow_state(api._get(ref))['last_run_at'] == max(
            run.ended_at for run in runs if run.workflow_id == ref)


@pytest.mark.parametrize('error_type', [PermissionError, OSError])
def test_public_tool_retains_committed_run_on_completion_clock_failure(tmp_path, monkeypatch, error_type):
    calls = []

    def analyze(*args):
        calls.append('execute')
        return {'done': True}

    api, actor = service(tmp_path, [time.time() - 60], analyze)
    mutate(api, actor, 'workflows.update', {'definition': definition(steps=[
        {'step_id': 'work', 'name': 'work', 'mode': 'llm_skill_analysis'}])}, 'steps')
    tool = WorkflowsTool(tmp_path, business_service=api, business_actor_provider=lambda: actor)
    original = WorkflowStore._record_completed_run

    def unavailable(store, record, snapshot):
        raise error_type('完成计时暂不可写')

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', unavailable)
    params = {'action': 'workflows.run', 'workflow_ref': 'completion-clock', 'idempotency_key': 'run'}
    first = tool.execute(params)
    assert not first.ok
    assert first.effect_outcome != 'not_started'
    data = first.result_envelope
    assert data['workflow_run_id']
    assert data['status'] == 'succeeded'
    assert data['phase'] == 'completion_clock'
    assert data['terminal_committed'] is True
    assert data['effect_outcome'] == 'unknown'
    assert data['error_code'] == 'WORKFLOW_COMPLETION_CLOCK_FAILED'
    assert data['error_type'] == error_type.__name__
    assert data['error'] == '完成计时暂不可写'
    assert data['next_action'] == 'retry_same_key_completion_clock_only'
    # 宿主层沿既有枚举；领域精确阶段仍在 receipt.phase，不扩公共DTO枚举。
    assert first.failure_stage == 'effect_reconciliation'
    saved = api._run_store().load_run(data['workflow_run_id'])
    assert saved.status == 'succeeded' and calls == ['execute']
    assert workflow_state(api._get('completion-clock'))['last_run_at'] is None
    retry = tool.execute(params)
    assert not retry.ok and retry.result_envelope['workflow_run_id'] == saved.workflow_run_id
    assert retry.result_envelope['phase'] == 'completion_clock'
    assert retry.effect_outcome == 'unknown' and retry.result_envelope['terminal_committed'] is True
    monkeypatch.setattr(WorkflowStore, '_record_completed_run', original)
    repaired = tool.execute(params)
    assert repaired.ok and repaired.result_envelope['workflow_run_id'] == saved.workflow_run_id
    assert repaired.result_envelope['state'] == 'completed'
    assert calls == ['execute']
    assert workflow_state(api._get('completion-clock'))['last_run_at'] == saved.ended_at


@pytest.mark.parametrize('scheduler', ['dag', 'foreach'])
def test_parallel_business_children_coalesce_without_context_or_lock_leaks(tmp_path, monkeypatch, scheduler):
    from agent_py_agent.agent.workflows.run_store import _COMPLETION_HANDOFF, _HELD_LOCKS
    rendezvous = Barrier(2, timeout=10)

    def analyze(spec, context, skills):
        if spec['step_id'] == 'leaf':
            rendezvous.wait()
        return {'done': True}

    api, actor = service(tmp_path, [time.time() - 60], analyze)
    api.execute('workflows.create', {'definition': definition('child-clock', [
        {'step_id': 'leaf', 'name': 'leaf', 'mode': 'llm_skill_analysis'}])},
        actor=actor, idempotency_key='create-child')
    child = {'step_id': 'child', 'name': 'child', 'mode': 'subworkflow',
             'subworkflow_ref': 'workflow:child-clock'}
    parent = definition(steps=[child])
    if scheduler == 'dag':
        parent['descriptor']['metadata'] = {'execution_mode': 'dag', 'max_parallel_steps': 2}
        parent['steps'] = [
            {'step_id': 'prep', 'name': 'prep', 'mode': 'llm_skill_analysis'},
            {**child, 'depends_on': ['prep']},
            {**child, 'step_id': 'sibling', 'depends_on': ['prep']}]
    else:
        child['foreach'] = {'from': 'inputs.rows', 'batch_size': 1, 'max_parallel': 2}
    mutate(api, actor, 'workflows.update', {'definition': parent}, 'parent')
    published = []
    original = WorkflowStore._record_completed_run

    def observe(store, record, snapshot):
        assert not getattr(_HELD_LOCKS, 'paths', set())
        assert _COMPLETION_HANDOFF.get() is None
        published.append((record.workflow_id, record.ended_at))
        return original(store, record, snapshot)

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', observe)
    result = api.execute('workflows.run', {'workflow_ref': 'completion-clock', 'inputs': {'rows': [1, 2]}},
                         actor=actor, idempotency_key='run')
    assert result['state'] == 'completed'
    children = api._run_store().list_runs(workflow_id='child-clock', limit=10)
    assert len(children) == 2 and all(run.status == 'succeeded' for run in children)
    assert [row for row in published if row[0] == 'child-clock'] == [
        ('child-clock', max(run.ended_at for run in children))]
    assert not _HELD_LOCKS.paths and _COMPLETION_HANDOFF.get() is None


def test_exception_and_busy_exit_clear_completion_handoff(tmp_path):
    from agent_py_agent.agent.workflows.run_store import (
        _COMPLETION_HANDOFF,
        _HELD_LOCKS,
        WorkflowRunStore,
    )
    runs = WorkflowRunStore(tmp_path)
    with pytest.raises(RuntimeError, match='body failed'):
        with runs.execution_lock('exception'):
            assert _COMPLETION_HANDOFF.get() is not None
            raise RuntimeError('body failed')
    assert not _HELD_LOCKS.paths and _COMPLETION_HANDOFF.get() is None
    with runs.execution_lock('busy'):
        with ThreadPoolExecutor(max_workers=1) as pool:
            def rejected_worker():
                with pytest.raises(RuntimeError, match='运行锁繁忙'):
                    with runs.execution_lock('busy'):
                        pytest.fail('第二执行者取得同一运行锁')
                assert not getattr(_HELD_LOCKS, 'paths', set())
                assert _COMPLETION_HANDOFF.get() is None
            pool.submit(rejected_worker).result(timeout=5)
    assert not _HELD_LOCKS.paths and _COMPLETION_HANDOFF.get() is None
