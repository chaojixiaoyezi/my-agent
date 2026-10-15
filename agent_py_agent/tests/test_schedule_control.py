from types import SimpleNamespace

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.conversation.host_actor import actor_from_scope
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    execute_gateway_conversation_control,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.schedule_control import (
    execute_schedule_control,
    monitor_counts,
)
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
from agent_py_agent.agent.scheduler.tool import ScheduleTool
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


def _agent(tmp_path, *, group=''):
    return SimpleAgent(AgentConfig(model_backend='echo',my_agent_home=str(tmp_path/'home'),
        my_agent_owner_provider='feishu' if group else 'local', my_agent_owner_kind='group' if group else 'main',
        my_agent_owner_id=group or 'main', enable_plugins=False),tmp_path)


def _job(agent, actor, name):
    source=agent.conversation_store.threads.get_or_create({'channel':'feishu','channel_conversation_id':'oc_group',
        'channel_user_id':actor,'canonical_user_id':actor,'owner_id':agent.home_paths.owner_id,
        'owner_home':str(agent.home_paths.owner_home_dir)})
    agent._current_run_params=SimpleNamespace(request_id=name,task_id='',task_attributes={
        'conversation_thread_id':source.thread_id,'host_channel_actor':{'channel':'feishu','conversation_id':'oc_group','actor_id':actor}})
    agent._current_run_params.host_channel_actor=actor_from_scope(agent, SimpleNamespace(channel='feishu',
        channel_conversation_id='oc_group',channel_user_id=actor),source.thread_id,name)
    if agent.home_paths.owner_kind == 'group':
        from agent_py_agent.tests.test_group_authorization import (
            grant_synthetic_group_monitor_capability,
        )

        grant_synthetic_group_monitor_capability(agent, agent._current_run_params.host_channel_actor,
            members={'ou_one', 'ou_two', actor})
    result=ScheduleTool(agent).execute({'action':'create','name':name,'prompt':'合成检查','schedule_kind':'every','every_seconds':600})
    assert result.ok,result.content if hasattr(result,'content') else result
    return agent.scheduler_repository.list_jobs()[0][-1]


def test_group_queries_show_all_creators_but_writes_keep_authorization(tmp_path):
    agent=_agent(tmp_path,group='oc_group')
    first=_job(agent,'ou_one','自己监控')
    second=_job(agent,'ou_two','他人监控')
    scope=GatewayControlScope('ou_one','feishu','oc_group',resolved_owner=OwnerIdentity.provider_group('feishu','oc_group'))
    listing=execute_schedule_control(agent,parse_conversation_control('/schedule'),scope)
    assert listing.ok and '自己监控' in listing.message and '他人监控' in listing.message
    assert monitor_counts(agent,scope)['total']==2
    denied=execute_schedule_control(agent,parse_conversation_control('/schedule pause '+second['alias']),scope)
    assert not denied.ok and agent.scheduler_repository.get_job(second['job_id'])['status']=='active'
    agent._current_run_params.task_attributes['host_channel_actor']['actor_id']='ou_one'
    thread_id=agent._current_run_params.task_attributes['conversation_thread_id']
    agent._current_run_params.host_channel_actor=actor_from_scope(agent, SimpleNamespace(channel='feishu',
        channel_conversation_id='oc_group',channel_user_id='ou_one'),thread_id,'query-own')
    assert ScheduleTool(agent).execute({'action':'get','job_id':second['job_id']}).ok
    stopped=execute_schedule_control(agent,parse_conversation_control('/schedule pause '+first['alias']),scope)
    assert stopped.ok and 'status:stop' in stopped.message


def test_admin_private_only_current_owner_without_aggregating_groups(tmp_path):
    group=_agent(tmp_path,group='oc_group')
    own=_job(group,'ou_admin','群内本人监控')
    _job(group,'ou_other','不可见监控')
    admin=_agent(tmp_path)
    bind_admin_channel_identity(admin.home_paths.root,'feishu','ou_admin')
    local=admin.scheduler_repository.create_job(SchedulerJobCreateRequest(name='TUI监控',prompt='合成',thread_id='local-thread',
        schedule={'kind':'every','every_seconds':600,'timezone':'UTC','anchor_at':1000},creator={'local':True},now=1000))[0]
    assert own['alias']!=local['alias']
    scope=GatewayControlScope('ou_admin','feishu','oc_private',metadata={'channel_chat_type':'p2p'},resolved_owner=OwnerIdentity.local_main())
    listing=execute_schedule_control(admin,parse_conversation_control('/schedule'),scope)
    assert listing.ok and '群内本人监控' not in listing.message and 'TUI监控' in listing.message and '不可见监控' not in listing.message
    assert monitor_counts(admin,scope)['total']==1
    control=execute_schedule_control(admin,parse_conversation_control('/schedule notify '+local['alias']+' none'),scope)
    assert control.ok and admin.scheduler_repository.get_job(local['job_id'])['delivery']['mode']=='none'
    paths=gateway_paths_from_root(tmp_path/'gateway')
    paths.root.mkdir(exist_ok=True)
    result=execute_gateway_conversation_control(admin,paths,parse_conversation_control('/status'),scope)
    assert result.ok and result.status.monitors['total']==1 and '/schedule' in result.message


def test_monitor_master_none_suppresses_explicit_rule_origin(tmp_path):
    from agent_py_agent.tests.test_scheduler_monitors import _create, _repo
    repository=_repo(tmp_path)
    job,request=_create(repository)
    rules=[dict(rule,delivery={'mode':'origin'}) for rule in request.rules]
    repository.update_job(job['job_id'],patch={'rules':rules,'delivery':{'mode':'none'}},expected_version=job['version'],now=950)
    assert all(run['delivery']['mode']=='none' for run in repository.reserve_due_runs(now=1000))
