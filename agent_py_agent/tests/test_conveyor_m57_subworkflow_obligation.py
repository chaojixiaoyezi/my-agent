from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.workflows.engine import WorkflowEngine


class AgentTasks:
    def __init__(self):
        self.submissions = []
        self.cancellations = []
        self.completed = False
        self.exit_proven = False

    def submit(self, run, spec, step):
        self.submissions.append(run.workflow_run_id)
        step.escalation_result.update(task_id='task-1', submission_state='accepted')

    def completion(self, run, step):
        if self.completed:
            return {'status': 'completed', 'completion_ref': 'completion-1',
                    'result_ref': 'result-1', 'run_id': 'task-run-1', 'output': {'value': 7}}
        return None

    def cancel(self, run, step):
        self.cancellations.append(run.workflow_run_id)
        if self.exit_proven:
            step.escalation_result['cancellation_receipt'] = {'state': 'completed', 'outstanding_refs': []}
        return self.exit_proven


def _save_workflow(engine, workflow_id, *steps):
    WorkflowStore(engine.owner_home).save_workflow(WorkflowDefinition(
        AssetDescriptor(id=workflow_id, name=workflow_id, kind='workflow', owner_id='alice'), steps))


@pytest.fixture
def workflow_chain(tmp_path):
    tasks = AgentTasks()
    engine = WorkflowEngine(tmp_path, agent_steps=tasks)
    _save_workflow(engine, 'child', WorkflowStepSpec(
        'agent', 'agent', 'agent_task', config={'instruction': 'Check the input.'}))
    _save_workflow(engine, 'parent', _delegate('delegate'))
    parent = engine.run_workflow('parent')
    assert parent.status == 'waiting_for_agent'
    child = engine.store.load_run(parent.steps[0].subworkflow_run_id)
    assert child.status == 'waiting_for_agent'
    return engine, tasks, parent, child


def _delegate(step_id):
    return WorkflowStepSpec(step_id, step_id, 'subworkflow', subworkflow_ref='workflow:child',
                            config={'output_mapping': {'answer': 'value'}})


def test_parent_waits_then_consumes_child_completion(workflow_chain):
    engine, tasks, parent, child = workflow_chain
    assert engine.resume_workflow_run(parent.workflow_run_id).status == 'waiting_for_agent'
    obligation = engine.store.load_run(parent.workflow_run_id).steps[0].escalation_result['obligation']
    assert obligation == {'kind': 'subworkflow', 'parent_run_id': parent.workflow_run_id, 'step_id': 'delegate',
                          'attempt': parent.steps[0].escalation_result['attempt_id'], 'child_run_id': child.workflow_run_id}
    tasks.completed = True
    assert engine.resume_workflow_run(child.workflow_run_id).status == 'succeeded'
    resumed = engine.resume_workflow_run(parent.workflow_run_id)
    assert resumed.status == 'succeeded'
    assert resumed.steps[0].output_snapshot == {'answer': 7}
    assert resumed.steps[0].escalation_result['subworkflow_receipt']['status'] == 'succeeded'
    assert tasks.submissions == [child.workflow_run_id]


