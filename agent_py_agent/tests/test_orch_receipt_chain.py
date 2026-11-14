"""同一隔离长期任务的定时管理、收件人交接、进展及测试证据短链。"""
import json
import subprocess
import sys

from agent_py_agent.agent.agent_core.orchestration.tools.handoff_board_tool import (
    ManageHandoffBoardTool,
)
from agent_py_agent.agent.agent_core.orchestration.tools.requirement_provenance_tool import (
    RequirementProvenanceTool,
)
from agent_py_agent.agent.agent_core.runtime.guidance_tool import SendGuidanceTool
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_owner_root
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.tooling.models import ToolInvocationContext
from agent_py_agent.agent.verification.repository import (
    VerificationEvidence,
    VerificationEvidenceRepository,
)
from agent_py_agent.tests.test_orch01_management_continuity import (
    _bound,  # noqa: F401 fixture
    _foreground,
    _start_scheduled_run,
    case,
)


def test_scheduled_management_recipient_ack_and_evidence_identity_chain(case):  # noqa: F811
    agent = case.agent
    # 此接收者还要绑定自己承担的需求/证据，故原宿主明确指派协调角色；普通worker不得因此获得该动作。
    recipient = agent.subagents.create_run(goal="接收并登记自身证据", thought="test", plan=["ack", "verify"],
        parent_id=case.parent_task_id, root_id=case.parent_task_id, depth=1, role="coordinator",
        allowed_tools=["read_file", "manage_handoff_board", "manage_requirement_provenance"])
    case.child_id = recipient.id
    ctx = ToolInvocationContext(runtime_snapshot=agent.tools.runtime_snapshot())
    _foreground(agent, case.thread, case.parent_task_id)
    direct = SendGuidanceTool(agent).execute_scoped({'target': case.child_id, 'message': 'first'}, ctx)
    assert direct.ok
    _start_scheduled_run(case)
    with _bound(case):
        continued = SendGuidanceTool(agent).execute_scoped({'target': case.child_id, 'message': 'second'}, ctx)
        assert continued.ok
        authority = json.loads(continued.output)['scope_resolution']
        assert authority['principal_run_id'] == case.parent_task_id
        assert authority['scheduler_run_id'] == case.run_id
        board = ManageHandoffBoardTool(agent)
        service = board._resolve_service()
        collab = service.store.open_case({'thread_id': case.thread.thread_id})
        arguments = {'action': 'publish_item', 'case_id': collab.case_id,
            'request_id': 'short-chain', 'responsible_actor': case.child_id,
            'frozen_input_version': 'fixture-v1'}
        denied_publication = board.execute_scoped(arguments, ctx)
        assert not denied_publication.ok and denied_publication.effect_outcome == 'not_started'
        assert 'HANDOFF_RECIPIENT_NOT_IN_SCOPE' in denied_publication.output

    # 定时管理连续性仅用于原管理入口；不能把独立scheduler TaskRun当作原交接发布作者。
    _foreground(agent, case.thread, case.parent_task_id)
    pub = board.execute_scoped(arguments, ctx)
    assert pub.ok, pub.output
    item_id = json.loads(pub.output)['result']['item']['item_id']
    ack_params = {'action': 'record_ack', 'case_id': collab.case_id, 'item_id': item_id,
                  'frozen_input_version': 'fixture-v1'}
    assert not board.execute_scoped(ack_params, ctx).ok

    agent._current_run_params = RunParams(run_id=case.child_id, task_id=case.child_id, request_id=case.child_id)
    previous = set_current_subagent_context(agent, run_id=case.child_id)
    try:
        assert board.execute_scoped(ack_params, ctx).ok
        todo = agent.tools.tools['task_progress'].execute({'action': 'update', 'items': [
            {'id': 'check', 'title': 'isolated check', 'status': 'in_progress', 'owner': case.child_id}]})
        assert todo.ok, todo.output
        provenance = RequirementProvenanceTool(agent)
        bound = provenance.execute_scoped({'action': 'bind_requirement', 'requirement_id': 'short-chain',
            'input_version': 'fixture-v1', 'watched_paths': ['checked.py']}, ctx)
        assert bound.ok
        verify_context = provenance._resolve_context({})
        assert verify_context.task_id == case.child_id
        completed = subprocess.run([sys.executable, '-c', 'assert 6 * 7 == 42'], capture_output=True, check=False)
        assert completed.returncode == 0
        repo = VerificationEvidenceRepository(runtime_owner_root(agent))
        event = repo.record(verify_context, VerificationEvidence(command='python -c isolated arithmetic assertion',
            canonical_command='python', kind='test', scope='targeted', status='passed', exit_code=0,
            cwd=str(case.workspace), root=str(case.workspace), output_summary='actual subprocess exit 0'))
        linked = provenance.execute_scoped({'action': 'link_verification', 'requirement_id': 'short-chain',
            'input_version': 'fixture-v1', 'verification_event_ids': [event['id']],
            'observation_scope': 'module_only'}, ctx)
        assert linked.ok, linked.output
        assert json.loads(linked.output)['requirement']['task_id'] == case.child_id
        assert board.execute_scoped({**ack_params, 'action': 'submit_handoff',
            'evidence_ids': [str(event['id'])]}, ctx).ok
    finally:
        restore_current_subagent_context(agent, previous)
    _foreground(agent, case.thread, case.parent_task_id)
    listed = agent.tools.tools['list_agents'].execute({'run_id': case.child_id})
    assert listed.ok, listed.output
    node = next(n for n in json.loads(listed.output)['nodes'] if n['run_id'] == case.child_id)
    assert node['progress']['sources']['required']['task_progress']['state'] == 'read'
    assert node['progress']['sources']['required']['task_progress']['open'] == 1
    item = service.store.load_handoff_item(collab.case_id, item_id)
    assert item.state == 'submitted'
    assert item.recipient_ack['actor_id'] == case.child_id
    assert item.recipient_ack['frozen_input_version'] == 'fixture-v1'
    # 完成一次小测试并不等于全候选验收。
    req = repo.get_requirement(verify_context.owner_ref, 'short-chain')
    assert req.observation_scope == 'module_only' and req.acceptance_status == 'unaccepted'
    changed = repo.mark_edited(verify_context, root=case.workspace, paths=['checked.py'])
    assert 'short-chain' in changed['invalidated_requirements']
