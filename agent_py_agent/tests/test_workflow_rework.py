"""静态审核反馈触发有限规划，不把工具重试当返工，不重放实际写入。"""
import pytest

from agent_py_agent.agent.verification.repository import VerificationContext
from agent_py_agent.agent.verification.requirement_provenance import IndependentReviewRecord
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.native_governance import NativeWorkflowGovernance
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_workflow_script_engine import script, step, workflow


def case(tmp_path, *, max_rounds=3, max_no_progress_rounds=2, prefix=False):
    effects = tmp_path / 'effects'
    effects.mkdir()
    target = effects / 'effect.txt'
    script(tmp_path, f'from pathlib import Path\nPath({str(target)!r}).write_text("executed")\nprint(\'{{"value":1}}\')')
    definition = workflow(step('effect'), step('replan', mode='llm_skill_analysis',
        config={'analysis_isolation': 'stateless_skill'}))
    if prefix:
        from agent_py_agent.agent.assets.descriptor import AssetDescriptor
        from agent_py_agent.agent.assets.script_store import ScriptStore
        marker = effects / 'prefix.txt'
        ScriptStore(tmp_path).save_script(AssetDescriptor(id='prefix', kind='script', name='成功前序'),
            f'from pathlib import Path\np=Path({str(marker)!r})\np.write_text((p.read_text() if p.exists() else "")+"once")\nprint(\'{{"prefix":true}}\')')
        definition['steps'].insert(0, step('prefix', script_id='prefix'))
    definition['descriptor']['metadata'] = {'workflow_review_policy': {'steps': ['effect']},
        'workflow_rework_policy': {'replan_step_id': 'replan', 'max_rounds': max_rounds,
            'max_no_progress_rounds': max_no_progress_rounds}}
    store = WorkflowRunStore(tmp_path)
    gate = NativeWorkflowGovernance(None, store, owner_home=tmp_path, owner_id='alice', author_provider=lambda: 'author')
    calls = []
    def analyze(spec, context, skills):
        calls.append(context['rework_feedback'])
        return {'plan': 'revise exact candidate', 'round': len(calls)}
    engine = WorkflowEngine(tmp_path, run_authorizer=gate.require_effect, analysis_handler=analyze)
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    engine.runner = ScriptRunner(tmp_path, extra_write_roots=(effects,))
    first = engine.run_workflow(definition)
    binding = next(item for item in first.steps if item.step_id == 'effect').escalation_result['pending_review']
    requirement = gate.repository.get_requirement('alice', binding['requirement_id'])
    gate.repository.record_independent_review(VerificationContext('alice', '', first.workflow_run_id),
        IndependentReviewRecord('request-change', binding['requirement_id'], 'reviewer', requirement.author_principal_id,
            binding['input_version'], 'changes_requested', 'workflow_effect.v1', owner_ref='alice'))
    return engine, store, first, calls, target


@pytest.mark.parametrize(('round_limit', 'idle_limit', 'expected_calls', 'code'), [
    (2, 10, 2, 'WORKFLOW_REWORK_LIMIT_REACHED'), (10, 1, 1, 'WORKFLOW_REWORK_NO_PROGRESS')])
def test_replan_is_finite_and_does_not_execute_rejected_effect(tmp_path, round_limit, idle_limit, expected_calls, code):
    engine, store, first, calls, target = case(tmp_path, max_rounds=round_limit, max_no_progress_rounds=idle_limit, prefix=True)
    current = first
    for _ in range(expected_calls + 2):
        current = engine.resume_workflow_run(first.workflow_run_id)
    assert len(calls) == expected_calls
    effect = next(item for item in current.steps if item.step_id == 'effect')
    assert current.status == 'paused_for_resume' and effect.error_code == code
    assert effect.escalation_result['rework_state']['rounds'] == expected_calls
    assert effect.output_snapshot == {} and current.final_output == {'prefix': True}
    assert (tmp_path / 'effects' / 'prefix.txt').read_text() == 'once'
    assert not target.exists()


def test_cancellation_precedes_replan_and_original_gate_cannot_be_removed(tmp_path):
    engine, store, first, calls, target = case(tmp_path)
    paused = engine.resume_workflow_run(first.workflow_run_id)
    cancelled = engine.cancel_workflow_run(first.workflow_run_id, expected_revision=paused.revision, operation_key='cancel-original')
    assert cancelled == [] and store.load_run(first.workflow_run_id).status == 'cancelled'
    with pytest.raises(ValueError, match='WORKFLOW_CANCELLED'):
        engine.resume_workflow_run(first.workflow_run_id)
    assert len(calls) == 1 and not target.exists()


