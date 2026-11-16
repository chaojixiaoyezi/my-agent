from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.schedule_control import execute_schedule_control
from agent_py_agent.agent.scheduler.tool import ScheduleTool
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_schedule_control import _agent, _job


def test_tool_denial_contains_trusted_actor_owner_and_next_step(tmp_path):
    agent = _agent(tmp_path)
    job = _job(agent, 'first', 'original')
    _job(agent, 'other', 'different')
    outcome = ScheduleTool(agent).execute({'action': 'pause', 'job_id': job['job_id'], 'expected_version': job['version']})
    assert not outcome.ok
    assert outcome.error_code == 'CHANNEL_ACTOR_SCOPE_MISMATCH'
    assert outcome.result_envelope['actor']['actor_id'] == 'other'
    assert outcome.result_envelope['owner'] == agent.scheduler_repository.owner
    assert outcome.result_envelope['next_step']
    assert outcome.result_envelope['effect_outcome'] == 'not_started'


def test_slash_denial_preserves_code_and_structured_context(tmp_path):
    agent = _agent(tmp_path)
    job = _job(agent, 'first', 'original')
    scope = GatewayControlScope('unbound', 'feishu', 'private-unbound', metadata={'channel_chat_type': 'p2p'}, resolved_owner=OwnerIdentity.local_main())
    outcome = execute_schedule_control(agent, parse_conversation_control('/schedule pause ' + job['job_id']), scope)
    assert not outcome.ok
    assert outcome.details['actor']['actor_id'] == 'unbound'
    assert outcome.details['owner'] == agent.scheduler_repository.owner
    assert outcome.details['required_identity'] and outcome.details['next_step']
