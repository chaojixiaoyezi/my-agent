"""进展只读聚合复用原验证/交接账；不把同owner别的任务或正文当进展。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.coordination_progress import (
    coordination_source_facts,
)
from agent_py_agent.agent.agent_core.orchestration.progress_watch import classify_node_progress
from agent_py_agent.agent.collaboration.models import CaseDecision, HandoffBoardItem
from agent_py_agent.agent.collaboration.store import CollaborationStore
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.verification.repository import (
    VerificationContext,
    VerificationEvidence,
    VerificationEvidenceRepository,
)
from agent_py_agent.agent.verification.requirement_provenance import (
    IndependentReviewRecord,
    RequirementProvenanceRecord,
)


@pytest.fixture
def source(tmp_path):
    conversations = ConversationStore(tmp_path / 'conversations')
    thread = conversations.threads.get_or_create({'canonical_user_id': 'owner', 'channel': 'local',
        'channel_conversation_id': 'c', 'channel_user_id': 'owner'})
    collaboration = CollaborationStore(tmp_path / 'collaboration')
    task = SimpleNamespace(id='run-A', owner='owner', root_id='root-A', attributes={
        'conversation_thread_id': thread.thread_id, 'conversation_task_id': 'root-A'})
    agent = SimpleNamespace(root=tmp_path, home_paths=SimpleNamespace(owner_id='owner', owner_home_dir=tmp_path),
        subagents=SimpleNamespace(load=lambda run: task), collaboration_store=collaboration,
        conversation_store=conversations)
    return agent, task, thread.thread_id


def _publish(source, *, root='root-A', actor='run-A'):
    agent, _, thread = source
    case = agent.collaboration_store.open_case({'thread_id': thread, 'task_id': root, 'created_by': actor})
    item = HandoffBoardItem('item-A', case.case_id, 'request-A', actor, 'internal_run', 'v1', (), 'published')
    agent.collaboration_store.append_handoff_item(item)
    agent.collaboration_store.append_decision(CaseDecision('decision-A', case.case_id, 'review', summary='PRIVATE_TEXT'))
    agent.conversation_store.observations.append({'thread_id': thread, 'root_task_id': root,
        'event_type': 'handoff_item_published', 'metadata': {'case_id': case.case_id, 'item_id': item.item_id}})
    return case, item


def test_canonical_handoff_and_case_decision_follow_latest_original_record(source):
    agent, _, _ = source
    _, item = _publish(source)
    agent.collaboration_store.append_handoff_item(replace(item, state='accepted', updated_at=123))
    facts = coordination_source_facts(agent, [{'run_id': 'run-A'}])['run-A']
    assert facts['coordination']['handoffs'][0]['state'] == 'accepted'
    assert facts['coordination']['case_decisions'][0]['records'][0]['decision_id'] == 'decision-A'
    assert 'PRIVATE_TEXT' not in str(facts)
    # 同一次源投影供分类展示，不因裁决或验收文字覆盖终态/等待协议。
    view = classify_node_progress({'status': 'DONE'}, now=1000, sources=facts)
    assert view['state'] == 'completed'
    assert view['sources']['coordination'] == facts['coordination']


@pytest.mark.parametrize('variant', ['other_task', 'other_actor', 'missing', 'truncated', 'symlink', 'hardlink', 'owner'])
def test_unproven_references_never_project_handoff(source, tmp_path, variant):
    agent, task, _ = source
    case, item = _publish(source, root='root-B' if variant == 'other_task' else 'root-A',
                          actor='run-B' if variant == 'other_actor' else 'run-A')
    path = agent.collaboration_store.root / 'handoff_items' / f'{case.case_id}.jsonl'
    if variant == 'missing':
        path.unlink()
    elif variant == 'truncated':
        with path.open('ab') as handle:
            handle.write(b'{"item_id":')
    elif variant == 'symlink':
        target = tmp_path / 'outside.jsonl'
        path.rename(target)
        path.symlink_to(target)
    elif variant == 'hardlink':
        (tmp_path / 'linked.jsonl').hardlink_to(path)
    elif variant == 'owner':
        task.owner = 'someone-else'
    fact = coordination_source_facts(agent, [{'run_id': 'run-A'}])['run-A']['coordination']
    assert fact['state'] == 'read' if variant == 'missing' else fact['state'] in {'unknown', 'unreadable'}
    assert 'handoffs' not in fact


def test_missing_database_does_not_create_state_and_exact_tests_are_scoped(source, tmp_path):
    agent, task, thread = source
    repo = VerificationEvidenceRepository(tmp_path)
    context = VerificationContext('owner', thread, task.id)
    assert repo.progress_metadata(context) == {'state': 'not_applicable'}
    assert not repo.db_path.parent.exists()
    evidence = VerificationEvidence('pytest PRIVATE', 'pytest', 'test', 'module_only', 'passed', 0,
                                    str(tmp_path), str(tmp_path), 'PRIVATE_OUTPUT')
    repo.record(context, evidence)
    repo.record(VerificationContext('owner', thread, 'run-B'), replace(evidence, status='failed'))
    fact = coordination_source_facts(agent, [{'run_id': task.id}])[task.id]['verification']
    assert len(fact['tests']) == 1 and fact['tests'][0]['status'] == 'passed'
    assert 'PRIVATE' not in str(fact)
    assert repo.progress_metadata(VerificationContext('other-owner', thread, task.id))['tests'] == []


def test_exact_bound_review_never_uses_other_candidate_or_old_version(source, tmp_path):
    agent, task, thread = source
    repo = VerificationEvidenceRepository(tmp_path)
    context = VerificationContext('owner', thread, 'root-A')
    requirement = repo.bind_requirement(context, RequirementProvenanceRecord('req-A', 'owner', 'root-A',
        str(tmp_path), 'v1', (), author_principal_id='root-A'))
    repo.record_independent_review(context, IndependentReviewRecord('review-A', 'req-A', 'run-A', 'root-A',
        'v1', 'pass', 'module_only', owner_ref='owner'))
    task.attributes['review_requirement_id'] = 'req-A'
    fact = coordination_source_facts(agent, [{'run_id': task.id}])[task.id]['verification']['review']
    assert fact['records'][0]['review_id'] == 'review-A'
    repo.bind_requirement(context, replace(requirement, input_version='v2'))
    fact = coordination_source_facts(agent, [{'run_id': task.id}])[task.id]['verification']['review']
    assert fact['records'] == []
    repo.bind_requirement(context, replace(requirement, task_id='unrelated', author_principal_id='unrelated'))
    fact = coordination_source_facts(agent, [{'run_id': task.id}])[task.id]['verification']['review']
    assert fact == {'state': 'unknown', 'reason_code': 'REVIEW_BINDING_UNPROVEN'}


def test_case_decision_without_handoff_is_visible_only_for_scoped_case_creator(source):
    agent, _, thread = source
    case = agent.collaboration_store.open_case({'thread_id': thread, 'task_id': 'root-A', 'created_by': 'run-A'})
    agent.collaboration_store.append_decision(CaseDecision('decision-only', case.case_id, 'observation'))
    agent.conversation_store.observations.append({'thread_id': thread, 'root_task_id': 'root-A',
        'event_type': 'collaboration_case_closed', 'metadata': {'case_id': case.case_id}})
    fact = coordination_source_facts(agent, [{'run_id': 'run-A'}])['run-A']['coordination']
    assert fact['case_decisions'][0]['records'][0]['decision_id'] == 'decision-only'
    assert 'handoffs' not in fact


def test_old_handoff_outside_bounded_tail_is_unknown_not_full_scan(source):
    agent, _, _ = source
    case, _ = _publish(source)
    for index in range(20):
        agent.collaboration_store.append_handoff_item(HandoffBoardItem(
            f'other-{index}', case.case_id, 'request', 'run-A', 'internal_run', 'v1', (), 'published'))
    fact = coordination_source_facts(agent, [{'run_id': 'run-A'}])['run-A']['coordination']
    assert 'handoffs' not in fact
    # case decision remains independently proven, but no absent item is invented.
    assert fact['case_decisions'][0]['records'][0]['decision_id'] == 'decision-A'


@pytest.mark.parametrize('state', ['DONE', 'BLOCKED', 'RUNNING'])
def test_exact_recent_coordination_is_progress_but_never_changes_wait_or_terminal(state):
    sources = {name: {'state': 'read'} for name in
               ('runtime_read', 'attempt', 'tool_failures', 'tool_approvals', 'task_progress')}
    sources['attempt']['status'] = 'running'
    sources['coordination'] = {'state': 'read', 'handoffs': [{'updated_at': 999}]}
    node = {'status': state, 'heartbeat_at': 999, 'last_progress_at': 1,
            'tool_contract': {'open_request_count': 0}, 'blockers': []}
    result = classify_node_progress(node, now=1000, sources=sources)
    assert result['state'] == {'DONE': 'completed', 'BLOCKED': 'waiting_dependency', 'RUNNING': 'working'}[state]
    if state == 'RUNNING':
        assert result['reason'] == 'RECENT_COORDINATION_EVENT'
        for invalid in [None, 'text says done', 1001, -1]:
            sources['coordination']['handoffs'][0]['updated_at'] = invalid
            assert classify_node_progress(node, now=1000, sources=sources)['state'] == 'no_progress'
        sources['coordination']['handoffs'][0]['updated_at'] = 999
        node['heartbeat_at'] = 1999
        assert classify_node_progress(node, now=2000, sources=sources)['state'] == 'no_progress'


def test_expired_shared_budget_never_reads_canonical_task(source):
    agent, _, _ = source
    agent.subagents.load = lambda run: pytest.fail('expired read must not open task')
    fact = coordination_source_facts(agent, [{'run_id': 'run-A'}], deadline=0)['run-A']['coordination']
    assert fact['state'] == 'unreadable' and fact['error_type'] == 'TimeoutError'


def test_list_agents_public_tool_shows_original_scoped_handoff_without_body(tmp_path):
    import json

    from agent_py_agent.agent.agent_core.orchestration.tools.list_agents import ListAgentsTool
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home'),
                                   enable_subagents=True), workspace)
    thread = agent.conversation_store.threads.get_or_create({'canonical_user_id': 'u', 'channel': 'local',
        'channel_conversation_id': 'c', 'channel_user_id': 'u', 'owner_id': agent.home_paths.owner_id,
        'owner_home': str(agent.home_paths.owner_home_dir)})
    agent.conversation_store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'task-T',
                                        'goal': 'native scoped progress', 'status': 'active'})
    child = agent.subagents.create_run(goal='native child', owner=agent.home_paths.owner_id,
        parent_id='task-T', root_id='task-T', depth=1, attributes={
            'conversation_thread_id': thread.thread_id, 'conversation_task_id': 'task-T'})
    agent._current_run_params = SimpleNamespace(run_id='gwreq-native', task_id='gwreq-native',
        request_id='gwreq-native', task_attributes={
            'conversation_task_id': 'task-T', 'conversation_thread_id': thread.thread_id})
    _publish((agent, child, thread.thread_id), root='task-T', actor=child.id)
    result = ListAgentsTool(agent).execute({'run_id': 'task-T'})
    assert result.ok
    body = json.loads(result.output)
    node = next(row for row in body['nodes'] if row['run_id'] == child.id)
    assert node['progress']['sources']['coordination']['handoffs'][0]['item_id'] == 'item-A'
    assert 'PRIVATE_TEXT' not in result.output
