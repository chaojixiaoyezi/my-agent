"""原生交接 ACK/提交只由确切责任人针对读过的冻结版本写入。"""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.tools.handoff_board_tool import (
    ManageHandoffBoardTool,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.models import ToolInvocationContext


@pytest.mark.parametrize('action', ['record_ack', 'submit_handoff'])
def test_native_handoff_recipient_cannot_be_impersonated(tmp_path, action):
    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)
    tool = ManageHandoffBoardTool(agent)
    service = tool._resolve_service()
    thread = agent.conversation_store.threads.create()
    case = service.store.open_case({'thread_id': thread.thread_id})
    item = service.publish_item(case_id=case.case_id, request_id='request', responsible_actor='recipient',
                                frozen_input_version='version-one')
    context = ToolInvocationContext(runtime_snapshot=agent.tools.runtime_snapshot())
    base = {'action': action, 'case_id': case.case_id, 'item_id': item.item_id,
            'frozen_input_version': 'version-one', 'evidence_ids': ['evidence']}
    agent._current_run_params = SimpleNamespace(run_id='coordinator', task_id='coordinator')
    denied = tool.execute_scoped(base, context)
    assert not denied.ok and denied.error_code == 'TOOL_PERMISSION_DENIED'
    assert service.store.load_handoff_item(case.case_id, item.item_id) == item
    agent._current_run_params = SimpleNamespace(run_id='recipient', task_id='recipient')
    if action == 'submit_handoff':
        ack = tool.execute_scoped({**base, 'action': 'record_ack'}, context)
        assert ack.ok
    before = service.store.load_handoff_item(case.case_id, item.item_id)
    for version in ('', 'different-version'):
        denied = tool.execute_scoped({**base, 'frozen_input_version': version}, context)
        assert not denied.ok
        assert service.store.load_handoff_item(case.case_id, item.item_id) == before
    accepted = tool.execute_scoped(base, context)
    assert accepted.ok, accepted.output
    row = json.loads(accepted.output)['result']['item']
    assert row['state'] == ('acked' if action == 'record_ack' else 'submitted')
    again = tool.execute_scoped(base, context)
    assert again.ok
    assert service.store.load_handoff_item(case.case_id, item.item_id).state == row['state']


def test_provenance_scoped_caller_cannot_forge_author_or_reviewer(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration.tools.requirement_provenance_tool import (
        RequirementProvenanceTool,
    )

    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)
    context = ToolInvocationContext(runtime_snapshot=agent.tools.runtime_snapshot())
    agent._current_run_params = SimpleNamespace(run_id='author', task_id='author')
    tool = RequirementProvenanceTool(agent)
    bound = tool.execute_scoped({'action': 'bind_requirement', 'requirement_id': 'R',
        'input_version': 'v1', 'watched_paths': ['source.py'],
        'task_id': 'another-task', 'author_principal_id': 'another-author'}, context)
    assert bound.ok, bound.output
    row = json.loads(bound.output)['requirement']
    assert row['task_id'] == row['author_principal_id'] == 'author'
    forged = tool.execute_scoped({'action': 'record_independent_review', 'requirement_id': 'R',
        'reviewer_principal_id': 'independent-person', 'reviewed_input_version': 'v1',
        'review_verdict': 'pass'}, context)
    assert not forged.ok and forged.error_code == 'TOOL_PERMISSION_DENIED'
    assert 'scope_warnings' in json.loads(forged.output)
    agent._current_run_params = SimpleNamespace(run_id='real-reviewer', task_id='real-reviewer')
    reviewed = tool.execute_scoped({'action': 'record_independent_review', 'requirement_id': 'R',
        'reviewer_principal_id': 'forged-name', 'reviewed_input_version': 'v1',
        'review_verdict': 'pass'}, context)
    assert reviewed.ok, reviewed.output
    assert json.loads(reviewed.output)['review']['reviewer_principal_id'] == 'real-reviewer'


