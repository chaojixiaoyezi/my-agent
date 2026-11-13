"""原生工作流待审经原ORCH静态记录恢复，不在读取回调中动态签新票。"""
import pytest

from agent_py_agent.agent.verification.repository import (
    VerificationContext,
    VerificationEvidenceRepository,
)
from agent_py_agent.agent.verification.requirement_provenance import (
    IndependentReviewRecord,
    SelfReviewForbiddenError,
)
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_acceptance_workflow_native_ports import flow, wire  # noqa: F401


def pending_review(wire, tmp_path):  # noqa: F811
    agent, client = wire
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    target = agent.effective_workspace_root / 'effect.txt'
    ScriptStore(agent.home_paths.owner_home_dir).save_script(AssetDescriptor(id='reviewed', kind='script', name='审核脚本'),
        f'from pathlib import Path\nPath({str(target)!r}).write_text("once")\nprint(\'{{"value":7}}\')')
    definition = flow('native-review', [{'step_id': 'effect', 'name': 'effect', 'mode': 'deterministic_script',
        'on_error': 'fail', 'script_id': 'reviewed'}])
    definition['descriptor']['metadata'] = {'workflow_review_policy': {'steps': ['effect'], 'minimum_reviews': 1}}
    assert client.call('workflows.create', {'definition': definition})['ok']
    first = client.call('workflows.run', {'workflow_ref': 'native-review'})
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    run = store.load_run(first['data']['workflow_run_id'])
    assert run.status == 'failed' and run.steps[0].error_code == 'WORKFLOW_REVIEW_REQUIRED'
    assert not target.exists()
    binding = run.steps[0].escalation_result['pending_review']
    repo = VerificationEvidenceRepository(agent.home_paths.owner_home_dir)
    requirement = repo.get_requirement(run.owner_id, binding['requirement_id'])
    assert requirement is not None and requirement.input_version == binding['input_version']
    return agent, client, store, run, binding, repo, requirement, target


@pytest.mark.parametrize('mismatch', ['', 'version', 'scope', 'owner'])
def test_static_native_review_is_exact_and_resume_preserves_unstarted_attempt(wire, tmp_path, mismatch):  # noqa: F811
    agent, client, store, run, binding, repo, requirement, target = pending_review(wire, tmp_path)
    owner = 'other-owner' if mismatch == 'owner' else run.owner_id
    review = IndependentReviewRecord('review-static', binding['requirement_id'], 'independent-reviewer',
        requirement.author_principal_id, 'wrong-version' if mismatch == 'version' else binding['input_version'],
        'pass', 'ordinary-review' if mismatch == 'scope' else 'workflow_effect.v1', owner_ref=owner)
    if mismatch:
        repo.record_independent_review(VerificationContext(owner, '', run.workflow_run_id), review)
    else:
        from agent_py_agent.agent.agent_core.host_turn_scope import host_turn_scope
        from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
        from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
            write_boundary_with_runtime_ledger,
        )
        from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
        with host_turn_scope(agent, '', RunParams(run_id='independent-review', request_id='independent-review', save=False),
                reason='native_review_test', tool_rounds=1) as turn:
            name = 'manage_requirement_provenance'
            snapshot = agent.tools.runtime_snapshot(allowed_tools=[name], run_id=turn.params.run_id)
            runtime = snapshot.runtime(name)
            call = ToolCall('native-review-call', name, {'action': 'record_independent_review',
                'requirement_id': binding['requirement_id'], 'reviewed_input_version': binding['input_version'],
                'review_scope': 'workflow_effect.v1', 'review_verdict': 'pass'}, 'text', runtime.model_spec.schema_hash,
                turn.params.run_id, turn.params.request_id, turn.params.attempt_id)
            result = agent.tools.execute_tool(call, runtime_snapshot=snapshot,
                write_boundary=write_boundary_with_runtime_ledger(agent, turn.params),
                trusted_run_context={'run_scope': {'run_id': turn.params.run_id, 'task_id': turn.params.task_id,
                    'attempt_id': turn.params.attempt_id}}).result
            assert result.ok, result.output
            turn.runtime_status = 'ok'
    response = client.send(client.prepare('workflow_runs.resume', {'workflow_run_id': run.workflow_run_id},
        expected_revision=run.revision))
    current = store.load_run(run.workflow_run_id)
    assert current.steps[0].escalation_result['attempt_id'] == binding['attempt_id']
    if mismatch:
        assert current.status == 'failed' and not target.exists(), response
    else:
        assert response['ok'] and current.status == 'succeeded', response
        assert target.read_text() == 'once'
        assert len(current.steps[0].escalation_result['review_refs']) == 1


def test_native_review_rejects_self_review_in_original_ledger(wire, tmp_path):  # noqa: F811
    agent, client, store, run, binding, repo, requirement, target = pending_review(wire, tmp_path)
    with pytest.raises(SelfReviewForbiddenError):
        repo.record_independent_review(VerificationContext(run.owner_id, '', run.workflow_run_id),
            IndependentReviewRecord('self-review', binding['requirement_id'], requirement.author_principal_id,
                requirement.author_principal_id, binding['input_version'], 'pass', 'workflow_effect.v1', owner_ref=run.owner_id))
    assert not target.exists()


