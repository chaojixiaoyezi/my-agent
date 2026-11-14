"""同候选的不同审核者不能互相替票；仅本人有效改票解除原返工。"""
import pytest

from agent_py_agent.agent.verification.repository import VerificationContext
from agent_py_agent.agent.verification.requirement_provenance import IndependentReviewRecord
from agent_py_agent.tests.test_workflow_dag_review_resume import case


def test_native_review_changes_requested_survives_other_pass_and_scope_or_expired_updates(tmp_path):
    engine, governance, run, target, calls = case(tmp_path)
    binding = run.steps[1].escalation_result['pending_review']
    requirement = governance.repository.get_requirement('alice', binding['requirement_id'])
    context = VerificationContext('alice', '', run.workflow_run_id)

    def vote(name, reviewer, verdict, *, scope='workflow_effect.v1', created_at=''):
        governance.repository.record_independent_review(context, IndependentReviewRecord(name,
            binding['requirement_id'], reviewer, requirement.author_principal_id, binding['input_version'],
            verdict, scope, created_at=created_at, owner_ref='alice'))

    vote('A-change', 'reviewer-A', 'changes_requested')
    vote('B-pass', 'reviewer-B', 'pass')
    assert governance.repository.get_requirement('alice', binding['requirement_id']).review_status == 'approved'
    first = engine.resume_workflow_run(run.workflow_run_id)
    assert first.steps[1].error_code == 'WORKFLOW_REVIEW_CHANGES_REQUESTED' and not target.exists()
    assert first.steps[1].escalation_result['attempt_id'] == binding['attempt_id']
    vote('A-other-purpose-pass', 'reviewer-A', 'pass', scope='ordinary-review')
    vote('A-expired-pass', 'reviewer-A', 'pass', created_at='2000-01-01T00:00:00+00:00')
    again = engine.resume_workflow_run(run.workflow_run_id)
    assert again.steps[1].error_code == 'WORKFLOW_REVIEW_CHANGES_REQUESTED' and not target.exists()
    vote('A-current-pass', 'reviewer-A', 'pass')
    # 不同用途与过期的意见不能重新阻断该workflow候选，即使原汇总status是changes_requested。
    vote('other-purpose-change', 'reviewer-C', 'changes_requested', scope='ordinary-review')
    vote('expired-change', 'reviewer-D', 'changes_requested', created_at='2000-01-01T00:00:00+00:00')
    resumed = engine.resume_workflow_run(run.workflow_run_id)
    assert resumed.status == 'succeeded', resumed.to_dict()
    assert target.read_text() == 'effect\n' and calls == ['prefix']
    assert resumed.steps[1].escalation_result['attempt_id'] == binding['attempt_id']

    # 同一实例：200条恰好完整仍可核验；第201条不能借截断掩盖任何尚未撤回的返工。
    from agent_py_agent.agent.workflows.governance import WorkflowEffectPhase
    from agent_py_agent.agent.workflows.native_governance import WorkflowReviewPending

    for index in range(193):
        vote('A-repeat-' + str(index), 'reviewer-A', 'pass')
    assert len(governance.repository.list_independent_reviews('alice', requirement_id=binding['requirement_id'], limit=200)) == 200
    assert governance.require_effect(run, WorkflowEffectPhase('effect')) is not None
    vote('A-repeat-overflow', 'reviewer-A', 'pass')
    with pytest.raises(WorkflowReviewPending, match='WORKFLOW_REVIEW_HISTORY_INCOMPLETE') as caught:
        governance.require_effect(run, WorkflowEffectPhase('effect'))
    assert caught.value.details['recommended_action'] == 'repair_against_acceptance_findings'
    assert '修订候选' in caught.value.details['recovery_hint']
    assert target.read_text() == 'effect\n'
