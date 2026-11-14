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
from agent_py_agent.tests.test_acceptance_workflow_native_ports import (
    host as raw_host,  # noqa: F401
)


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


def test_pending_review_reports_actual_error_without_blind_retry(wire, tmp_path):  # noqa: F811
    from agent_py_agent.agent.agent_core.host_turn_scope import host_turn_scope
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams

    agent, client, _, run, _, _, _, target = pending_review(wire, tmp_path)
    params = RunParams(run_id='review-wait', request_id='review-wait', save=False)
    with host_turn_scope(agent, '', params, reason='workflow_pending_diagnostic', tool_rounds=1):
        outcome = agent.tools.tools['workflows'].execute({'action': 'resume', 'workflow_run_id': run.workflow_run_id})
    assert not outcome.ok and outcome.error_code == 'WORKFLOW_REVIEW_REQUIRED', outcome.output
    assert not outcome.retryable and outcome.recommended_action == 'request_approval'
    assert 'WORKFLOW_REVIEW_REQUIRED' in outcome.result_envelope['display']['summary']
    assert not target.exists()
    public = client.call('operations.get', {'operation_id': run.workflow_run_id})
    assert public['ok'], public
    error = public['data']['error']
    assert error['code'] == outcome.error_code and error['retryable'] is False
    assert '独立审核' in error['message']


def test_historical_review_error_needs_exact_attempt_and_preserves_unknown_effects():
    from agent_py_agent.agent.gateway_parts.business_api_v13_readers import _observed_tool_error

    code = 'WORKFLOW_REVIEW_REQUIRED'
    step = {'step_id': 'release', 'error_code': code, 'escalation_result': {
        'attempt_id': 'original', 'pending_review': {'schema': 'workflow.pending-review.v1',
            'workflow_run_id': 'run', 'step_id': 'release', 'attempt_id': 'obsolete'}}}
    projection = _observed_tool_error('TOOL_EXECUTION_FAILED',
        result={'effect_outcome': 'unknown', 'retryable': True},
        workflow={'workflow_run_id': 'run', 'failed_step_id': 'release', 'status': 'failed', 'steps': [step]})
    assert projection['code'] == 'TOOL_EXECUTION_FAILED' and projection['retryable'] is False
    assert _observed_tool_error('TOOL_OPERATION_OUTCOME_UNKNOWN',
        result={'reported_error_code': code, 'effect_outcome': 'unknown'})['code'] == 'TOOL_OPERATION_OUTCOME_UNKNOWN'


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


@pytest.mark.parametrize('declared_effect', ['failed', 'unknown'])
def test_workflow_unknown_step_projects_reconciliation_without_review_dispatch(declared_effect):
    from agent_py_agent.agent.tooling.scripts_tool import _outcome
    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool

    tool = WorkflowsTool.__new__(WorkflowsTool)
    tool.run_params_provider = None
    tool._execute = lambda params: _outcome('workflows', {
        'workflow_run_id': 'original-run', 'state': 'failed', 'effect_outcome': declared_effect,
        'steps': [{'step_id': 'issue', 'error_code': 'SCRIPT_TIMEOUT', 'escalation_result': {
            'effect_started': True, 'effect_outcome': 'unknown', 'attempt_id': 'original-attempt'}}]},
        False, 'TOOL_EXECUTION_FAILED')
    dispatches = []
    tool.review_dispatch = lambda *args: dispatches.append(args)
    result = tool.execute_scoped({'action': 'run'}, object())
    assert result.error_code == 'WORKFLOW_RECONCILIATION_REQUIRED'
    assert result.effect_outcome == 'unknown' and result.retryable is False
    assert result.result_envelope['steps'][0]['error_code'] == 'SCRIPT_TIMEOUT'
    assert '不要原样重试' in result.recovery_hint
    assert dispatches == []
    query = result.result_envelope['recovery_query']
    assert query['parameters'] == {'action': 'inspect_run', 'workflow_run_id': 'original-run'}
    assert query['requires_new_user_message'] and query['read_only'] and not query['resume_permitted']
    assert '另发一条消息' in query['instruction']


def test_workflow_unknown_without_original_id_does_not_invent_query():
    from agent_py_agent.agent.tooling.scripts_tool import _outcome
    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool

    tool = WorkflowsTool.__new__(WorkflowsTool)
    tool.run_params_provider = None
    tool._execute = lambda params: _outcome('workflows', {'effect_outcome': 'unknown'},
        False, 'TOOL_EXECUTION_FAILED')
    tool.review_dispatch = None
    result = tool.execute_scoped({'action': 'run'}, None)
    assert result.error_code == 'WORKFLOW_RECONCILIATION_REQUIRED'
    assert 'recovery_query' not in result.result_envelope