def test_builtin_templates_use_original_gallery_without_forcing_analysis_review(wire):  # noqa: F811
    agent, client = wire
    result = client.call('workflows.templates.list', {})
    assert result['ok'], result
    items = {row['definition']['descriptor']['id']: row for row in result['data']['items']}
    simple = items['builtin-simple-analysis']['definition']
    development = items['builtin-development-stages']['definition']
    assert 'workflow_review_policy' not in simple['descriptor']['metadata']
    assert development['descriptor']['metadata']['workflow_review_policy']['steps'] == ['review', 'regression', 'release']
    assert [step['step_id'] for step in development['steps']] == ['architecture', 'implementation', 'review', 'regression', 'release']
    assert client.call('workflows.create', {'definition': development})['ok']


def test_new_changes_requested_does_not_reuse_earlier_pass(wire, tmp_path):  # noqa: F811
    agent, client, store, run, binding, repo, requirement, target = pending_review(wire, tmp_path)
    context = VerificationContext(run.owner_id, '', run.workflow_run_id)
    for review_id, verdict in [('earlier-pass', 'pass'), ('later-request', 'changes_requested')]:
        repo.record_independent_review(context, IndependentReviewRecord(review_id, binding['requirement_id'],
            'independent-reviewer', requirement.author_principal_id, binding['input_version'], verdict,
            'workflow_effect.v1', owner_ref=run.owner_id))
    client.send(client.prepare('workflow_runs.resume', {'workflow_run_id': run.workflow_run_id}, expected_revision=run.revision))
    current = store.load_run(run.workflow_run_id)
    assert current.steps[0].error_code == 'WORKFLOW_REVIEW_CHANGES_REQUESTED'
    assert not target.exists()


def test_author_context_dies_on_exit_and_cannot_cross_threads(wire):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context

    from agent_py_agent.agent.agent_core.host_turn_scope import host_turn_scope
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.workflows.native_governance import (
        current_workflow_author,
        workflow_review_author_scope,
    )
    agent, client = wire
    with host_turn_scope(agent, '', RunParams(run_id='author-scope', request_id='author-scope', save=False),
            reason='author_scope_test', tool_rounds=0) as turn:
        with workflow_review_author_scope(agent, turn):
            assert current_workflow_author(agent) == turn.params.run_id
            copied = copy_context()
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(copied.run, current_workflow_author, agent).result() == ''
        assert copied.run(current_workflow_author, agent) == ''
        turn.runtime_status = 'ok'


def test_pending_review_cannot_be_bypassed_by_patch_output(wire, tmp_path):  # noqa: F811
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.native_governance import NativeWorkflowGovernance
    agent, client, store, run, binding, repo, requirement, target = pending_review(wire, tmp_path)
    governance = NativeWorkflowGovernance(agent.tools.operation_store, store, owner_home=agent.home_paths.owner_home_dir,
        owner_id=run.owner_id, author_provider=lambda: '')
    engine = WorkflowEngine(agent.home_paths.owner_home_dir, run_authorizer=governance.require_effect)
    with pytest.raises(PermissionError, match='WORKFLOW_REVIEW_PATCH_FORBIDDEN'):
        engine.resume_workflow_run(run.workflow_run_id, patched_step_output={'value': 7}, resolution_origin='agent')
    current = store.load_run(run.workflow_run_id)
    assert current.revision == run.revision and current.status == 'failed'
    assert not target.exists()


def test_current_definition_cannot_remove_frozen_review_policy(wire, tmp_path):  # noqa: F811
    from dataclasses import replace

    from agent_py_agent.agent.assets.workflow_store import WorkflowStore
    agent, client, store, run, binding, repo, requirement, target = pending_review(wire, tmp_path)
    assets = WorkflowStore(agent.home_paths.owner_home_dir)
    current = assets.get_workflow('native-review')
    metadata = {key: value for key, value in current.descriptor.metadata.items() if key != 'workflow_review_policy'}
    assets.save_workflow(replace(current, descriptor=replace(current.descriptor, metadata=metadata)))
    client.send(client.prepare('workflow_runs.resume', {'workflow_run_id': run.workflow_run_id}, expected_revision=run.revision))
    saved = store.load_run(run.workflow_run_id)
    assert saved.status == 'failed' and saved.steps[0].error_code == 'WORKFLOW_REVIEW_REQUIRED'
    assert not target.exists()


def test_explicit_changed_script_invalidates_static_old_review(wire, tmp_path):  # noqa: F811
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.native_governance import NativeWorkflowGovernance
    agent, client, store, run, binding, repo, requirement, target = pending_review(wire, tmp_path)
    repo.record_independent_review(VerificationContext(run.owner_id, '', run.workflow_run_id),
        IndependentReviewRecord('old-pass', binding['requirement_id'], 'reviewer', requirement.author_principal_id,
            binding['input_version'], 'pass', 'workflow_effect.v1', owner_ref=run.owner_id))
    scripts = ScriptStore(agent.home_paths.owner_home_dir)
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    scripts.save_script(AssetDescriptor(id='reviewed', kind='script', name='审核脚本'), 'print(\'{"value":8}\')')
    governance = NativeWorkflowGovernance(agent.tools.operation_store, store, owner_home=agent.home_paths.owner_home_dir,
        owner_id=run.owner_id, author_provider=lambda: '')
    engine = WorkflowEngine(agent.home_paths.owner_home_dir, run_authorizer=governance.require_effect)
    changed = engine.resume_workflow_run(run.workflow_run_id, allow_script_drift=True)
    assert changed.status == 'failed' and changed.steps[0].error_code == 'WORKFLOW_REVIEW_STALE'
    assert not target.exists()
