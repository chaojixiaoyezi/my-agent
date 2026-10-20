"""原受管进程完成源经过C只读发现与P3/P5消费，不启动真实命令或模型。"""
import json
import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.process_events import process_completion_target
from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.multiprocess_admission import session_scope_for_request
from agent_py_agent.agent.gateway_parts.multiprocess_background_discovery import (
    CoordinatorBackgroundRequests,
)
from agent_py_agent.agent.gateway_parts.multiprocess_background_request import enqueue_source
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_claim import (
    freeze_coordinator_claim,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
    coordinator_execution_scope,
)
from agent_py_agent.agent.gateway_parts.queue_service import claim_request
from agent_py_agent.agent.gateway_parts.request_execution import _handle_gateway_request
from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
from agent_py_agent.agent.tooling.process_registry import (
    BackgroundProcess,
    capture_process_birth_token,
)
from agent_py_agent.agent.tooling.process_scope import ProcessAccessScope
from agent_py_agent.agent.tooling.process_session_records import PROCESS_SESSION_SCHEMA
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    process_session_store_root,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, _owner_home_result
from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case
from agent_py_agent.tests.test_gateway_coordinator_loops import cold_context


# LLM: 原v4只使用合成已退出PID；归属由真实Agent/Store产生，业务命令和模型都不执行。
# 函数用途: 写入一条准确自然完成却尚欠通知的原会话记录。
def _completed(agent, owner=None, *, session_id='bg-original-completion'):
    owner = owner or OwnerIdentity.local_main()
    home = agent.home_paths.owner_home_dir
    store = agent.conversation_store
    thread = store.threads.get_or_create({'canonical_user_id':'completion-user','channel':'test',
        'channel_conversation_id':'completion-original-thread'})
    params = SimpleNamespace(context_scope='conversation',run_id='original-process-task',task_id='original-process-task',
        task_attributes={'conversation_thread_id':thread.thread_id,'conversation_task_id':'original-process-task'})
    target = process_completion_target(agent,params)
    root = process_session_store_root(home,home)
    access = ProcessAccessScope(_owner_home_result(agent.home_paths.root,owner,home).owner_id,thread.thread_id,str(home))
    proc = BackgroundProcess(session_id,'synthetic',99999999,1.0,access_scope=access,pid_birth_token='host-birth',
        status='exited',exit_code=0,finished_at=2.0,completion_target=target,store_root=str(root))
    proc.persisted_snapshot = {'schema':'managed_process_session.v1'}
    payload = proc.to_record()
    payload.update(schema=PROCESS_SESSION_SCHEMA,revision=0,activation_scope=None,retain_until_consumed=False,
        execution_scope={'owner_home':str(home),'thread_id':thread.thread_id,'root_task_id':'original-process-task',
            'run_id':'original-process-task','attempt_id':'original-attempt'},
        launcher_pid=os.getpid(),launcher_birth_token=capture_process_birth_token(os.getpid()),
        child_pid=99999998,child_pid_birth_token='child-birth',reserved_at=1.0,child_launch_started=True,
        handoff_confirmed=True,stop_requested=False)
    authority = ProcessSessionStore(root)
    return authority,authority.write(payload),thread


