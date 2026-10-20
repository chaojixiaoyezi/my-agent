# LLM: 真实临时ConversationStore/claim及C/P3组件验证叶子；业务失败只注入actual类，不替换授权或伪造退出。
# 模块用途: 固定后台RunParams短门、未结传播及原claim责任，模型/网络/服务零调用。
import json
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable
from agent_py_agent.agent.conversation import background_claim as claims
from agent_py_agent.agent.conversation import background_execution as execution
from agent_py_agent.agent.conversation import runtime
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
    coordinator_execution_scope,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests.test_bounded_call_error_origin import held_call
from agent_py_agent.tests.test_gateway_claim_transition_scope import c_actual as c_actual
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context


# LLM: 宿主身份经原public factory规范化，与原C已准备的local/main一致；Store只写pytest临时owner家。
# 函数用途: 组合真实C claim与真实后台持久store，不伪造worker绑定。
@pytest.fixture
def case(c_actual):
    gate,claim,agent=c_actual
    home=agent.home_paths
    agent.home_paths=home_paths_with_owner(home,resolve_owner_home(home.root,OwnerIdentity.local_main()))
    store=ConversationStore(agent.home_paths.owner_home_dir/'conversations')
    thread=store.threads.get_or_create({'canonical_user_id':'owner','channel':'internal',
        'channel_conversation_id':'ports','channel_user_id':'owner'})
    agent.conversation_store=store
    request=runtime.BackgroundRunRequest(thread_id=thread.thread_id,task_id='ports-task',reason='synthetic',now=0)
    return gate,claim,agent,store,thread,request


# LLM: Bounded错误由实际原Thread尾部生成，其call_id/Context不伪造；其它actual类保原claim字段。
# 函数用途: 提供真实类型反例，确保每个出口不按error_code判断。
@pytest.fixture(params=['outcome','bounded','context','tool'])
def unknown(request,case,monkeypatch):
    _gate,claim,*_=case
    if request.param=='bounded':
        with held_call(monkeypatch,key='background-leaf') as (error,_call):
            yield error
    else:
        yield {'outcome':RequestExecutionOutcomeUncertainError(json.loads(claim.path.read_text()),'actual-background'),
               'context':RequestExecutionContextUnavailable('original_request_required'),
               'tool':ToolWriteContextUnavailable('original_tool_claim_required')}[request.param]


# LLM: 原real ClaimStore生成编号并持久化，依赖里的业务查询只表达该合成任务事实，不替换权限门。
# 函数用途: 为run_with_heartbeat准备真实已领取claim及可控一次业务调用。
def claimed(case,run_once):
    _gate,_claim,_agent,store,thread,_request=case
    args={'thread_id':thread.thread_id,'task_id':'ports-task','reason':'synthetic','now':0}
    row=store.claims.acquire({**args,'lease_seconds':30})
    assert row is not None
    policy=[]
    deps=claims.BackgroundClaimDependencies(claims=store.claims,claim_scope_id=lambda *_:thread.thread_id,
        child_owns_task=lambda _:False,terminal_task=lambda _:False,recovery_block=lambda _:None,
        source_admission=lambda _:'',retire_source=lambda _:None,run_once=run_once,runtime_facts=lambda:{},
        record_policy_failure=policy.append,lease_seconds=30,heartbeat_interval_seconds=1)
    return deps,args,row,policy


def test_actual_unknown_keeps_original_claim_running(case,unknown):
    def fail(_): raise unknown
    deps,args,row,policy=claimed(case,fail)
    with pytest.raises(type(unknown)) as caught:
        claims.run_with_heartbeat(deps,row['claim_id'],args,claim_scope_id=row['claim_scope_id'])
    assert caught.value is unknown
    current=deps.claims.load(args['thread_id'])
    assert current['status']=='running' and current['claim_id']==row['claim_id']
    assert not policy and current.get('takeover',{}).get('allowed') is False


