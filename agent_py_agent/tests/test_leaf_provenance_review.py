"""叶审核沿父精确工具和原TaskRun授权；工具名不签出绑定、验收或跨工作权限。"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.create_policy import leaf_allowed_tools
from agent_py_agent.agent.agent_core.orchestration.tools.requirement_provenance_tool import (
    RequirementProvenanceTool,
)
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.capability_scope import DIRECT_PARENT_TOOL_AUTHORITY_ATTR
from agent_py_agent.agent.subagents.services.base import CreateRunParams
from agent_py_agent.agent.verification.repository import (
    VerificationContext,
    VerificationEvidenceRepository,
)
from agent_py_agent.agent.verification.requirement_provenance import RequirementProvenanceRecord
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

NAME = 'manage_requirement_provenance'


@pytest.fixture
def leaf(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home')), tmp_path / 'project')
    owner = agent.home_paths.owner_id
    runtime = agent.subagents.runtime_db
    business = BusinessExecutionContext(owner, 'space-one', None, 'stask-original', 1, None, (), (), 'business-context.v1')
    parent = runtime.record_run_creation(owner_id=owner, conversation_task_id='original-work',
        run_id='parent-run', task_run_metadata={'business_context': business.to_dict(), 'session_task_ref': business.task_id})
    ref = runtime.parent_execution_ref(owner_id=owner, run_id='parent-run', attempt_id=parent['attempt_id'])
    task = agent.subagents.create_run(params=CreateRunParams(goal='核对准确候选', thought='review', plan=['核对', '登记'],
        role='tester', parent_id='parent-run', root_id='parent-run', owner=owner,
        allowed_tools=[NAME, 'read_file'], host_parent_execution=ref, business_context=business))
    attempt = task.attributes['runtime_authority']['attempt_id']
    repo = VerificationEvidenceRepository(agent.home_paths.owner_home_dir)
    repo.bind_requirement(VerificationContext(owner, '', business.task_id), RequirementProvenanceRecord(
        'R', owner, business.task_id, str(agent.root), 'candidate-v1', (), author_principal_id='parent-run'))
    previous = set_current_subagent_context(agent, run_id=task.id, attempt_id=attempt)
    try:
        yield SimpleNamespace(agent=agent, task=task, attempt=attempt, repo=repo, owner=owner,
            runtime=runtime, business=business, tool=RequirementProvenanceTool(agent))
    finally:
        restore_current_subagent_context(agent, previous)


def invoke(case, arguments, *, allowed=None):
    return execute_canonical_test_call(case.agent.root, tools={NAME: case.tool}, tool_name=NAME,
        arguments=arguments, run_id=case.task.id, attempt_id=case.attempt,
        allowed_tools=case.task.allowed_tools if allowed is None else allowed, operation_owner_id=case.owner,
        call_id='review-' + arguments.get('action', 'none')).result


def review_params(**updates):
    return {'action': 'record_independent_review', 'requirement_id': 'R',
        'reviewed_input_version': 'candidate-v1', 'review_verdict': 'pass', **updates}


def test_explicit_leaf_tool_survives_all_role_filters_without_parent_or_exact_escalation(leaf):
    assert NAME in leaf.task.allowed_tools
    assert NAME in leaf_allowed_tools([NAME, 'create_subagents'])
    manager = leaf.agent.subagents
    for allowed, parent in [(['read_file'], [NAME, 'read_file']), ([NAME, 'read_file'], ['read_file'])]:
        task = manager.create_run(goal='读取', thought='test', plan=['read'], role='tester', owner=leaf.owner,
            allowed_tools=allowed, attributes={DIRECT_PARENT_TOOL_AUTHORITY_ATTR: {
                'schema_version': 'direct_parent_tool_authority.v1', 'available_tool_names': parent,
                'tool_grants': parent}})
        assert NAME not in task.allowed_tools
    denied = invoke(leaf, review_params(), allowed=['read_file'])
    assert not denied.ok and not denied.handler_executed
    assert leaf.repo.list_independent_reviews(leaf.owner, requirement_id='R') == []


def test_same_work_review_records_only_real_leaf_identity_and_exact_candidate(leaf):
    result = invoke(leaf, review_params(reviewer_principal_id='parent-run', task_id='forged-root'))
    assert result.ok, result.output
    saved = leaf.repo.list_independent_reviews(leaf.owner, requirement_id='R')
    assert len(saved) == 1 and saved[0].reviewer_principal_id == leaf.task.id
    assert saved[0].author_principal_id == 'parent-run' and saved[0].reviewed_input_version == 'candidate-v1'
    assert leaf.repo.get_requirement(leaf.owner, 'R').acceptance_status == 'unaccepted'
    queried = invoke(leaf, {'action': 'query_provenance', 'requirement_id': 'R'})
    assert queried.ok and json.loads(queried.output)['records'][0]['requirement_id'] == 'R'


@pytest.mark.parametrize('action', ['bind_requirement', 'link_verification', 'record_acceptance', 'query_provenance'])
def test_leaf_tool_name_never_grants_coordinator_actions_or_owner_enumeration(leaf, action):
    result = invoke(leaf, {'action': action})
    assert not result.ok and result.effect_outcome == 'not_started'
    assert leaf.repo.get_requirement(leaf.owner, 'R').review_status == 'missing'
    assert leaf.repo.get_requirement(leaf.owner, 'R').acceptance_status == 'unaccepted'


@pytest.mark.parametrize('scope', ['version', 'other_root', 'other_owner', 'missing', 'self_review', 'old_attempt'])
def test_leaf_review_refuses_invalid_scope_without_mutating_candidate(leaf, scope):
    args = review_params()
    before = leaf.repo.get_requirement(leaf.owner, 'R')
    if scope == 'version':
        args['reviewed_input_version'] = 'wrong-version'
    elif scope == 'other_root':
        leaf.repo.bind_requirement(VerificationContext(leaf.owner, '', 'other-root'),
            replace(before, requirement_id='other', task_id='other-root'))
        args['requirement_id'] = 'other'
    elif scope == 'other_owner':
        leaf.repo.bind_requirement(VerificationContext('foreign', '', 'foreign-root'),
            replace(before, requirement_id='foreign', owner_ref='foreign', task_id='foreign-root'))
        args['requirement_id'] = 'foreign'
    elif scope == 'missing':
        args['requirement_id'] = 'not-bound'
    elif scope == 'self_review':
        leaf.repo.bind_requirement(VerificationContext(leaf.owner, '', leaf.business.task_id),
            replace(before, requirement_id='self', author_principal_id=leaf.task.id))
        args['requirement_id'] = 'self'
    else:
        leaf.runtime.create_attempt(leaf.task.attributes['runtime_authority']['agent_run_id'])
    result = invoke(leaf, args)
    assert not result.ok and result.effect_outcome == 'not_started', result.output
    assert leaf.repo.get_requirement(leaf.owner, 'R') == before
    assert leaf.repo.list_independent_reviews(leaf.owner) == []


def test_leaf_workflow_review_uses_original_pending_candidate_and_business_root(leaf, tmp_path):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.native_governance import NativeWorkflowGovernance
    from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
    from agent_py_agent.tests.test_wf_07_08_foreach_and_dag import _flow

    store = WorkflowRunStore(leaf.agent.home_paths.owner_home_dir)
    governance = NativeWorkflowGovernance(None, store, owner_home=store.owner_home,
        owner_id=leaf.owner, author_provider=lambda: 'parent-run')
    engine = WorkflowEngine(store.owner_home, analysis_handler=lambda *args: pytest.fail('审核前不能执行'),
        run_authorizer=governance.require_effect)
    definition = _flow([{'step_id': 'reviewed', 'mode': 'llm_skill_analysis'}], descriptor={
        'metadata': {'workflow_review_policy': {'steps': ['reviewed']}}})
    definition['descriptor']['owner_id'] = leaf.owner
    run = engine.run_workflow(definition, owner_id=leaf.owner, business_context=leaf.business)
    binding = run.steps[0].escalation_result['pending_review']
    wrong_scope = invoke(leaf, review_params(requirement_id=binding['requirement_id'],
        reviewed_input_version=binding['input_version'], review_scope='ordinary-review'))
    assert not wrong_scope.ok and wrong_scope.effect_outcome == 'not_started'
    assert leaf.repo.list_independent_reviews(leaf.owner) == []
    result = invoke(leaf, review_params(requirement_id=binding['requirement_id'],
        reviewed_input_version=binding['input_version'], review_scope='workflow_effect.v1'))
    assert result.ok, result.output
    saved = leaf.repo.list_independent_reviews(leaf.owner, requirement_id=binding['requirement_id'])
    assert saved[0].reviewer_principal_id == leaf.task.id
    assert saved[0].reviewed_input_version == binding['input_version']
    assert store.load_run(run.workflow_run_id).status == run.status  # 登记审核不自动验收、恢复或执行。
    assert run.steps[0].error_code == 'WORKFLOW_REVIEW_REQUIRED'
    other = engine.run_workflow(definition, owner_id=leaf.owner,
        business_context=replace(leaf.business, space_id='other-space'))
    denied_binding = other.steps[0].escalation_result['pending_review']
    denied = invoke(leaf, review_params(requirement_id=denied_binding['requirement_id'],
        reviewed_input_version=denied_binding['input_version'], review_scope='workflow_effect.v1'))
    assert not denied.ok and denied.effect_outcome == 'not_started'
    assert leaf.repo.list_independent_reviews(leaf.owner, requirement_id=denied_binding['requirement_id']) == []


def test_direct_and_scheduled_explicit_review_grants_share_parent_and_exact_intersection(leaf):
    from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
    from agent_py_agent.agent.subagents.services.hierarchy.tool_policy import (
        ToolPolicyRequest,
        scheduled_child_tools,
    )

    requested = {'role': 'tester', '_exact_allowed_tools': True,
        'delegation_constraints': {'permitted_tools': [NAME, 'read_file']}}
    # 原父main入口派工；此fixture的叶不拥有一个可重新签发派工的SessionTask。
    previous = set_current_subagent_context(leaf.agent, run_id='', attempt_id='')
    try:
        direct = create_run_params(leaf.agent, requested, '独立核对', ['read_file'])
        direct_without = create_run_params(leaf.agent, {'role': 'tester', '_exact_allowed_tools': True},
            '读取', ['read_file'])
    finally:
        restore_current_subagent_context(leaf.agent, previous)
    assert NAME in direct.allowed_tools
    spec = SimpleNamespace(role='tester', allowed_tools=[NAME, 'read_file'], attributes={
        'delegation_constraints': {'permitted_tools': [NAME, 'read_file']}})
    scheduled = scheduled_child_tools(ToolPolicyRequest(parent_tools=[NAME, 'read_file'],
        spec=spec, extra_write_roots=[], role='tester'))
    assert NAME in scheduled
    with pytest.raises(PermissionError, match='exceeds parent authority'):
        scheduled_child_tools(ToolPolicyRequest(parent_tools=['read_file'], spec=spec,
            extra_write_roots=[], role='tester'))
    assert NAME not in direct_without.allowed_tools


@pytest.mark.parametrize('action', ['record_independent_review', 'query_provenance'])
def test_leaf_candidate_rebind_between_scope_check_and_repository_action_is_refused(leaf, monkeypatch, action):
    before = leaf.repo.get_requirement(leaf.owner, 'R')
    original_execute = leaf.tool.execute

    def rebind_then_execute(params, **kwargs):
        leaf.repo.bind_requirement(VerificationContext(leaf.owner, '', 'outside-work'),
            replace(before, task_id='outside-work', root='/outside-private-root',
                input_version='outside-private-version', author_principal_id='outside-private-author'))
        return original_execute(params, **kwargs)

    monkeypatch.setattr(leaf.tool, 'execute', rebind_then_execute)
    params = review_params() if action == 'record_independent_review' else {'action': action, 'requirement_id': 'R'}
    result = invoke(leaf, params)
    assert not result.ok and result.effect_outcome == 'not_started', result.output
    assert 'outside-private' not in result.output
    assert leaf.repo.list_independent_reviews(leaf.owner) == []


def test_leaf_generated_review_id_collision_cannot_modify_another_reviewer_receipt(leaf, monkeypatch):
    from agent_py_agent.agent.verification import repository
    from agent_py_agent.agent.verification.requirement_provenance import IndependentReviewRecord

    stamp = '2026-11-13T00:00:00+00:00'
    original = leaf.repo.record_independent_review(VerificationContext(leaf.owner, '', leaf.business.task_id),
        IndependentReviewRecord('rev-' + stamp, 'R', 'another-real-reviewer', 'parent-run',
            'candidate-v1', 'changes_requested', 'original-scope', ('original-evidence',), owner_ref=leaf.owner))
    monkeypatch.setattr(repository, '_utc_now', lambda: stamp)
    result = invoke(leaf, review_params(review_scope='new-scope', evidence_refs=['new-evidence']))
    assert not result.ok and result.effect_outcome == 'not_started', result.output
    assert leaf.repo.list_independent_reviews(leaf.owner) == [original]


def test_leaf_review_success_returns_committed_snapshot_if_binding_changes_after_commit(leaf, monkeypatch):
    original = VerificationEvidenceRepository.record_independent_review
    before = leaf.repo.get_requirement(leaf.owner, 'R')

    def commit_then_rebind(repo, context, record, **kwargs):
        saved = original(repo, context, record, **kwargs)
        leaf.repo.bind_requirement(VerificationContext(leaf.owner, '', 'outside-work'),
            replace(before, task_id='outside-work', root='/outside-private-root',
                input_version='outside-private-version', author_principal_id='outside-private-author'))
        return saved

    monkeypatch.setattr(VerificationEvidenceRepository, 'record_independent_review', commit_then_rebind)
    result = invoke(leaf, review_params())
    assert result.ok and result.handler_executed, result.output
    assert 'outside-private' not in result.output
    payload = json.loads(result.output)
    assert payload['requirement']['task_id'] == leaf.business.task_id
    assert payload['requirement']['review_status'] == 'approved'
    assert len(leaf.repo.list_independent_reviews(leaf.owner)) == 1
    assert leaf.repo.get_requirement(leaf.owner, 'R').task_id == 'outside-work'


def test_leaf_query_keeps_one_sql_snapshot_across_canonical_rebinding(leaf, monkeypatch):
    from agent_py_agent.agent.verification import repository
    from agent_py_agent.agent.verification.requirement_provenance import IndependentReviewRecord

    original = repository._require_expected_requirement

    def rebind_after_first_read(conn, context, expected):
        snapshot = original(conn, context, expected)
        leaf.repo.bind_requirement(VerificationContext(leaf.owner, '', 'outside-work'),
            replace(expected, task_id='outside-work', root='/outside-private-root',
                input_version='outside-private-version', author_principal_id='outside-private-author'))
        leaf.repo.record_independent_review(VerificationContext(leaf.owner, '', 'outside-work'),
            IndependentReviewRecord('outside-receipt', 'R', 'outside-private-reviewer', 'outside-private-author',
                'outside-private-version', 'pass', 'outside-private-scope', owner_ref=leaf.owner))
        return snapshot

    monkeypatch.setattr(repository, '_require_expected_requirement', rebind_after_first_read)
    result = invoke(leaf, {'action': 'query_provenance', 'requirement_id': 'R'})
    assert result.ok, result.output
    assert 'outside-private' not in result.output
    assert json.loads(result.output)['reviews'] == []
    assert leaf.repo.get_requirement(leaf.owner, 'R').task_id == 'outside-work'


@pytest.mark.parametrize('version', ['candidate-v1', 'guessed-private-version'])
def test_outside_work_is_denied_before_private_version_or_author_comparison(leaf, version):
    before = leaf.repo.get_requirement(leaf.owner, 'R')
    leaf.repo.bind_requirement(VerificationContext(leaf.owner, '', 'outside-work'),
        replace(before, requirement_id='outside', task_id='outside-work',
            input_version='candidate-v1', author_principal_id='outside-private-author'))
    result = invoke(leaf, review_params(requirement_id='outside', reviewed_input_version=version))
    assert not result.ok and result.effect_outcome == 'not_started'
    payload = json.loads(result.output)
    assert payload['reason'] == 'REVIEW_TASK_SCOPE_MISMATCH'
    assert 'outside-private' not in result.output


def test_guarded_receipt_replay_is_immutable_but_main_updates_keep_original_contract(leaf):
    from agent_py_agent.agent.verification.repository import ProvenanceAuthorizationError
    from agent_py_agent.agent.verification.requirement_provenance import IndependentReviewRecord

    expected = leaf.repo.get_requirement(leaf.owner, 'R')
    context = VerificationContext(leaf.owner, '', leaf.task.id)
    record = IndependentReviewRecord('leaf-fixed-id', 'R', leaf.task.id, 'parent-run',
        'candidate-v1', 'pass', 'independent_review', ('proof-one',), owner_ref=leaf.owner)
    first, requirement = leaf.repo.record_independent_review(context, record,
        expected_requirement=expected, include_requirement=True)
    replay, snapshot = leaf.repo.record_independent_review(context, record,
        expected_requirement=expected, include_requirement=True)
    assert replay == first and snapshot == requirement
    with pytest.raises(ProvenanceAuthorizationError, match='不可篡改'):
        leaf.repo.record_independent_review(context, replace(record, evidence_refs=('changed',)),
            expected_requirement=expected)
    assert leaf.repo.list_independent_reviews(leaf.owner) == [first]
    main = VerificationContext(leaf.owner, '', 'main-owner')
    host = replace(record, review_id='host-owned-id', reviewer_principal_id='main-owner')
    leaf.repo.record_independent_review(main, host)
    changed = leaf.repo.record_independent_review(main, replace(host, verdict='changes_requested',
        scope='updated-host-scope', evidence_refs=('updated-host-proof',)))
    assert changed.verdict == 'changes_requested' and changed.scope == 'updated-host-scope'
    assert next(r for r in leaf.repo.list_independent_reviews(leaf.owner) if r.review_id == host.review_id).evidence_refs == ('updated-host-proof',)


def test_leaf_review_write_transaction_blocks_competing_sql_rebind(leaf, monkeypatch):
    import sqlite3

    import agent_py_agent.agent.verification.repository as module

    original = module._require_expected_requirement
    blocked = []

    def check_with_competing_writer(conn, context, expected):
        result = original(conn, context, expected)
        # 另一个物理连接模拟其它进程；不能借模块内RLock伪造写互斥证明。
        competing = sqlite3.connect(leaf.repo.db_path, timeout=0.01)
        try:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                competing.execute('UPDATE requirement_links SET task_id=? WHERE owner_ref=? AND requirement_id=?',
                    ('outside-work', leaf.owner, 'R'))
            blocked.append(True)
        finally:
            competing.close()
        return result

    monkeypatch.setattr(module, '_require_expected_requirement', check_with_competing_writer)
    result = invoke(leaf, review_params())
    assert result.ok and result.handler_executed, result.output
    assert blocked == [True]
    assert leaf.repo.get_requirement(leaf.owner, 'R').task_id == leaf.business.task_id
    assert leaf.repo.list_independent_reviews(leaf.owner, requirement_id='R')[0].reviewer_principal_id == leaf.task.id