def test_cold_c_original_process_completion_reaches_original_claim_and_wake_once(tmp_path,monkeypatch):
    with curator_case(tmp_path,monkeypatch) as (agent,_,backend,gate,_,_):
        authority,record,thread = _completed(agent)
        context = cold_context(agent,gate)
        producer = CoordinatorBackgroundRequests(context.control_plane,drain_runtime=context.drain_runtime)
        from agent_py_agent.agent.core import SimpleAgent
        with monkeypatch.context() as guard:
            guard.setattr(SimpleAgent,'__init__',lambda *_a,**_k: (_ for _ in ()).throw(AssertionError('C不能构造Agent')))
            guard.setattr(ProcessSessionStore,'transaction',lambda *_a,**_k: (_ for _ in ()).throw(AssertionError('C不能取process事务/redo')))
            refs = [ref for ref in producer._owner_sources(SimpleNamespace(identity=OwnerIdentity.local_main(),
                home_dir=agent.home_paths.owner_home_dir)) if ref['kind']=='process_completion']
        assert len(refs)==1, 'managed_terminal_completion_must_have_MP_source'
        path,created = enqueue_source(gate.paths,agent.home_paths.root,refs[0])
        assert created
        request = read_json_file(path)
        decision = gate.admit(path,(OwnerIdentity.local_main(),session_scope_for_request(request)),AdmissionLimits(8,8))
        assert decision['status']=='admitted'
        claim = claim_request(gate.paths,path,coordinator_snapshot=freeze_coordinator_claim(gate,decision))
        assert claim is not None
        with coordinator_execution_scope(gate,claim,agent):
            response = _handle_gateway_request(agent,claim.path)
        assert response['ok'] and response['background_result']['outcome']=='handled'
        assert backend.calls==0
        [wake] = agent.conversation_store.wakes.pending()
        assert wake.reason=='managed_process_exited' and wake.thread_id==thread.thread_id
        assert wake.root_task_id==record['completion_target']['task_id']
        assert wake.metadata['process_completion']['session_id']==record['session_id']
        assert authority.load(record['session_id']).record['completion_notice_id']==wake.wake_signal_id
        assert not [ref for ref in producer._owner_sources(SimpleNamespace(identity=OwnerIdentity.local_main(),
            home_dir=agent.home_paths.owner_home_dir)) if ref['kind']=='process_completion']
        assert len(agent.conversation_store.wakes.pending())==1


# LLM: 这些组件负例使用真实canonical目录与原Store，不绑定模型或创建业务命令。
# 函数用途: 为来源身份、并发与回执故障准备一个独立owner。
@pytest.fixture
def completion(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path/'home'),model_backend='echo'),tmp_path/'workspace')
    authority,record,thread = _completed(agent)
    return agent,authority,record,thread


# LLM: 测试只经原纯冻结器取得locator，不手拼权限/claim字典。
# 函数用途: 为一个真实记录构造原来源引用。
def _freeze(agent,record):
    from agent_py_agent.agent.gateway_parts.process_completion_source import (
        freeze_process_completion,
    )
    return freeze_process_completion(agent.home_paths.root,OwnerIdentity.local_main(),agent.conversation_store,record['session_id'])


@pytest.mark.parametrize('change',['stop','notice','revision'])
def test_old_full_snapshot_has_zero_publication_after_original_record_changes(completion,change):
    from agent_py_agent.agent.gateway_parts.process_completion_source import (
        consume_process_completion,
    )
    agent,authority,record,_ = completion
    ref = _freeze(agent,record)
    updated = {**record,**({'stop_requested':True} if change=='stop' else
        {'completion_notice_id':'existing-notice'} if change=='notice' else {'reason':'new-revision'})}
    newer = authority.write(updated)
    assert consume_process_completion(agent,ref)=='source_changed'
    assert agent.conversation_store.wakes.pending()==[]
    assert authority.load(record['session_id']).record==newer


@pytest.mark.parametrize('fault',['owner','thread','store','filename','unknown_schema'])
def test_foreign_or_corrupt_completion_never_becomes_source(completion,fault):
    from agent_py_agent.agent.gateway_parts.process_completion_source import (
        freeze_process_completion,
    )
    agent,authority,record,_ = completion
    path = authority.record_path(record['session_id'])
    value = json.loads(path.read_text())
    if fault=='owner':
        value['access_scope']['owner_id']='foreign-owner'
    elif fault=='thread':
        value['completion_target']['thread_id']='thread-foreign'
    elif fault=='store':
        value['completion_target']['store_root']=str(agent.root/'another-conversations')
    elif fault=='filename':
        value['session_id']='bg-other-original'
    else:
        value['schema']='managed_process_session.unknown'
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    with pytest.raises((ValueError,TypeError)):
        freeze_process_completion(agent.home_paths.root,OwnerIdentity.local_main(),agent.conversation_store,record['session_id'])
    assert path.read_bytes()==before and agent.conversation_store.wakes.pending()==[]