def test_replan_policy_rejects_effectful_or_unbounded_planner(tmp_path):
    from agent_py_agent.agent.assets.workflow_store import WorkflowDefinition
    definition = workflow(step('effect'))
    for policy in [{'replan_step_id': 'effect', 'max_rounds': 3},
                   {'replan_step_id': 'effect', 'max_rounds': 0}]:
        definition['descriptor']['metadata'] = {'workflow_rework_policy': policy}
        with pytest.raises(ValueError, match='WORKFLOW_RE(WORK|PLAN)'):
            WorkflowDefinition.from_any(definition)


def test_same_control_replay_does_not_consume_another_planning_round(tmp_path):
    engine, store, first, calls, target = case(tmp_path)
    one = engine.resume_workflow_run(first.workflow_run_id, expected_revision=first.revision, operation_key='plan-once')
    again = engine.resume_workflow_run(first.workflow_run_id, expected_revision=first.revision, operation_key='plan-once')
    assert one.to_dict() == again.to_dict()
    assert len(calls) == 1 and not target.exists()


def test_unknown_prior_effect_is_reconciled_before_any_replan(tmp_path):
    engine, store, first, calls, target = case(tmp_path)
    first.effect_outcome = 'unknown'
    first.steps[0].escalation_result['effect_started'] = True
    store.save_run(first, resuming=True)
    with pytest.raises(PermissionError, match='WORKFLOW_RECONCILIATION_REQUIRED'):
        engine.resume_workflow_run(first.workflow_run_id)
    assert calls == [] and not target.exists()


def test_failed_planner_keeps_budget_and_specific_diagnostic(tmp_path):
    engine, store, first, calls, target = case(tmp_path, max_rounds=2, max_no_progress_rounds=10)
    def failed(*args):
        calls.append(1)
        raise RuntimeError('isolated planner unavailable')
    engine.analysis_handler = failed
    one = engine.resume_workflow_run(first.workflow_run_id)
    state = one.steps[0].escalation_result['rework_state']
    assert one.steps[0].error_code == 'WORKFLOW_REPLAN_FAILED'
    assert state['error_code'] and 'RuntimeError' in one.steps[0].error_message
    engine.resume_workflow_run(first.workflow_run_id)
    end = engine.resume_workflow_run(first.workflow_run_id)
    assert end.steps[0].error_code == 'WORKFLOW_REWORK_LIMIT_REACHED'
    assert len(calls) == 2 and not target.exists()


def test_cancellation_after_planning_started_preserves_consumed_round_and_effect_output(tmp_path):
    from agent_py_agent.agent.common.cancellation import (
        CancellationToken,
        ToolCancelled,
        bind_cancellation_token,
    )
    engine, store, first, calls, target = case(tmp_path)
    token = CancellationToken()
    def cancelled_after_response(*args):
        calls.append(1)
        token.cancel('cancel after planning response')
        return {'value': 'untrusted plan cannot replace business output'}
    engine.analysis_handler = cancelled_after_response
    with bind_cancellation_token(token), pytest.raises(ToolCancelled):
        engine.resume_workflow_run(first.workflow_run_id)
    current = store.load_run(first.workflow_run_id)
    effect = current.steps[0]
    assert effect.escalation_result['rework_state']['rounds'] == 1
    assert effect.output_snapshot == {} and current.final_output == {}
    assert len(calls) == 1 and not target.exists()


def test_disabled_planning_dependency_is_checked_before_consuming_round(tmp_path):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDisabledError
    engine, store, first, calls, target = case(tmp_path)
    original = engine._require_step_enabled
    def check(spec):
        if spec['step_id'] == 'replan':
            raise AssetDisabledError('skill', 'disabled-planner')
        return original(spec)
    engine._require_step_enabled = check
    current = engine.resume_workflow_run(first.workflow_run_id)
    assert current.steps[0].error_code == 'ASSET_DISABLED_BY_OWNER'
    assert current.steps[0].escalation_result['rework_state']['rounds'] == 0
    assert calls == [] and not target.exists()