@pytest.mark.parametrize('dag', [False, True])
def test_native_reviewed_analysis_keeps_author_and_blocks_worker_effect(wire, dag):  # noqa: F811
    """并行worker仍从原宿主执行事实绑定作者，缺票不能进入实际分析。"""
    agent, client = wire
    calls = []
    agent.backend.generate_structured = lambda *args, **kwargs: calls.append(args)
    definition = flow('reviewed-analysis-ports', [
        {'step_id': sid, 'name': sid, 'mode': 'llm_skill_analysis', 'on_error': 'fail'}
        for sid in ('left', 'right')], dag=dag)
    definition['descriptor']['metadata'].update({
        'workflow_review_policy': {'steps': ['left', 'right'], 'minimum_reviews': 1},
        'max_parallel_steps': 2})
    assert client.call('workflows.create', {'definition': definition})['ok']
    outcome = client.call('workflows.run', {'workflow_ref': 'reviewed-analysis-ports'})
    run = WorkflowRunStore(agent.home_paths.owner_home_dir).load_run(outcome['data']['workflow_run_id'])
    assert run.status == 'failed' and calls == []
    pending = [step for step in run.steps if step.error_code == 'WORKFLOW_REVIEW_REQUIRED']
    assert pending, run.to_dict()
    repo = VerificationEvidenceRepository(agent.home_paths.owner_home_dir)
    for step in pending:
        binding = step.escalation_result['pending_review']
        requirement = repo.get_requirement(run.owner_id, binding['requirement_id'])
        assert requirement.author_principal_id == run.workflow_run_id
        assert requirement.input_version == binding['input_version']
        assert repo.list_independent_reviews(run.owner_id, requirement_id=binding['requirement_id']) == []


def test_native_author_invoker_uses_live_original_scope_and_canonical_attempt(wire, monkeypatch):  # noqa: F811
    import copy
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.agent_core.host_turn_scope import host_turn_scope
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.workflows.native_governance import (
        current_workflow_author,
        workflow_review_author_scope,
    )

    agent, _ = wire
    from types import SimpleNamespace

    record = SimpleNamespace(workflow_run_id='author-worker', owner_id=agent.home_paths.owner_id)
    params = RunParams(run_id='author-worker', request_id='author-worker', save=False)
    with host_turn_scope(agent, '', params, reason='author_worker_test', tool_rounds=0) as turn:
        with pytest.raises(ValueError, match='原宿主执行轮'):
            with workflow_review_author_scope(agent, copy.copy(turn)):
                pytest.fail('复制宿主句柄不能签出作者')
        with pytest.raises(ValueError, match='原宿主执行轮'):
            turn.require(agent, copy.copy(turn.params))
        with ThreadPoolExecutor(max_workers=1) as pool:
            with workflow_review_author_scope(agent, turn,
                    authorize=lambda record, phase: current_workflow_author(agent),
                    workflow_run_id=record.workflow_run_id) as invoke:
                assert pool.submit(invoke, record, 'preflight').result() == turn.params.run_id
                wrong = copy.copy(record)
                wrong.workflow_run_id = 'another-workflow-run'
                with pytest.raises(PermissionError, match='WORKFLOW_REVIEW_AUTHORIZER_RUN_MISMATCH'):
                    invoke(wrong, 'preflight')
                wrong.trigger_kind = 'subworkflow'
                wrong.parent_workflow_run_id = record.workflow_run_id
                with pytest.raises(PermissionError, match='WORKFLOW_REVIEW_AUTHORIZER_RUN_MISMATCH'):
                    invoke(wrong, 'preflight')
                with pytest.raises(TypeError):
                    invoke(lambda: pytest.fail('任意函数不能借审核身份'))
                with monkeypatch.context() as patch:
                    patch.setattr('agent_py_agent.agent.workflows.native_governance.os.getpid', lambda: -1)
                    with pytest.raises(ValueError, match='原宿主执行轮'):
                        invoke(record, 'preflight')
                # 原canonical门拒绝时不能凭已签闭包继续；不替该门补发attempt。
                def reject(*args, **kwargs):
                    raise PermissionError('TEST_CANONICAL_ATTEMPT_REVOKED')
                with monkeypatch.context() as patch:
                    patch.setattr(agent.tools.operation_store, 'require_authority', reject)
                    with pytest.raises(PermissionError, match='TEST_CANONICAL_ATTEMPT_REVOKED'):
                        pool.submit(invoke, record, 'preflight').result()
            with pytest.raises(ValueError, match='原宿主执行轮'):
                pool.submit(invoke, record, 'preflight').result()
        turn.runtime_status = 'ok'
    with pytest.raises(ValueError, match='原宿主执行轮'):
        invoke(record, 'preflight')