def test_preexisting_receipt_replay_does_not_rewrite_or_reclaim_identity(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)
    agent._current_run_params = SimpleNamespace(run_id='recipient', task_id='recipient')
    tool = ManageHandoffBoardTool(agent)
    service = tool._resolve_service()
    thread = agent.conversation_store.threads.create()
    case = service.store.open_case({'thread_id': thread.thread_id})
    item = service.publish_item(case_id=case.case_id, request_id='legacy', responsible_actor='recipient',
                                frozen_input_version='v1')
    before = service.record_ack(case_id=case.case_id, item_id=item.item_id,
                               recipient_ack={'status': 'claimed'}, frozen_input_version='v1')
    context = ToolInvocationContext(runtime_snapshot=agent.tools.runtime_snapshot())
    result = tool.execute_scoped({'action': 'record_ack', 'case_id': case.case_id,
        'item_id': item.item_id, 'frozen_input_version': 'v1'}, context)
    assert result.ok, result.output
    assert service.store.load_handoff_item(case.case_id, item.item_id) == before
    assert 'existing_receipt_reused_without_historical_identity_claim' in json.loads(result.output)['scope_warnings']


def test_provenance_rejects_passing_test_from_unrelated_project(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration.tools.requirement_provenance_tool import (
        RequirementProvenanceTool,
    )
    from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_owner_root
    from agent_py_agent.agent.verification.repository import (
        VerificationEvidence,
        VerificationEvidenceRepository,
    )

    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)
    agent._current_run_params = SimpleNamespace(run_id='author', task_id='author')
    context = ToolInvocationContext(runtime_snapshot=agent.tools.runtime_snapshot())
    tool = RequirementProvenanceTool(agent)
    assert tool.execute_scoped({'action': 'bind_requirement', 'requirement_id': 'R',
        'input_version': 'v1', 'watched_paths': ['app.py']}, context).ok
    repo = VerificationEvidenceRepository(runtime_owner_root(agent))
    event = repo.record(tool._resolve_context({}), VerificationEvidence(command='test', canonical_command='test',
        kind='test', scope='targeted', status='passed', exit_code=0, cwd=str(tmp_path / 'other'),
        root=str(tmp_path / 'other'), output_summary='another project'))
    before = repo.get_requirement(agent.home_paths.owner_id, 'R')
    linked = tool.execute_scoped({'action': 'link_verification', 'requirement_id': 'R',
        'verification_event_ids': [event['id']], 'observation_scope': 'module_only'}, context)
    assert not linked.ok
    assert 'VERIFICATION_ROOT_MISMATCH' in linked.output
    assert repo.get_requirement(agent.home_paths.owner_id, 'R') == before


def test_parent_query_preserves_authorized_task_filter_and_unfiltered_history(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration.tools.requirement_provenance_tool import (
        RequirementProvenanceTool,
    )

    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)
    context = ToolInvocationContext(runtime_snapshot=agent.tools.runtime_snapshot())
    tool = RequirementProvenanceTool(agent)
    for child in ('child-a', 'child-b'):
        agent._current_run_params = SimpleNamespace(run_id=child, task_id=child)
        assert tool.execute_scoped({'action': 'bind_requirement', 'requirement_id': child,
            'input_version': 'v1', 'watched_paths': ['source.py']}, context).ok
    agent._current_run_params = SimpleNamespace(run_id='parent-next-turn', task_id='parent-next-turn')
    all_rows = tool.execute_scoped({'action': 'query_provenance'}, context)
    assert {r['task_id'] for r in json.loads(all_rows.output)['records']} == {'child-a', 'child-b'}
    selected = tool.execute_scoped({'action': 'query_provenance', 'task_id': 'child-a'}, context)
    assert [r['task_id'] for r in json.loads(selected.output)['records']] == ['child-a']
    assert 'scope_warnings' not in json.loads(selected.output)