def test_model_attempt_does_not_release_inputs_for_actual_unknown(case,unknown,monkeypatch):
    _gate,_claim,agent,store,*_=case
    called=[]
    def fail(_prompt,*,params):
        called.append(params)
        raise unknown
    agent.run=fail
    released=[]
    monkeypatch.setattr(execution,'_release_failed_attempt_inputs',lambda *_:released.append(True))
    with pytest.raises(type(unknown)) as caught:
        execution._run_background_model_attempt(agent,'合成任务',RunParams(request_id='ports-turn'))
    assert caught.value is unknown and len(called)==1 and not released
    with pytest.raises(InterruptedError):
        called[0].conversation_task_binding_callback.bind_runtime_authority({
            'invocation_run_id':'ports-turn','task_id':'t','run_id':'r','agent_run_id':'a','attempt_id':'x'})


def test_runtime_unknown_does_not_reject_original_inputs(case,unknown,monkeypatch):
    _gate,_claim,agent,store,thread,request=case
    def fail(*_, **_kwargs): raise unknown
    monkeypatch.setattr(runtime,'_invoke_background_main_agent',fail)
    settled=[]
    monkeypatch.setattr(runtime,'_settle_unconsumed_background_turn_input',lambda *_:settled.append(True))
    host=runtime.BackgroundMainAgentRuntime(agent=agent,store=store)
    with pytest.raises(type(unknown)) as caught:
        host._run_agent(thread,request,goal_context=runtime.GoalRuntimeContext())
    assert caught.value is unknown and not settled


@pytest.mark.parametrize('target',['release','reject','closeout'])
def test_auxiliary_actual_unknown_is_not_swallowed(case,unknown,monkeypatch,target):
    _gate,_claim,agent,store,_thread,request=case
    def fail(*_args,**_kwargs): raise unknown
    if target=='release':
        monkeypatch.setattr(store.guidance.recovery,'release_reserved',fail)
        def invoke():
            return execution._release_failed_attempt_inputs(agent,'turn',{'attempt_id':'attempt'})
    elif target=='reject':
        monkeypatch.setattr(runtime,'_scheduler_run_id',lambda _:'turn')
        monkeypatch.setattr(store.guidance.recovery,'reject_pending',fail)
        def invoke():
            return runtime._settle_unconsumed_background_turn_input(agent,request,ValueError('ordinary'))
    else:
        from agent_py_agent.agent.conversation import session_task_report
        monkeypatch.setattr(session_task_report,'close_out_turn',fail)
        result=SimpleNamespace(runtime_status='',error=None,response='合成结果')
        def invoke():
            return runtime._close_out_session_task_turn(agent,request,result,turn_id='turn')
    with pytest.raises(type(unknown)) as caught:
        invoke()
    assert caught.value is unknown


def test_typed_unknown_does_not_mark_activity_as_failure(case,unknown,monkeypatch):
    _gate,_claim,agent,store,thread,request=case
    notes=[]
    class Sink:
        def __init__(self,*_a,**_k): pass
        def fail(self): notes.append('failed')
        def finish(self): notes.append('finished')
    def fail(*_a,**_k): raise unknown
    monkeypatch.setattr(execution,'BackgroundMainActivitySink',Sink)
    monkeypatch.setattr(execution,'run_background_turn_with_compact',fail)
    with pytest.raises(type(unknown)) as caught:
        execution.invoke_background_turn(execution.BackgroundExecutionDependencies(agent,store,None),thread,request,
            user_prompt='合成任务',continuation_injection=[],proactive_delivery_available=False)
    assert caught.value is unknown and not notes


def test_wake_unknown_does_not_finish_or_record_ordinary_failure(case,unknown,monkeypatch):
    *_,store,thread,_request=case
    notes=[]
    def fail(*_a,**_k): raise unknown
    monkeypatch.setattr(runtime,'_claim_scheduler_wake',lambda *_a,**_k:runtime._WakeClaimState())
    monkeypatch.setattr(runtime,'_execute_wake_signal',fail)
    monkeypatch.setattr(runtime,'_note_wake_error',lambda *_:notes.append('note'))
    monkeypatch.setattr(runtime,'_handle_nonquota_wake_error',lambda *_a,**_k:notes.append('finish'))
    signal=SimpleNamespace(reason='synthetic',urgency='normal')
    scheduler=SimpleNamespace()
    with pytest.raises(type(unknown)) as caught:
        runtime._BackgroundSchedulerWakeMixin._run_wake_signal(scheduler,signal,now=0)
    assert caught.value is unknown and not notes