def test_native_author_worker_loses_scope_when_original_stack_exits(wire):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from agent_py_agent.agent.agent_core.host_turn_scope import host_turn_scope
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.workflows.native_governance import (
        current_workflow_author,
        workflow_review_author_scope,
    )

    agent, _ = wire
    entered, released = Event(), Event()

    def observe_after_exit(record, phase):
        entered.set()
        assert released.wait(5)
        return current_workflow_author(agent)

    with host_turn_scope(agent, '', RunParams(run_id='author-source-exit', request_id='author-source-exit', save=False),
            reason='author_source_exit_test', tool_rounds=0) as turn:
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                from types import SimpleNamespace
                record = SimpleNamespace(workflow_run_id=turn.params.run_id, owner_id=agent.home_paths.owner_id)
                with workflow_review_author_scope(agent, turn, authorize=observe_after_exit,
                        workflow_run_id=record.workflow_run_id) as invoke:
                    future = pool.submit(invoke, record, 'preflight')
                    assert entered.wait(5)
            finally:
                released.set()
            assert future.result() == ''
        turn.runtime_status = 'ok'


@pytest.mark.parametrize('reviewed', [False, True])
def test_native_reviewed_subworkflow_binds_own_original_run(wire, reviewed):  # noqa: F811
    agent, client = wire
    from types import SimpleNamespace
    agent.backend.generate_structured = (lambda *args, **kwargs: pytest.fail('无票不得分析')) if reviewed else (
        lambda *args, **kwargs: SimpleNamespace(text='{"value":7}'))
    child = flow('reviewed-nested-child', [{'step_id': 'analysis', 'name': 'analysis',
        'mode': 'llm_skill_analysis', 'on_error': 'fail'}])
    if reviewed:
        child['descriptor']['metadata']['workflow_review_policy'] = {'steps': ['analysis']}
    parent = flow('reviewed-nested-parent', [{'step_id': 'nested', 'name': 'nested',
        'mode': 'subworkflow', 'on_error': 'fail', 'subworkflow_ref': 'workflow:reviewed-nested-child'}])
    for definition in (child, parent):
        assert client.call('workflows.create', {'definition': definition})['ok']
    outcome = client.call('workflows.run', {'workflow_ref': 'reviewed-nested-parent'})
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    runs = store.list_runs(limit=10)
    nested = next(run for run in runs if run.workflow_id == 'reviewed-nested-child')
    assert nested.parent_workflow_run_id == outcome['data']['workflow_run_id']
    if not reviewed:
        assert nested.status == 'succeeded'
        assert store.load_run(outcome['data']['workflow_run_id']).status == 'succeeded'
        return
    step = nested.steps[0]
    assert step.error_code == 'WORKFLOW_REVIEW_REQUIRED', nested.to_dict()
    binding = step.escalation_result['pending_review']
    requirement = VerificationEvidenceRepository(agent.home_paths.owner_home_dir).get_requirement(
        nested.owner_id, binding['requirement_id'])
    assert requirement.author_principal_id == nested.workflow_run_id


def test_native_authorizer_rejects_other_real_run_and_cannot_reopen_after_close(wire, tmp_path, monkeypatch):  # noqa: F811
    from contextlib import contextmanager

    import agent_py_agent.agent.workflows.native_governance as module
    from agent_py_agent.agent.workflows.governance import WorkflowEffectPhase

    agent, client, store, other, _, repo, original_requirement, target = pending_review(wire, tmp_path)
    original = module.workflow_review_author_scope
    captured = []

    @contextmanager
    def observe(host, turn, **kwargs):
        with original(host, turn, **kwargs) as authorize:
            if authorize is not None:
                captured.append(authorize)
                with pytest.raises(PermissionError, match='WORKFLOW_REVIEW_AUTHORIZER_RUN_MISMATCH'):
                    authorize(other, WorkflowEffectPhase('effect'))
            yield authorize

    monkeypatch.setattr(module, 'workflow_review_author_scope', observe)
    result = client.call('workflows.run', {'workflow_ref': 'native-review'})
    current = store.load_run(result['data']['workflow_run_id'])
    assert current.workflow_run_id != other.workflow_run_id
    assert current.steps[0].error_code == 'WORKFLOW_REVIEW_REQUIRED'
    assert not target.exists() and captured
    binding = current.steps[0].escalation_result['pending_review']
    own = repo.get_requirement(current.owner_id, binding['requirement_id'])
    assert own.author_principal_id == current.workflow_run_id
    assert repo.get_requirement(other.owner_id, original_requirement.requirement_id) == original_requirement
    for authorize in captured:
        with pytest.raises(ValueError, match='原宿主执行轮'):
            authorize(current, WorkflowEffectPhase('effect'))