def test_parent_cancel_waits_for_child_exit(workflow_chain, monkeypatch):
    engine, tasks, parent, child = workflow_chain
    entered, release = Event(), Event()
    cancel_task = tasks.cancel

    def observe_cancel(run, step):
        entered.set()
        assert release.wait(10)
        return cancel_task(run, step)

    monkeypatch.setattr(tasks, 'cancel', observe_cancel)
    control = {'operation_key': 'cancel-parent', 'expected_revision': parent.revision}
    with ThreadPoolExecutor(max_workers=1) as pool:
        action = pool.submit(engine.cancel_workflow_run, parent.workflow_run_id, **control)
        try:
            assert entered.wait(5), action.result() if action.done() else 'child cancellation was not requested'
            assert engine.store.load_run(parent.workflow_run_id).status == 'cancelling'
            with pytest.raises(RuntimeError, match='WORKFLOW_RUN_BUSY'):
                engine.cancel_workflow_run(parent.workflow_run_id, **control)
        finally:
            release.set()
        assert action.result(timeout=10) == [child.workflow_run_id]
    assert engine.store.load_run(child.workflow_run_id).status == 'cancelling'
    tasks.exit_proven = True
    assert engine.cancel_workflow_run(parent.workflow_run_id, **control) == []
    cancelled = engine.store.load_run(parent.workflow_run_id)
    assert cancelled.status == engine.store.load_run(child.workflow_run_id).status == 'cancelled'
    assert cancelled.steps[0].escalation_result['subworkflow_receipt']['status'] == 'cancelled'
    assert len(tasks.cancellations) == 2
    assert engine.cancel_workflow_run(parent.workflow_run_id, **control) == []
    assert len(tasks.cancellations) == 2
    with pytest.raises(ValueError, match='WORKFLOW_CANCELLED'):
        engine.resume_workflow_run(parent.workflow_run_id)


def test_legacy_waiting_parent_uses_child_receipt(workflow_chain):
    engine, tasks, parent, child = workflow_chain
    step = parent.steps[0]
    step.escalation_result.pop('obligation', None)
    step.subworkflow_run_id = ''
    engine.store.save_step_result(parent.workflow_run_id, step)
    tasks.completed = True
    assert engine.resume_workflow_run(child.workflow_run_id).status == 'succeeded'
    assert engine.resume_workflow_run(parent.workflow_run_id).status == 'succeeded'


@pytest.mark.parametrize('missing_id', [False, True])
def test_missing_child_is_unknown(workflow_chain, missing_id):
    engine, tasks, parent, child = workflow_chain
    step = parent.steps[0]
    if missing_id:
        step.escalation_result.pop('obligation', None)
        step.escalation_result['subworkflow_receipt'].pop('child_run_id')
        step.subworkflow_run_id = ''
        engine.store.save_step_result(parent.workflow_run_id, step)
    else:
        (engine.store.run_dir(child.workflow_run_id) / 'run.json').unlink()
    result = engine.resume_workflow_run(parent.workflow_run_id)
    assert result.status == 'failed' and result.effect_outcome == 'unknown'
    assert result.steps[0].error_code == 'WORKFLOW_SUBWORKFLOW_CHILD_MISSING'
    assert result.steps[0].escalation_result['effect_outcome'] == 'unknown'
    repeated = engine.resume_workflow_run(parent.workflow_run_id)
    assert repeated.revision == result.revision
    outstanding = engine.cancel_workflow_run(parent.workflow_run_id, expected_revision=result.revision,
                                             operation_key='missing-child-cancel')
    assert outstanding == ['delegate' if missing_id else child.workflow_run_id]
    assert engine.store.load_run(parent.workflow_run_id).status == 'cancelling'
    assert tasks.submissions == [child.workflow_run_id]


def test_parent_cancel_preserves_busy_child_then_consumes_terminal_record(workflow_chain, monkeypatch):
    engine, tasks, parent, child = workflow_chain
    entered, release = Event(), Event()
    tasks.completed = True
    complete_task = tasks.completion

    def observe_completion(run, step):
        entered.set()
        assert release.wait(10)
        return complete_task(run, step)

    monkeypatch.setattr(tasks, 'completion', observe_completion)
    control = {'operation_key': 'cancel-parent', 'expected_revision': parent.revision}
    with ThreadPoolExecutor(max_workers=1) as pool:
        resuming = pool.submit(engine.resume_workflow_run, child.workflow_run_id)
        try:
            assert entered.wait(5)
            assert engine.cancel_workflow_run(parent.workflow_run_id, **control) == [child.workflow_run_id]
            assert engine.store.load_run(parent.workflow_run_id).status == 'cancelling'
            assert tasks.cancellations == []
        finally:
            release.set()
        assert resuming.result(timeout=10).status == 'succeeded'
    assert engine.cancel_workflow_run(parent.workflow_run_id, **control) == []
    saved_parent = engine.store.load_run(parent.workflow_run_id)
    assert saved_parent.status == 'cancelled'
    assert saved_parent.steps[0].escalation_result['subworkflow_receipt']['status'] == 'succeeded'
    assert engine.store.load_run(child.workflow_run_id).status == 'succeeded'


