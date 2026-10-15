from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def _agent(tmp_path,kind='user',name='alice'):
    agent = SimpleAgent(AgentConfig(model_backend='echo',my_agent_home=str(tmp_path/'home'),my_agent_owner_provider='feishu',my_agent_owner_kind=kind,my_agent_owner_id=name,enable_self_learning=True),tmp_path/'work')
    store = agent.conversation_store
    thread = store.threads.get_or_create({'canonical_user_id':name,'channel':'feishu','channel_conversation_id':'chat-'+name,'channel_user_id':name})
    task = 'task-'+name
    store.tasks.bind({'thread_id':thread.thread_id,'task_id':task,'goal':'整理方法','status':'active'})
    store.tasks.select_workspace_task({'thread_id':thread.thread_id,'task_id':task})
    store.claims.acquire({'thread_id':thread.thread_id,'task_id':task,'reason':'thread_goal_continue'})
    agent._current_run_params = SimpleNamespace(run_id=task,task_id=task,task_attributes={'conversation_thread_id':thread.thread_id,'conversation_task_id':task,'conversation_task_turn_active':True})
    return agent


@pytest.mark.parametrize('kind',['user','group'])
def test_real_owner_claim_allows_draft_request_only(tmp_path,kind):
    agent = _agent(tmp_path,kind)
    tool = agent.tools.tools['skill_summarize']
    assert tool.execute({'target':'current'}).ok
    from agent_py_agent.tests.test_skill_learning import _ctx, _FakeBackend, _output
    service = agent.skill_learning
    service.runtime = replace(service.runtime,backend=_FakeBackend(_output()))
    current = agent._current_run_params
    final = _ctx(current.run_id,tool_rounds=1,task_id=current.task_id,task_attributes={**current.task_attributes,"conversation_task_completed":True})
    assert service.enqueue_from_finalize(final)=="queued"
    assert service.run_pending().status=="proposed"
    assert not agent.skill_learning.store.load_registry().skills
    assert not list(agent.skill_learning.store.skills_root.glob('learned/*/SKILL.md'))
    from agent_py_agent.agent.capability.skill_learning_proposals import list_learning_proposals
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.gateway_parts.skill_control_service import execute_skill_control
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    candidate = list_learning_proposals(service.store)[0]
    command = parse_conversation_control(f"/skills learned confirm {candidate['proposal_id']} 1")
    other = GatewayControlScope('bob','feishu','chat-bob',resolved_owner=OwnerIdentity.provider_user('feishu','bob'))
    assert not execute_skill_control(agent,command,other).ok
    assert not service.store.load_registry().skills
    owner = OwnerIdentity('feishu',kind,'alice')
    exact = GatewayControlScope('alice','feishu','chat-alice',resolved_owner=owner)
    assert execute_skill_control(agent,command,exact).ok
    assert service.store.load_registry().skills
    assert list_learning_proposals(service.store)[0]['resolved_by']['actor_id']=='alice'

    agent.config.enable_self_learning=False
    assert tool.execute({'target':'current'}).error_code=='SKILL_SUMMARIZE_DISABLED'


def test_cross_owner_service_thread_and_expired_claim_are_rejected(tmp_path):
    agent = _agent(tmp_path/'a')
    other = _agent(tmp_path/'b',name='bob')
    original = agent.skill_learning
    agent.skill_learning = other.skill_learning
    tool = agent.tools.tools['skill_summarize']
    assert tool.execute({'target':'current'}).error_code=='TOOL_PERMISSION_DENIED'
    agent.skill_learning = original
    attrs = agent._current_run_params.task_attributes
    thread = attrs['conversation_thread_id']
    attrs['conversation_thread_id']='another-thread'
    assert tool.execute({'target':'previous'}).error_code=='TOOL_PERMISSION_DENIED'
    attrs['conversation_thread_id']=thread
    claim,_ = agent.conversation_store.claims.load_report(thread)
    agent.conversation_store.claims.finish({'thread_id':thread,'task_id':agent._current_run_params.task_id,'claim_id':claim['claim_id'],'status':'failed'})
    assert claim['status']=='running'
    assert tool.execute({'target':'current'}).error_code=='TOOL_PERMISSION_DENIED'


def test_model_schemas_have_no_human_approval_authority():
    from agent_py_agent.agent.capability.skill_feedback_tool import SkillFeedbackTool
    from agent_py_agent.agent.tooling.skill_summarize_tool import SkillSummarizeTool
    feedback = SkillFeedbackTool.model_spec.input_schema
    summarize = SkillSummarizeTool.model_spec.input_schema
    assert feedback['properties']['operation']['enum']==['adopted','status']
    assert set(summarize['properties'])=={'target','focus'}
    assert feedback['additionalProperties'] is False and summarize['additionalProperties'] is False