def test_real_c_runparams_transport_preserves_previous_callbacks_and_expiry(case,tmp_path):
    gate,claim,agent,_store,thread,_request=case
    observed=[]
    marker=tmp_path/'written'
    repo=RuntimeRepository(tmp_path/'runtime.db')
    chain=repo.record_run_creation(owner_id=agent.home_paths.owner_id,
        goal='合成绑定',conversation_task_id='ports-task',thread_id=thread.thread_id,run_id='ports-run',role='assistant')
    binding={'invocation_run_id':'ports-turn','task_id':str(chain['task_id']),'run_id':str(repo.get_agent_run(chain['agent_run_id'])['run_id']),
             'agent_run_id':str(chain['agent_run_id']),'attempt_id':str(chain['attempt_id'])}
    def previous(phase,operation):
        bridge.require_background_transaction(phase)
        observed.append(phase)
        return operation()
    def run(_prompt,*,params):
        observed.append(params)
        assert params.conversation_task_binding_callback.bind_runtime_authority(binding) is True
        return params.active_turn_transition_callback('submit',lambda:marker.write_text('one'))
    agent.run=run
    params=RunParams(request_id='ports-turn',run_id='ports-turn',task_id='ports-task',
        active_turn_transition_callback=previous,task_attributes={'unmodified':'original'})
    ctx={'agent':agent,'request':json.loads(claim.path.read_text()),'request_path':claim.path,'request_id':claim.request_id}
    with coordinator_execution_scope(gate,claim,agent),bridge.background_request_scope(ctx):
        result,resolved=execution._run_background_model_attempt(agent,'合成任务',params)
        captured=copy_context()
        sent=observed[0]
        assert marker.read_text()=='one' and result==3 and observed[1:]==['submit']
        persisted=json.loads(claim.path.read_text())['runtime_authority']
        assert persisted['attempt_id']==binding['attempt_id'] and persisted['request_id']==claim.request_id
        assert persisted['gateway_execution_attempt_id']==claim.execution_attempt_id
        assert resolved.attempt_id==binding['attempt_id'] and params.task_attributes=={'unmodified':'original'}
    with pytest.raises(RequestExecutionOutcomeUncertainError):
        captured.run(sent.active_turn_transition_callback,'submit',lambda:pytest.fail('expired callback executed'))
    assert marker.read_text()=='one'


@pytest.mark.parametrize('mode',['ordinary','cancel','compact','capacity'])
def test_original_claim_cleanup_partition_is_preserved(case,mode):
    error={'ordinary':RuntimeError('samecode ordinary'),'cancel':InterruptedError(),
           'compact':execution.BackgroundCompactSliceYield(),'capacity':execution.BackgroundCapacityDeferred()}[mode]
    error.error_code='ACTIVE_TURN_OUTCOME_UNCERTAIN'
    def fail(_): raise error
    deps,args,row,policy=claimed(case,fail)
    if mode=='ordinary':
        with pytest.raises(RuntimeError) as caught:
            claims.run_with_heartbeat(deps,row['claim_id'],args,claim_scope_id=row['claim_scope_id'])
        assert caught.value is error and policy==[args]
    else:
        assert claims.run_with_heartbeat(deps,row['claim_id'],args,claim_scope_id=row['claim_scope_id']) is None
        assert not policy
    assert deps.claims.load(args['thread_id'])['status']==('failed' if mode=='ordinary' else 'cancelled' if mode=='cancel' else 'finished')


def test_count_zero_bind_has_same_params_and_no_source_query(monkeypatch):
    params=RunParams(request_id='single-turn')
    monkeypatch.setattr(bridge,'current_worker_execution',lambda:pytest.fail('default queried source'))
    assert bridge.bind_background_run_params(None,params) is params


