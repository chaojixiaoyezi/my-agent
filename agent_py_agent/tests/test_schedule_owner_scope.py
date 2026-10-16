"""S01–05原owner监控展示、实时离群代清理和累计指标隔离矩阵。"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.conversation.host_actor import HostActorError
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.schedule_control import (
    execute_schedule_control,
    monitor_counts,
)
from agent_py_agent.agent.scheduler import creator_access
from agent_py_agent.agent.user_space.identity_store import (
    ProviderIdentityRecord,
    link_provider_identity,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_scheduler_host_actor import create, group_case


@pytest.fixture(autouse=True)
def isolated_audit_logger(monkeypatch):
    monkeypatch.setattr('agent_py_agent.agent.agent_core.audit_dispatch._logger_for', lambda config:None)


@pytest.mark.parametrize('background',[False,True])
@pytest.mark.parametrize('action',['pause','delete','cancel','resume','update','run_now','recover','takeover'])
@pytest.mark.parametrize('membership',['active','departed','unknown','operator_departed'])
def test_tool_group_cleanup_exact_membership_and_operation(tmp_path,monkeypatch,background,action,membership):
    agent,_,actor,tool=group_case(tmp_path,background=background)
    job=create(tool)
    agent._current_run_params.host_channel_actor=replace(actor,actor_id='ou_b')
    calls=[]
    def members(host,chat):
        calls.append((host,chat))
        if membership=='unknown':
            raise HostActorError('SCHEDULER_MEMBERSHIP_UNAVAILABLE')
        return {'ou_a','ou_b'} if membership=='active' else {'ou_b'} if membership=='departed' else set()
    monkeypatch.setattr(creator_access,'current_group_members',members)
    before=agent.scheduler_repository.store_path.read_bytes()
    result=tool.execute({'action':action,'job_id':job['job_id'],'expected_version':job['version'],'prompt':'should-not-overwrite'})
    permitted=membership=='departed' and action in {'pause','delete','cancel'}
    assert result.ok is permitted,(action,membership,result.error_code)
    if not permitted:
        assert agent.scheduler_repository.store_path.read_bytes()==before
    assert len(calls)==(1 if action in {'pause','delete','cancel'} else 0)
    assert agent.scheduler_repository.get_job(job['job_id'],include_deleted=True)['creator']=={'channel':'feishu','actor_id':'ou_a'}


@pytest.mark.parametrize('operation',['pause','delete','cancel','resume','run','notify'])
def test_slash_same_cleanup_policy(tmp_path,monkeypatch,operation):
    agent,_,_,tool=group_case(tmp_path)
    job=create(tool)
    monkeypatch.setattr(creator_access,'current_group_members',lambda *a:{'ou_b'})
    monkeypatch.setattr('agent_py_agent.agent.gateway_parts.control_service._request_agent_for_scope',lambda *args:agent)
    scope=GatewayControlScope('ou_b','feishu','oc_source',resolved_owner=OwnerIdentity.provider_group('feishu','oc_source'))
    suffix=' none' if operation=='notify' else ''
    result=execute_schedule_control(agent,parse_conversation_control(f'/schedule {operation} {job["job_id"]}{suffix}'),scope)
    assert result.ok is (operation in {'pause','delete','cancel'}),result.error_code
    assert agent.scheduler_repository.get_job(job['job_id'],include_deleted=True)['creator']=={'channel':'feishu','actor_id':'ou_a'}


def test_creator_names_from_identity_index_never_authorize_names(tmp_path,monkeypatch):
    agent,_,actor,tool=group_case(tmp_path)
    job=create(tool)
    link_provider_identity(agent.home_paths,ProviderIdentityRecord('feishu','ou_a','user','ou_a',display_name='同名甲'))
    link_provider_identity(agent.home_paths,ProviderIdentityRecord('feishu','ou_b','user','ou_b',display_name='同名甲'))
    agent._current_run_params.host_channel_actor=replace(actor,actor_id='ou_b')
    data=json.loads(tool.execute({'action':'list'}).output)
    assert data['total']==1 and data['jobs'][0]['creator_display']=='同名甲（feishu:ou_a）'
    monkeypatch.setattr(creator_access,'current_group_members',lambda *a:{'ou_a','ou_b'})
    result=tool.execute({'action':'delete','job_id':job['job_id'],'expected_version':job['version']})
    assert result.error_code=='SCHEDULER_CREATOR_STILL_MEMBER'
    monkeypatch.setattr('agent_py_agent.agent.gateway_parts.control_service._request_agent_for_scope',lambda *args:agent)
    scope=GatewayControlScope('ou_b','feishu','oc_source',resolved_owner=OwnerIdentity.provider_group('feishu','oc_source'))
    assert '同名甲（feishu:ou_a）' in execute_schedule_control(agent,parse_conversation_control('/schedule'),scope).message
    assert monitor_counts(agent,scope)['total']==data['total']


@pytest.mark.parametrize('mode',['complete','permission','loop','pages','timeout','missing_more','bad_items'])
def test_real_member_lookup_requires_complete_bounded_official_pages(monkeypatch,mode):
    calls=[]
    clock=[0.0]
    monkeypatch.setattr('agent_py_agent.agent.adapter.feishu_provider_facts.time.monotonic',lambda:clock[0])
    class Response:
        def __enter__(self):
            return self
        def __exit__(self,*args):
            return False
        def read(self,*args):
            if mode=='timeout':
                clock[0]=3
            if mode=='permission':
                return b'{"code":99991672}'
            row={'items':[{'member_id_type':'open_id','member_id':'ou_b'}], 'has_more':mode!='complete',
                'page_token':'same' if mode=='loop' else str(len(calls))}
            if mode=='missing_more':
                row.pop('has_more')
            if mode=='bad_items':
                row['items']=[{'name':'untrusted'}]
            return json.dumps({'code':0,'data':row}).encode()
    def send(request,*,timeout):
        assert request.full_url.startswith('https://open.feishu.cn/open-apis/im/v1/chats/oc_group/members?')
        assert 0<timeout<=2
        calls.append(request.full_url)
        return Response()
    monkeypatch.setattr(creator_access,'open_authenticated_request',send)
    agent=SimpleNamespace(delivery_service=SimpleNamespace(registry=SimpleNamespace(adapter_for=lambda channel:
        SimpleNamespace(_get_tenant_access_token=lambda:'synthetic-token'))))
    if mode=='complete':
        assert creator_access.current_group_members(agent,'oc_group')=={'ou_b'}
    else:
        with pytest.raises(HostActorError) as error:
            creator_access.current_group_members(agent,'oc_group')
        assert error.value.error_code=='SCHEDULER_MEMBERSHIP_UNAVAILABLE'
    assert len(calls)<=10


def test_private_and_two_groups_keep_one_owner_counts_and_exact_ids(tmp_path):
    from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
    from agent_py_agent.tests.test_schedule_control import _agent
    agents=[_agent(tmp_path),_agent(tmp_path,group='oc_a'),_agent(tmp_path,group='oc_b')]
    all_ids=[]
    for agent,count in zip(agents,[10,10,5],strict=True):
        identifiers=[]
        for index in range(count):
            job=agent.scheduler_repository.create_job(SchedulerJobCreateRequest(name=f'row-{index}',prompt='synthetic',
                thread_id='source',schedule={'kind':'every','every_seconds':600,'anchor_at':1000,'timezone':'UTC'},
                creator={'channel':'feishu','actor_id':'ou_a' if index%2 else 'ou_b'},now=1000))[0]
            identifiers.append(job['job_id'])
        all_ids.append(identifiers)
    for index,agent in enumerate(agents):
        identity=OwnerIdentity.local_main() if index==0 else OwnerIdentity.provider_group('feishu','oc_a' if index==1 else 'oc_b')
        scope=GatewayControlScope('ou_a','feishu','oc_private' if index==0 else identity.owner_id,resolved_owner=identity)
        listing=execute_schedule_control(agent,parse_conversation_control('/schedule'),scope)
        assert listing.ok and listing.details['total']==[10,10,5][index]
        assert monitor_counts(agent,scope)['total']==listing.details['total']
        foreign=all_ids[(index+1)%3][0]
        denied=execute_schedule_control(agent,parse_conversation_control('/schedule delete '+foreign),scope)
        assert not denied.ok and denied.error_code=='SCHEDULER_NOT_FOUND'
        assert not execute_schedule_control(agent,parse_conversation_control('/schedule all'),scope).ok


@pytest.mark.parametrize('known,estimate,cached,expected',[(True,0,250,'25.00%'),(True,25,0,'0.00%'),(False,0,None,'未知')])
def test_status_cumulative_matches_original_tui_snapshot(known,estimate,cached,expected):
    from agent_py_agent.agent.conversation.model_metrics import public_model_metrics
    from agent_py_agent.agent.gateway_parts.status_projection import render_session_model_usage
    from agent_py_agent.cli.chat_parts.tui_model_metrics import render_model_metrics
    raw={'schema':'model_runtime_metrics.v1','input_tokens':1000,'output_tokens':120,'estimated_tokens':estimate,
        'totals_known':known,'cache_read_input_tokens':cached or 0,'cache_read_reported_calls':int(cached is not None)}
    metrics=public_model_metrics(raw)
    text=render_session_model_usage(metrics)
    tui=str(render_model_metrics(raw,1000))
    assert expected in text
    assert '120' in text if known else '输入 未知' in text
    if expected!='未知':
        assert expected in tui
    assert ('约 ' in text)==bool(known and estimate)


def test_cleanup_audit_preserves_creator_and_actual_actor(tmp_path,monkeypatch):
    agent,_,actor,tool=group_case(tmp_path)
    job=create(tool)
    agent._current_run_params.host_channel_actor=replace(actor,actor_id='ou_b')
    monkeypatch.setattr(creator_access,'current_group_members',lambda *a:{'ou_b'})
    events=[]
    monkeypatch.setattr('agent_py_agent.agent.agent_core.audit_dispatch._logger_for',lambda config:SimpleNamespace(log=events.append))
    result=tool.execute({'action':'delete','job_id':job['job_id'],'expected_version':job['version']})
    assert result.ok and len(events)==1
    event=events[0]
    assert event.user_id=='ou_b' and event.details['creator']=={'channel':'feishu','actor_id':'ou_a'}
    assert event.details['operator_actor_id']=='ou_b' and event.status=='pending'
    assert '合成内部任务' not in str(event)


def test_rejoined_creator_invalidates_cleanup_and_cross_group_never_queries(tmp_path,monkeypatch):
    from agent_py_agent.agent.scheduler.control import authorize_job
    agent,_,actor,tool=group_case(tmp_path)
    job=create(tool)
    raw=agent.scheduler_repository.get_job(job['job_id'])
    caller=replace(actor,actor_id='ou_b')
    snapshots=iter([{'ou_b'},{'ou_a','ou_b'}])
    calls=[]
    def current(*args):
        calls.append(True)
        return next(snapshots)
    monkeypatch.setattr(creator_access,'current_group_members',current)
    authorize_job(agent.scheduler_repository,raw,caller,agent=agent,operation='pause')
    with pytest.raises(HostActorError) as error:
        authorize_job(agent.scheduler_repository,raw,caller,agent=agent,operation='delete')
    assert error.value.error_code=='SCHEDULER_CREATOR_STILL_MEMBER'
    for different in [replace(caller,conversation_id='oc_other'),replace(caller,owner_id='feishu/group/oc_other')]:
        with pytest.raises(HostActorError):
            authorize_job(agent.scheduler_repository,raw,different,agent=agent,operation='delete')
    with pytest.raises(HostActorError):
        authorize_job(agent.scheduler_repository,{**raw,'creator':None},caller,agent=agent,operation='delete')
    assert len(calls)==2