@pytest.mark.parametrize('mode,dag', [('deterministic_script', False), ('tool_call', False), ('tool_call', True)])
def test_raw_owner_global_declared_review_cannot_use_bare_engine(raw_host, tmp_path, mode, dag):  # noqa: F811
    host = raw_host
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.assets.workflow_store import WorkflowStore

    target = host.effective_workspace_root / 'raw-review-effect.txt'
    target.parent.mkdir(parents=True, exist_ok=True)
    ScriptStore(host.home_paths.owner_home_dir).save_script(
        AssetDescriptor(id='raw-reviewed-effect', kind='script', name='原门后写'),
        f'from pathlib import Path\nPath({str(target)!r}).write_text("once")\nprint(\'{{"value":1}}\')')
    spec = {'step_id': 'effect', 'name': 'effect', 'mode': mode, 'on_error': 'fail'}
    if mode == 'deterministic_script':
        spec['script_id'] = 'raw-reviewed-effect'
    else:
        spec['tool_spec'] = {'tool_name': 'read_file', 'arguments': {'path': str(target)}}
    definition = flow('raw-owner-review', [spec], dag=dag)
    definition['descriptor']['metadata']['workflow_review_policy'] = {'steps': ['effect']}
    WorkflowStore(host.home_paths.owner_home_dir).save_workflow(definition)
    tool = host.tools.tools['workflows']
    result = tool.execute({'action': 'run', 'workflow_id': 'raw-owner-review'})
    assert not result.ok and result.error_code == 'WORKFLOW_REVIEW_REQUIRED', result.output
    [run] = WorkflowRunStore(host.home_paths.owner_home_dir).list_runs(limit=10)
    assert run.business_context == {} and run.scope_snapshot == {}
    binding = run.steps[0].escalation_result['pending_review']
    assert binding['step_id'] == 'effect' and not target.exists()
    again = tool.execute({'action': 'resume', 'workflow_run_id': run.workflow_run_id})
    assert not again.ok and again.error_code == 'WORKFLOW_REVIEW_REQUIRED', again.output
    current = WorkflowRunStore(host.home_paths.owner_home_dir).load_run(run.workflow_run_id)
    assert current.steps[0].escalation_result['pending_review'] == binding and not target.exists()


def test_declared_review_without_host_authorizer_refuses_before_any_effect(tmp_path):
    from agent_py_agent.agent.workflows.business_scope import WorkflowPolicyRefusal
    from agent_py_agent.agent.workflows.engine import WorkflowEngine

    definition = flow('no-review-host', [{'step_id': 'effect', 'name': 'effect', 'mode': 'tool_call',
        'on_error': 'fail', 'tool_spec': {'tool_name': 'write_file', 'arguments': {'path': 'ignored'}}}])
    definition['descriptor']['metadata']['workflow_review_policy'] = {'steps': ['effect']}
    calls = []
    engine = WorkflowEngine(tmp_path, tool_step_executor=lambda *args: calls.append(args))
    with pytest.raises(WorkflowPolicyRefusal) as failure:
        engine.run_workflow(definition)
    assert failure.value.error_code == 'WORKFLOW_REVIEW_SERVICE_UNAVAILABLE'
    assert failure.value.details['effect_outcome'] == 'not_started' and calls == []
    assert engine.store.list_runs(limit=10) == []


def test_owner_global_plain_still_runs_but_review_service_absence_refuses(raw_host, tmp_path):  # noqa: F811
    from agent_py_agent.agent.assets.workflow_store import WorkflowStore

    host = raw_host
    path = host.effective_workspace_root / 'plain-source.txt'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('plain global still works')
    spec = {'step_id': 'read', 'name': 'read', 'mode': 'tool_call', 'on_error': 'fail',
        'tool_spec': {'tool_name': 'read_file', 'arguments': {'path': str(path)}}}
    store = WorkflowStore(host.home_paths.owner_home_dir)
    store.save_workflow(flow('plain-global', [spec]))
    tool = host.tools.tools['workflows']
    original_governance = tool.review_governance_provider
    tool.review_governance_provider = lambda: None
    try:
        plain = tool.execute({'action': 'run', 'workflow_id': 'plain-global'})
        assert plain.ok, plain.output
        reviewed = flow('missing-review-service', [spec])
        reviewed['descriptor']['metadata']['workflow_review_policy'] = {'steps': ['read']}
        store.save_workflow(reviewed)
        refused = tool.execute({'action': 'run', 'workflow_id': 'missing-review-service'})
        assert not refused.ok and refused.error_code == 'WORKFLOW_REVIEW_SERVICE_UNAVAILABLE', refused.output
        assert refused.effect_outcome == 'not_started' and refused.retryable is False
        runs = WorkflowRunStore(host.home_paths.owner_home_dir).list_runs(limit=10)
        assert len(runs) == 1 and runs[0].workflow_id == 'plain-global'
    finally:
        tool.review_governance_provider = original_governance


