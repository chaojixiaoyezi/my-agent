"""跨入口控制只扩展宿主前台证明；创建与通知来源不变。"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.conversation.host_actor import HostActorError, actor_from_scope
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.schedule_control import execute_schedule_control
from agent_py_agent.agent.scheduler.control import authorize_job
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_schedule_control import _agent, _job


@pytest.mark.parametrize('operation', ['pause', 'resume', 'run', 'notify', 'delete', 'cancel', 'recover'])
@pytest.mark.parametrize('entry', ['local', 'first', 'second'])
def test_foreground_admin_cross_entry_slash_preserves_provenance(tmp_path, operation, entry):
    agent = _agent(tmp_path)
    bind_admin_channel_identity(agent.home_paths.root, 'feishu', 'first')
    bind_admin_channel_identity(agent.home_paths.root, 'feishu', 'second')
    job = _job(agent, 'first', 'cross-entry')
    original = deepcopy(job)
    scope = (GatewayControlScope('main', 'local', '', resolved_owner=OwnerIdentity.local_main()) if entry == 'local'
        else GatewayControlScope(entry, 'feishu', 'private-' + entry,
            metadata={'channel_chat_type': 'p2p'}, resolved_owner=OwnerIdentity.local_main()))
    suffix = ' none' if operation == 'notify' else ''
    result = execute_schedule_control(agent, parse_conversation_control('/schedule ' + operation + ' ' + job['job_id'] + suffix), scope)
    assert result.ok, (result.error_code, result.message)
    current = agent.scheduler_repository.get_job(job['job_id'], include_deleted=True)
    for key in ('owner', 'creator', 'source_thread_id', 'thread_id', 'notification_binding'):
        assert current.get(key) == original.get(key)


def test_false_proof_preserves_old_creator_policy_and_owner_first(tmp_path):
    agent = _agent(tmp_path)
    job = _job(agent, 'first', 'original')
    with pytest.raises(HostActorError):
        authorize_job(agent.scheduler_repository, job, agent=agent)
    with pytest.raises(HostActorError):
        authorize_job(agent.scheduler_repository, dict(job, owner={'provider': 'local', 'kind': 'main', 'id': 'other'}),
            agent=agent, host_foreground_interactive=True)


def test_unbound_actor_cannot_use_foreground_proof(tmp_path):
    agent = _agent(tmp_path)
    job = _job(agent, 'first', 'original')
    thread = agent.conversation_store.threads.get_or_create({'channel': 'feishu', 'channel_conversation_id': 'private-other',
        'channel_user_id': 'other', 'canonical_user_id': 'other', 'owner_id': agent.home_paths.owner_id,
        'owner_home': str(agent.home_paths.owner_home_dir)})
    actor = actor_from_scope(agent, SimpleNamespace(channel='feishu', channel_conversation_id='private-other', channel_user_id='other'), thread.thread_id, 'control')
    with pytest.raises(HostActorError):
        authorize_job(agent.scheduler_repository, job, actor, agent=agent, home_root=agent.home_paths.root,
            host_foreground_interactive=True)


@pytest.mark.parametrize('mode', ['ask', 'auto', 'full-access'])
@pytest.mark.parametrize('entry', ['local', 'second'])
def test_tool_foreground_cross_entry_all_modes_and_nested_actions(tmp_path, mode, entry):
    import json

    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.scheduler.tool import ScheduleTool
    from agent_py_agent.agent.user_space.admin_channel_identity import remove_admin_channel_identity

    agent = _agent(tmp_path)
    for actor_id in ('first', 'second'):
        bind_admin_channel_identity(agent.home_paths.root, 'feishu', actor_id)
    job = _job(agent, 'first', 'different-source')
    policy = Path(agent.home_paths.owner_tool_policy_json)
    value = json.loads(policy.read_text())
    value['permission_mode'] = mode
    policy.write_text(json.dumps(value))
    channel = 'local' if entry == 'local' else 'feishu'
    thread = agent.conversation_store.threads.get_or_create({'channel': channel,
        'channel_conversation_id': '' if entry == 'local' else 'private-second',
        'channel_user_id': '' if entry == 'local' else 'second', 'canonical_user_id': 'second',
        'owner_id': agent.home_paths.owner_id, 'owner_home': str(agent.home_paths.owner_home_dir)})
    actor = None if entry == 'local' else actor_from_scope(agent, SimpleNamespace(channel=channel,
        channel_conversation_id='private-second', channel_user_id='second'), thread.thread_id, 'foreground-control')
    agent._current_run_params = RunParams(request_id='foreground-control', task_attributes={'conversation_thread_id': thread.thread_id},
        host_channel_actor=actor, host_foreground_interactive=True)
    tool = ScheduleTool(agent)
    scoped = tool.execute_scoped({'action': 'pause', 'job_id': job['job_id'], 'expected_version': job['version']},
        SimpleNamespace(runtime_snapshot=SimpleNamespace(owner_type='subagent')))
    assert not scoped.ok
    for action in ('pause', 'resume', 'update', 'recover', 'run_now', 'cancel', 'delete'):
        current = agent.scheduler_repository.get_job(job['job_id'])
        params = {'action': action, 'job_id': job['job_id'], 'expected_version': current['version']}
        if action == 'update':
            params['prompt'] = '合成更新'
        result = tool.execute(params)
        assert result.ok, (mode, entry, action, result.output)
        if action != 'delete':
            assert agent.scheduler_repository.get_job(job['job_id'])['creator'] == job['creator']
    second = _job(agent, 'first', 'revocation')
    agent._current_run_params = RunParams(request_id='foreground-control', task_attributes={'conversation_thread_id': thread.thread_id},
        host_channel_actor=actor, host_foreground_interactive=False)
    assert not tool.execute({'action': 'pause', 'job_id': second['job_id'], 'expected_version': second['version']}).ok
    if actor is not None:
        remove_admin_channel_identity(agent.home_paths.root, 'feishu', 'second')
        agent._current_run_params.host_foreground_interactive = True
        assert not tool.execute({'action': 'pause', 'job_id': second['job_id'], 'expected_version': second['version']}).ok