# LLM: 反复只观察同一个actual未知，不运行或重放业务；唯一WakeAttemptStore控制计数与隔离，原claim始终实际保留。
# 函数用途: 以原阈值验证运输未知不会把真实pending来源当毒丸结束，并保普通同码错误对照。
def _record_repeated_failure(case,error):
    from agent_py_agent.agent.conversation.wake_attempt_tracking import track_wake_attempt
    from agent_py_agent.agent.conversation.wake_poison import WAKE_POISON_SAME_CAUSE_LIMIT_COUNT

    _gate,_claim,agent,store,thread,_request=case
    signal=store.wakes.raise_signal({'thread_id':thread.thread_id,'root_task_id':'ports-task','reason':'synthetic'})
    row=store.claims.acquire({'thread_id':thread.thread_id,'task_id':'ports-task','reason':'synthetic','lease_seconds':30})
    scheduler=SimpleNamespace(store=store,runtime=SimpleNamespace(agent=agent),_wake_retry_after={},_quota_fallback_wakes=set())
    for index in range(WAKE_POISON_SAME_CAUSE_LIMIT_COUNT):
        with pytest.raises(type(error)) as caught:
            with track_wake_attempt(scheduler,(signal,),index+1,turn_id='ports-turn') as tracker:
                assert tracker.begin(row['claim_id'])==''
                raise error
        assert caught.value is error
    return store,signal,thread,row


def test_actual_unknown_does_not_poison_real_pending_source(case,unknown):
    from agent_py_agent.agent.conversation.wake_poison import WAKE_POISON_SAME_CAUSE_LIMIT_COUNT

    store,signal,thread,row=_record_repeated_failure(case,unknown)
    assert store.wakes.pending_one(signal.wake_signal_id) is not None
    state,error=store.wakes.attempts.state_report(signal.wake_signal_id)
    assert error is None and state.total_count==state.same_cause_count==0
    assert state.uncounted_count==WAKE_POISON_SAME_CAUSE_LIMIT_COUNT
    assert store.wakes.attempts.quarantined_count()==0
    assert store.claims.load(thread.thread_id)['claim_id']==row['claim_id']
    assert store.claims.load(thread.thread_id)['status']=='running'


def test_samecode_ordinary_failure_keeps_original_poison_threshold(case):
    error=RuntimeError('ordinary failure')
    error.error_code='ACTIVE_TURN_OUTCOME_UNCERTAIN'
    store,signal,_thread,_row=_record_repeated_failure(case,error)
    assert store.wakes.pending_one(signal.wake_signal_id) is None
    records,errors=store.wakes.attempts.quarantined()
    assert not errors and len(records)==1 and store.wakes.attempts.quarantined_count()==1


def test_original_c_scope_through_three_leaf_layers_keeps_p3_and_claim(case,unknown,monkeypatch):
    gate,request_claim,agent,store,thread,request=case
    host=runtime.BackgroundMainAgentRuntime(agent=agent,store=store)
    calls=[]
    def run(_prompt,*,params):
        calls.append(params)
        raise unknown
    agent.run=run
    def invoke(*_args, **_kwargs):
        result,_params=execution._run_background_model_attempt(agent,'合成任务',RunParams(request_id='ports-turn'))
        return result,{}
    monkeypatch.setattr(runtime,'_invoke_background_main_agent',invoke)
    def run_once(_):
        return host._run_agent(thread,request,goal_context=runtime.GoalRuntimeContext())
    ctx={'agent':agent,'request':json.loads(request_claim.path.read_text()),
         'request_path':request_claim.path,'request_id':request_claim.request_id}
    with coordinator_execution_scope(gate,request_claim,agent),bridge.background_request_scope(ctx):
        deps,args,row,policy=claimed(case,run_once)
        from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
        p3=reservation_file(gate.paths,request_claim.request_id)
        before=request_claim.path.read_bytes()
        original_reservation=p3.read_bytes()
        with pytest.raises(type(unknown)) as caught:
            claims.run_with_heartbeat(deps,row['claim_id'],args,claim_scope_id=row['claim_scope_id'])
        assert caught.value is unknown and len(calls)==1 and not policy
        assert store.claims.load(thread.thread_id)['claim_id']==row['claim_id']
        assert store.claims.load(thread.thread_id)['status']=='running'
        assert request_claim.path.read_bytes()==before and p3.read_bytes()==original_reservation