@pytest.mark.parametrize('policy', [None, {'steps': []}])
def test_optional_or_empty_review_policy_needs_no_review_service(tmp_path, policy):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.models import WorkflowRunRecord

    engine = WorkflowEngine(tmp_path)
    run = WorkflowRunRecord('qa-empty-policy', 'qa-global', '', 'manual', 'running', 1, 0, 0)
    engine._authorize_run(run, 'preflight', definition={'descriptor': {'metadata': {'workflow_review_policy': policy}}})
    assert run._declared_review_required is False


@pytest.mark.parametrize('policy', [{}, {'steps': 'effect'}, {'steps': [], 'minimum_reviews': 0}])
def test_invalid_declared_review_policy_is_not_silently_absent(tmp_path, policy):
    from agent_py_agent.agent.workflows.business_scope import WorkflowPolicyRefusal
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.models import WorkflowRunRecord

    engine = WorkflowEngine(tmp_path)
    run = WorkflowRunRecord('qa-invalid-policy', 'qa-global', '', 'manual', 'running', 1, 0, 0)
    with pytest.raises(WorkflowPolicyRefusal, match='WORKFLOW_REVIEW_POLICY_INVALID'):
        engine._authorize_run(run, 'preflight', definition={'descriptor': {'metadata': {'workflow_review_policy': policy}}})


def test_raw_declared_review_dispatch_uses_original_host_without_business_dto(raw_host):  # noqa: F811
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.workflow_review_dispatch import dispatch_pending_review
    from agent_py_agent.agent.assets.workflow_store import WorkflowStore

    host = raw_host
    definition = flow('raw-dispatch', [{'step_id': 'effect', 'name': 'effect', 'mode': 'tool_call',
        'on_error': 'fail', 'tool_spec': {'tool_name': 'read_file', 'arguments': {'path': 'unused'}}}])
    definition['descriptor']['metadata']['workflow_review_policy'] = {'steps': ['effect']}
    WorkflowStore(host.home_paths.owner_home_dir).save_workflow(definition)
    result = host.tools.tools['workflows'].execute({'action': 'run', 'workflow_id': 'raw-dispatch'})
    assert not result.ok and result.error_code == 'WORKFLOW_REVIEW_REQUIRED'
    [run] = WorkflowRunStore(host.home_paths.owner_home_dir).list_runs(limit=10)
    params = host._current_run_params
    assert params.business_context is None and run.business_context == {}
    snapshot = host.tools.runtime_snapshot(allowed_tools=['workflows'], run_id=params.run_id)
    receipt = dispatch_pending_review(host, run.workflow_run_id, SimpleNamespace(runtime_snapshot=snapshot))
    assert receipt['state'] == 'not_started' and receipt['code'] == 'REVIEW_DISPATCH_TOOLS_UNAVAILABLE', receipt
    assert set(receipt['missing_tools']) == {'create_subagents', 'manage_requirement_provenance'}


def test_raw_review_does_not_require_enabling_business_spaces(raw_host):  # noqa: F811
    from agent_py_agent.agent.assets.workflow_store import WorkflowStore

    host = raw_host
    host.config.business_spaces_enabled = False
    definition = flow('raw-without-space-feature', [{'step_id': 'effect', 'name': 'effect',
        'mode': 'tool_call', 'on_error': 'fail', 'tool_spec': {'tool_name': 'read_file', 'arguments': {'path': 'unused'}}}])
    definition['descriptor']['metadata']['workflow_review_policy'] = {'steps': ['effect']}
    WorkflowStore(host.home_paths.owner_home_dir).save_workflow(definition)
    outcome = host.tools.tools['workflows'].execute({'action': 'run', 'workflow_id': 'raw-without-space-feature'})
    assert not outcome.ok and outcome.error_code == 'WORKFLOW_REVIEW_REQUIRED', outcome.output
    [run] = WorkflowRunStore(host.home_paths.owner_home_dir).list_runs(limit=10)
    assert run.business_context == {} and run.scope_snapshot == {}
    assert run.steps[0].escalation_result['effect_started'] is False