@pytest.mark.parametrize('status',['running','unknown'])
def test_unfinished_process_is_not_completion_source(completion,status):
    agent,authority,record,_ = completion
    value={**record,'status':status,'finished_at':None,'exit_code':None}
    path=authority.record_path(record['session_id'])
    path.write_text(json.dumps(value))
    assert _freeze(agent,value) is None
    assert agent.conversation_store.wakes.pending()==[]


def test_exact_stopped_process_only_records_original_explicit_stop(completion):
    from agent_py_agent.agent.gateway_parts.process_completion_source import (
        consume_process_completion,
    )
    agent,authority,record,_ = completion
    record=authority.write({**record,'stop_requested':True})
    assert consume_process_completion(agent,_freeze(agent,record))=='handled'
    assert authority.load(record['session_id']).record['completion_notice_id']=='explicit_stop'
    assert agent.conversation_store.wakes.pending()==[]


def test_wake_then_receipt_failure_keeps_pending_and_original_dedupe(completion,monkeypatch):
    from agent_py_agent.agent.gateway_parts.process_completion_source import (
        consume_process_completion,
    )
    from agent_py_agent.agent.tooling.process_session_store import ProcessSessionTransaction
    agent,authority,record,_ = completion
    ref = _freeze(agent,record)
    original=ProcessSessionTransaction.write
    def fail_receipt(transaction,row):
        if row.get('completion_notice_id'):
            raise OSError('synthetic local receipt failure')
        return original(transaction,row)
    with monkeypatch.context() as guard:
        guard.setattr(ProcessSessionTransaction,'write',fail_receipt)
        assert consume_process_completion(agent,ref)=='pending'
    [first]=agent.conversation_store.wakes.pending()
    assert authority.load(record['session_id']).record['completion_notice_id']==''
    assert consume_process_completion(agent,ref)=='handled'
    [same]=agent.conversation_store.wakes.pending()
    assert same.wake_signal_id==first.wake_signal_id
    assert authority.load(record['session_id']).record['completion_notice_id']==first.wake_signal_id


def test_process_source_read_never_follows_record_symlink(completion,tmp_path):
    from agent_py_agent.agent.gateway_parts.process_completion_source import (
        freeze_process_completion,
    )
    agent,authority,record,_=completion
    path=authority.record_path(record['session_id'])
    foreign=tmp_path/'foreign.json'
    foreign.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(foreign)
    before=foreign.read_bytes()
    with pytest.raises((ValueError,OSError)):
        freeze_process_completion(agent.home_paths.root,OwnerIdentity.local_main(),agent.conversation_store,record['session_id'])
    assert foreign.read_bytes()==before and agent.conversation_store.wakes.pending()==[]


def test_disabled_notifications_do_not_consume_existing_completion(completion):
    from agent_py_agent.agent.gateway_parts.process_completion_source import (
        consume_process_completion,
    )
    agent,authority,record,_=completion
    ref=_freeze(agent,record)
    agent.config.background_process_notifications=False
    assert consume_process_completion(agent,ref)=='source_changed'
    assert agent.conversation_store.wakes.pending()==[]
    assert authority.load(record['session_id']).record==record


from agent_py_agent.tests.test_gateway_multiprocess_worker_agent import worker

_FIXTURES = (worker,)


