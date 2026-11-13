"""原生DAG审核拒绝未启动业务；已启动未知仍须对账，不重放非幂等效果。"""
import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.verification.repository import VerificationContext
from agent_py_agent.agent.verification.requirement_provenance import IndependentReviewRecord
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.governance import WorkflowReconciliationRequired
from agent_py_agent.agent.workflows.native_governance import NativeWorkflowGovernance
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_acceptance_workflow_native_ports import wire  # noqa: F401
from agent_py_agent.tests.test_wf_07_08_foreach_and_dag import _flow


def case(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    target = workspace / 'writes.txt'
    ScriptStore(tmp_path).save_script(AssetDescriptor(id='once', kind='script', name='一次写入'),
        f'from pathlib import Path\np=Path({str(target)!r})\np.write_text((p.read_text() if p.exists() else "")+"effect\\n")\nprint(\'{{"value":7}}\')')
    store = WorkflowRunStore(tmp_path)
    governance = NativeWorkflowGovernance(None, store, owner_home=tmp_path,
        owner_id='alice', author_provider=lambda: 'original-author')
    calls = []
    def analyze(*_):
        calls.append('prefix')
        return {'value': 7}
    engine = WorkflowEngine(tmp_path, workspace_root=workspace, analysis_handler=analyze, run_authorizer=governance.require_effect)
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    engine.runner = ScriptRunner(tmp_path, workspace_root=workspace, extra_write_roots=(workspace,))
    definition = _flow([{'step_id': 'prefix', 'mode': 'llm_skill_analysis'},
        {'step_id': 'effect', 'mode': 'deterministic_script', 'script_id': 'once', 'depends_on': ['prefix']}],
        descriptor={'metadata': {'workflow_review_policy': {'steps': ['effect'], 'minimum_reviews': 1}}})
    run = engine.run_workflow(definition)
    assert run.steps[1].error_code == 'WORKFLOW_REVIEW_REQUIRED'
    return engine, governance, run, target, calls


def approve(governance, run):
    binding = run.steps[1].escalation_result['pending_review']
    requirement = governance.repository.get_requirement('alice', binding['requirement_id'])
    governance.repository.record_independent_review(VerificationContext('alice', '', run.workflow_run_id),
        IndependentReviewRecord('exact-review', binding['requirement_id'], 'independent-reviewer',
            requirement.author_principal_id, binding['input_version'], 'pass', 'workflow_effect.v1', owner_ref='alice'))
    return binding


def test_native_dag_first_review_refusal_preserves_original_attempt_and_resumes_once(tmp_path):
    engine, governance, run, target, calls = case(tmp_path)
    assert not target.exists() and calls == ['prefix']
    assert run.steps[1].escalation_result['effect_started'] is False
    binding = approve(governance, run)
    resumed = engine.resume_workflow_run(run.workflow_run_id)
    assert resumed.status == 'succeeded', resumed.to_dict()
    assert resumed.steps[1].escalation_result['attempt_id'] == binding['attempt_id']
    assert resumed.steps[1].escalation_result['review_refs'] == ['exact-review']
    assert calls == ['prefix'] and target.read_text() == 'effect\n'


@pytest.mark.parametrize('unknown', ['legacy_dispatch', 'other_started_step'])
def test_old_unknown_or_other_partial_effect_still_requires_reconciliation(tmp_path, unknown):
    engine, governance, run, target, calls = case(tmp_path)
    approve(governance, run)
    step = run.steps[1] if unknown == 'legacy_dispatch' else run.steps[0]
    step.escalation_result['effect_started'] = True
    if unknown == 'other_started_step':
        step.status, step.needs_repair = 'failed', True
    run.effect_outcome = 'unknown'
    engine.store.save_run(run)
    with pytest.raises(WorkflowReconciliationRequired):
        engine.resume_workflow_run(run.workflow_run_id)
    assert calls == ['prefix'] and not target.exists()
    assert engine.store.load_run(run.workflow_run_id).steps[1].escalation_result['attempt_id'] == run.steps[1].escalation_result['attempt_id']


def test_raw_tool_preserves_reconciliation_code_and_reason(wire, tmp_path):  # noqa: F811
    from agent_py_agent.agent.agent_core.host_turn_scope import host_turn_scope
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.tests.test_workflow_native_governance import pending_review

    agent, _, store, run, _, _, _, target = pending_review(wire, tmp_path)
    run.steps[0].escalation_result['effect_started'] = True
    run.effect_outcome = 'unknown'
    store.save_run(run)
    with host_turn_scope(agent, '', RunParams(run_id='resume-check', request_id='resume-check', save=False),
                        reason='unknown_resume_diagnostic', tool_rounds=1):
        result = agent.tools.tools['workflows'].execute({'action': 'resume', 'workflow_run_id': run.workflow_run_id})
    assert not result.ok and result.error_code == 'WORKFLOW_RECONCILIATION_REQUIRED', result.output
    assert '"reason"' in result.output and 'operation_' in result.output
    assert result.effect_outcome == 'not_started' and result.retryable is False
    assert '核对' in result.recovery_hint and not target.exists()


@pytest.mark.parametrize('bad_marker', ['missing', None, 0, '', True])
@pytest.mark.parametrize('overall_outcome', ['unknown', 'failed'])
def test_missing_or_wrong_typed_effect_marker_never_grants_pending_resume(tmp_path, bad_marker, overall_outcome):
    engine, governance, run, target, _ = case(tmp_path)
    approve(governance, run)
    if bad_marker == 'missing':
        run.steps[1].escalation_result.pop('effect_started')
    else:
        run.steps[1].escalation_result['effect_started'] = bad_marker
    run.effect_outcome = overall_outcome
    engine.store.save_run(run)
    with pytest.raises(WorkflowReconciliationRequired):
        engine.resume_workflow_run(run.workflow_run_id)
    assert not target.exists()


def test_later_guard_refusal_preserves_already_executed_worker_effect(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows.dag_scheduler import _run_step_worker
    from agent_py_agent.agent.workflows.native_governance import WorkflowReviewPending

    engine, _, run, target, _ = case(tmp_path)
    step = run.steps[1]
    step.escalation_result['effect_started'] = True  # 协调线程派发义务。
    run._batch_worker = True
    calls = []
    def authorize(_run, _phase):
        calls.append('admit')
        if len(calls) == 2:
            raise WorkflowReviewPending(step.escalation_result['pending_review'])
    engine.run_authorizer = authorize
    def execute(record, spec, outputs, definition, local_run):
        engine._admit_effect(local_run, record)
        target.write_text('actual non-idempotent effect')
        engine._admit_effect(local_run, record)  # 真实写后第二个准入点撤权/缺审核。
    monkeypatch.setattr(engine, '_execute_step_with_retry_and_watermarks', execute)
    returned, error = _run_step_worker(engine, step, {}, {}, {}, run)
    assert error is None and returned.status == 'failed'
    assert returned.escalation_result['effect_started'] is True
    assert target.read_text() == 'actual non-idempotent effect'