def test_dag_resumes_each_original_child_before_dispatching_dependent(workflow_chain):
    engine, tasks, prior_parent, _ = workflow_chain
    ScriptStore(engine.owner_home).save_script(
        AssetDescriptor(id='join-script', name='join', kind='script', owner_id='alice'),
        'print(\'{"joined": true}\')\n',
    )
    _save_workflow(engine, 'dag-parent', _delegate('left'), _delegate('right'), WorkflowStepSpec(
        'join', 'join', 'deterministic_script', script_id='join-script', depends_on=('left', 'right')))
    parent = engine.run_workflow('dag-parent')
    assert parent.status == 'waiting_for_agent'
    left, right, join = parent.steps
    assert join.status == 'pending'
    assert left.escalation_result['obligation']['child_run_id'] == left.subworkflow_run_id
    assert right.escalation_result['obligation']['child_run_id'] == right.subworkflow_run_id

    tasks.completed = True
    engine.resume_workflow_run(right.subworkflow_run_id)
    parent = engine.resume_workflow_run(parent.workflow_run_id)
    assert [step.status for step in parent.steps] == ['waiting_for_agent', 'succeeded', 'pending']

    engine.resume_workflow_run(left.subworkflow_run_id)
    final = engine.resume_workflow_run(parent.workflow_run_id)
    assert final.status == 'succeeded', [(step.step_id, step.error_code, step.error_message) for step in final.steps]
    assert final.final_output == {'joined': True}
    assert [step.output_snapshot for step in final.steps[:2]] == [{'answer': 7}, {'answer': 7}]
    assert sorted(tasks.submissions) == sorted([
        prior_parent.steps[0].subworkflow_run_id, left.subworkflow_run_id, right.subworkflow_run_id,
    ])


def test_completed_child_recovers_after_parent_step_publication(workflow_chain, monkeypatch):
    engine, tasks, parent, child = workflow_chain
    tasks.completed = True
    assert engine.resume_workflow_run(child.workflow_run_id).status == 'succeeded'
    save_run = engine.store.save_run

    def fail_parent_publication(record, *args, **kwargs):
        if record.workflow_run_id == parent.workflow_run_id:
            raise OSError('simulated run publication error')
        return save_run(record, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(engine.store, 'save_run', fail_parent_publication)
        with pytest.raises(OSError, match='simulated run publication error'):
            engine.resume_workflow_run(parent.workflow_run_id)
    partial = engine.store.load_run(parent.workflow_run_id)
    assert partial.status == 'waiting_for_agent' and partial.steps[0].status == 'succeeded'
    assert engine.resume_workflow_run(parent.workflow_run_id).status == 'succeeded'
    assert tasks.submissions == [child.workflow_run_id]


def test_durable_subworkflow_binding_precedes_sequential_child_effect(workflow_chain, monkeypatch):
    engine, tasks, original, _ = workflow_chain
    submit = tasks.submit

    def verify_binding(run, spec, step):
        parent = engine.store.load_run(run.parent_workflow_run_id)
        binding = parent.steps[0].escalation_result['obligation']
        assert parent.workflow_run_id != original.workflow_run_id
        assert binding['kind'] == 'subworkflow'
        assert binding['child_run_id'] == run.workflow_run_id
        assert parent.steps[0].escalation_result['subworkflow_receipt']['status'] == 'dispatching'
        submit(run, spec, step)

    monkeypatch.setattr(tasks, 'submit', verify_binding)
    assert engine.run_workflow('parent').status == 'waiting_for_agent'