def test_original_worker_claim_publishes_completion_without_model_or_command(worker):
    from dataclasses import asdict

    from agent_py_agent.agent.gateway_parts.io import (
        GATEWAY_REQUEST_FINGERPRINT_SCHEMA,
        gateway_request_fingerprint,
        write_json_file_atomic,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
    from agent_py_agent.agent.gateway_parts.multiprocess_background_request import source_digest
    from agent_py_agent.agent.gateway_parts.multiprocess_dispatch_binding import (
        bind_claimed_request,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_dispatch_claim import (
        ClaimCandidate,
        freeze_claim_snapshot,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_execution import worker_execution_scope
    from agent_py_agent.agent.gateway_parts.multiprocess_route import (
        RouteStamp,
        freeze_request_route,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_worker_agent import create_worker_pool
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.process_completion_source import (
        freeze_process_completion,
    )
    from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
    from agent_py_agent.agent.gateway_parts.request_owner_binding import REQUEST_OWNER_KEY
    from agent_py_agent.agent.user_space.owner_resolver import ensure_owner_home
    from agent_py_agent.tests.support.multiprocess_dispatch_cases import reservation_for

    frozen,config,root,owner,_ = worker
    ensure_owner_home(frozen.launch.owner_root,owner)
    agent=create_worker_pool(frozen,config,root).get(owner)
    agent.backend.generate=lambda *_a,**_k: pytest.fail('完成生产不能请求模型')
    authority,record,thread=_completed(agent,owner)
    ref=freeze_process_completion(frozen.launch.owner_root,owner,agent.conversation_store,record['session_id'])
    raw={'id':'background-'+source_digest(ref),'kind':'background','status':'pending','source':'gateway_background',
         'user_id':owner.owner_id,REQUEST_OWNER_KEY:asdict(owner),'metadata':{'background_source':ref,
         'client_input_digest':source_digest(ref),'channel':owner.provider,'gateway_admin_binding_version':'binding-v1'},
         'priority':'background'}
    raw['request_fingerprint']=gateway_request_fingerprint(raw,raw['id'])
    raw['request_fingerprint_schema']=GATEWAY_REQUEST_FINGERPRINT_SCHEMA
    request=freeze_request_route(raw,owner,RouteStamp(2,frozen.launch.partition.c_run_id,100,'binding-v1'))
    paths=gateway_paths_from_root(frozen.launch.gateway_root)
    ensure_gateway_folders(paths)
    path=paths.inbox/(request['id']+'.json')
    write_json_file_atomic(path,request)
    snapshot=freeze_claim_snapshot(frozen,ClaimCandidate(path,request,reservation_for(request)))
    p3=reservation_file(paths,request['id'])
    p3.parent.mkdir(parents=True,exist_ok=True)
    p3.write_bytes(snapshot.reservation_bytes)
    claim=claim_request(paths,path,worker_snapshot=snapshot)
    binding=bind_claimed_request(snapshot,claim,read_json_file(claim.path))
    with worker_execution_scope(binding,agent):
        response=_handle_gateway_request(agent,claim.path)
    assert response['ok'] and response['background_result']['outcome']=='handled'
    [wake]=agent.conversation_store.wakes.pending()
    assert wake.reason=='managed_process_exited' and wake.thread_id==thread.thread_id
    assert authority.load(record['session_id']).record['completion_notice_id']==wake.wake_signal_id


@pytest.mark.parametrize('after_install',[False,True])
def test_local_wake_publication_failure_retries_same_original_dedupe(completion,monkeypatch,after_install):
    from agent_py_agent.agent.gateway_parts.process_completion_source import (
        consume_process_completion,
    )
    agent,authority,record,_=completion
    ref=_freeze(agent,record)
    original=agent.conversation_store.wakes.raise_signal
    def fail_publication(request):
        if after_install:
            original(request)
        raise OSError('synthetic local wake publication failure')
    with monkeypatch.context() as guard:
        guard.setattr(agent.conversation_store.wakes,'raise_signal',fail_publication)
        assert consume_process_completion(agent,ref)=='pending'
    assert len(agent.conversation_store.wakes.pending())==int(after_install)
    assert authority.load(record['session_id']).record['completion_notice_id']==''
    assert consume_process_completion(agent,ref)=='handled'
    [wake]=agent.conversation_store.wakes.pending()
    assert authority.load(record['session_id']).record['completion_notice_id']==wake.wake_signal_id


@pytest.mark.parametrize('kind',['request_unknown','context_missing','bounded_unknown','tool_context_missing'])
def test_actual_unknown_same_object_is_not_converted_to_pending(completion,monkeypatch,kind):
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable
    from agent_py_agent.agent.gateway_parts import process_completion_source as source
    agent,authority,record,_=completion
    ref=_freeze(agent,record)
    errors={'request_unknown':RequestExecutionOutcomeUncertainError({'id':'original-request',
        'execution_attempt_id':'original-attempt','lease_epoch':1,'lease_owner':'original-worker'},'completion'),
        'context_missing':RequestExecutionContextUnavailable('synthetic'),
        'bounded_unknown':BoundedCallExitUnconfirmedError('original-call','worker_exit_unconfirmed'),
        'tool_context_missing':ToolWriteContextUnavailable('synthetic')}
    error=errors[kind]
    def unknown(*_args):
        raise error
    monkeypatch.setattr(source,'publish_process_completion',unknown)
    with pytest.raises(type(error)) as caught:
        source.consume_process_completion(agent,ref)
    assert caught.value is error
    assert agent.conversation_store.wakes.pending()==[]
    assert authority.load(record['session_id']).record==record


def test_original_pending_transport_is_requeued_after_receipt_write_failure(tmp_path,monkeypatch):
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
    from agent_py_agent.agent.gateway_parts.request_worker import _finish_claimed_gateway_request
    from agent_py_agent.agent.tooling.process_session_store import ProcessSessionTransaction
    with curator_case(tmp_path,monkeypatch) as (agent,_,backend,gate,_,_):
        authority,record,_=_completed(agent)
        ref=_freeze(agent,record)
        path,_=enqueue_source(gate.paths,agent.home_paths.root,ref)
        request=read_json_file(path)
        decision=gate.admit(path,(OwnerIdentity.local_main(),session_scope_for_request(request)),AdmissionLimits(8,8))
        claim=claim_request(gate.paths,path,coordinator_snapshot=freeze_coordinator_claim(gate,decision))
        p3=reservation_file(gate.paths,claim.request_id)
        original=ProcessSessionTransaction.write
        def unavailable(transaction,row):
            if row.get('completion_notice_id'):
                raise OSError('synthetic receipt I/O')
            return original(transaction,row)
        with coordinator_execution_scope(gate,claim,agent),monkeypatch.context() as guard:
            guard.setattr(ProcessSessionTransaction,'write',unavailable)
            response=_handle_gateway_request(agent,claim.path)
            assert response['background_result']['outcome']=='pending'
            _finish_claimed_gateway_request(gate.paths,claim.path,claim.request_id,response,
                conversation_store=agent.conversation_store,execution_agent=agent,
                expected_execution_attempt_id=claim.execution_attempt_id,expected_lease_epoch=claim.lease_epoch)
        assert path.exists() and read_json_file(path)['status']=='pending'
        assert p3.exists() and not (gate.paths.terminal/path.name).exists()
        assert authority.load(record['session_id']).record['completion_notice_id']==''
        assert len(agent.conversation_store.wakes.pending())==1 and backend.calls==0


@pytest.mark.parametrize('fault',['nofollow','permission','not_directory'])
def test_publication_hard_path_or_permission_error_remains_same_object(completion,monkeypatch,fault):
    from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError
    from agent_py_agent.agent.gateway_parts import process_completion_source as source
    agent,authority,record,_=completion
    ref=_freeze(agent,record)
    error={'nofollow':NoFollowPathError('synthetic'),'permission':PermissionError('synthetic'),
           'not_directory':NotADirectoryError('synthetic')}[fault]
    def hard_failure(*_args):
        raise error
    monkeypatch.setattr(source,'publish_process_completion',hard_failure)
    with pytest.raises(type(error)) as caught:
        source.consume_process_completion(agent,ref)
    assert caught.value is error and agent.conversation_store.wakes.pending()==[]
    assert authority.load(record['session_id']).record==record


def test_rebound_process_root_is_rejected_before_any_foreign_lock_or_write(completion,tmp_path):
    from agent_py_agent.agent.gateway_parts.process_completion_source import (
        consume_process_completion,
    )
    agent,authority,record,_=completion
    ref=_freeze(agent,record)
    old=authority.root.with_name(authority.root.name+'-original')
    authority.root.rename(old)
    foreign=tmp_path/'foreign-directory'
    foreign.mkdir()
    authority.root.symlink_to(foreign,target_is_directory=True)
    with pytest.raises(ValueError,match='background_process_store_anchor_changed'):
        consume_process_completion(agent,ref)
    assert list(foreign.iterdir())==[] and agent.conversation_store.wakes.pending()==[]
