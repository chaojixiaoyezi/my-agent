"""原输入行/运行库的到期与迟到取消闭包；只有合成本机资料，不调用网络或模型。"""
import json
import time

from agent_py_agent.agent.conversation.input_dependencies import (
    arm_input_dependency,
    create_input_dependency,
    resolve_input_dependency,
)
from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.gateway_parts.request_worker import _process_gateway_requests
from agent_py_agent.agent.user_space.task_credentials import credential_path
from agent_py_agent.tests.test_background_tool_approval import background
from agent_py_agent.tests.test_gateway_credential_live_entry import _queued_input

_FIXTURES = (background,)


def test_last_resolved_input_retires_only_its_original_deadline(background):
    agent = background.agent
    scope = tool_approval_scope(agent, background.task_id)
    row = create_input_dependency(agent, scope, input_id='deadline-only', provider='synthetic',
                                  expires_at=time.time()+180)
    arm_input_dependency(agent, row['input_id'])
    repo = agent.subagents.runtime_db
    repo.upsert_wake(root_task_id='another-task', next_due_at=time.time()+50, kind='sleep')
    resolve_input_dependency(agent, row['input_id'])
    wakes = repo.list_pending_wakes()
    assert not any(w['root_task_id'] == background.task_id and w['kind'] == 'task_input_deadline' for w in wakes)
    assert any(w['root_task_id'] == 'another-task' and w['kind'] == 'sleep' for w in wakes)
    assert len(background.store.wakes.pending()) == 1


def test_card_cancel_after_wait_reply_closes_only_execution_not_task(tmp_path):
    world = _queued_input(tmp_path, 'group')
    world.base.config.feishu_session_lock_enabled = False
    assert _process_gateway_requests(world.base, world.paths) == 1
    response = json.loads(world.response_path.read_text())
    assert response['runtime_status'] == 'needs_user_input'
    link = world.agent.conversation_store.tasks.load(world.request_id)
    input_id = link.input_dependency_refs[0]
    dep = json.loads(next((world.agent.conversation_store.session_tasks.storage.session_tasks_dir/'inputs').glob('*.json')).read_text())
    card = json.loads(credential_path(world.agent.home_paths.owner_home_dir, dep['reference']).read_text())
    body = {'reference':card['reference'],'decision':'cancel','message_id':card['message_id'],'chat_id':card['chat_id'],
            'submitter':{'actor_id':'ou_synthetic','chat_id':'oc_synthetic','current_group_member':True}}
    assert resolve_private_interaction(world.base, body, channel='feishu', actor_id='ou_synthetic')['ok']
    repo = world.agent.subagents.runtime_db
    run = repo.main_agent_run_for_task(world.request_id)
    assert run['status'] == 'cancelled'
    task_run = repo.get_task_run(run['task_run_id'])
    assert task_run['status'] == 'cancelled' and task_run['closed_at'] > 0
    link = world.agent.conversation_store.tasks.load(world.request_id)
    assert link.status == 'active' and input_id not in link.input_dependency_refs
    assert world.agent.conversation_store.wakes.pending() == []
    assert world.backend.calls == 2 and len(world.sent) == 1


import pytest

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


@pytest.mark.parametrize('change', ['deadline','kind','claim'])
def test_frozen_runtime_row_cannot_claim_or_settle_reordered_same_id(tmp_path, change):
    repo = RuntimeRepository(tmp_path/'runtime.db')
    row = dict(repo.upsert_wake(root_task_id='task',next_due_at=10,kind='task_input_deadline'))
    claim = repo.claim_runtime_wake(row,now=20)
    assert claim is not None
    if change == 'claim':
        fresh = repo.claim_runtime_wake(claim,now=321)
        assert fresh is not None
    else:
        fresh = dict(repo.upsert_wake(root_task_id='task',next_due_at=50 if change == 'deadline' else 10,
                                      kind='sleep' if change == 'kind' else 'task_input_deadline'))
    assert fresh['wake_id'] == row['wake_id']
    assert repo.claim_runtime_wake(row,now=400) is None
    assert not repo.settle_runtime_wake(claim)
    assert not repo.settle_runtime_wake(claim,retry_after=500)
    assert not repo.retire_input_deadline(row)
    assert repo.read_runtime_wake(row['wake_id']) == fresh


def test_runtime_wake_read_only_discovery_then_exact_claim_complete(tmp_path):
    import sqlite3

    from agent_py_agent.agent.gateway_parts.runtime_wake_source import runtime_wake_reader
    repo = RuntimeRepository(tmp_path/'runtime.db')
    row = dict(repo.upsert_wake(root_task_id='task',next_due_at=10,kind='sleep'))
    reader = runtime_wake_reader(tmp_path)
    assert reader.read_only is True and reader.existing_only is True
    with reader._runtime_connection() as connection:
        assert connection.execute('PRAGMA busy_timeout').fetchone()[0] == 0
    assert reader.due_runtime_wakes(now=20) == [row]
    with pytest.raises(sqlite3.OperationalError):
        reader.claim_runtime_wake(row,now=20)
    claim = repo.claim_runtime_wake(row,now=20)
    assert claim is not None and repo.claim_runtime_wake(row,now=20) is None
    assert repo.settle_runtime_wake(claim)
    assert repo.read_runtime_wake(row['wake_id']) is None
    locked_home = tmp_path/'locked'
    locked_repo = RuntimeRepository(locked_home/'runtime.db')
    locked_repo.upsert_wake(root_task_id='locked-task',next_due_at=10,kind='sleep')
    with sqlite3.connect(locked_repo.db_path) as holder:
        holder.execute('PRAGMA journal_mode=DELETE')
        holder.execute('BEGIN EXCLUSIVE')
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            runtime_wake_reader(locked_home).due_runtime_wakes(now=20)
        assert time.monotonic()-started < 2.0
        holder.rollback()


@pytest.mark.parametrize('fault', ['run_unknown','child_unknown_done_attempt','terminal_child_unknown_operation',
                                  'active_child','new_attempt','attempt_unknown','task_run_unknown'])
def test_retired_execution_final_sql_rejects_unknown_or_changed_tree(tmp_path, fault):
    repo = RuntimeRepository(tmp_path/'runtime.db')
    root = repo.record_run_creation(owner_id='local/main',goal='synthetic',run_id='root',role='main')
    assert repo.settle_agent_attempt(agent_run_id=root['agent_run_id'],attempt_id=root['attempt_id'],
        payload={'runtime_status':'needs_user_input','runtime_reason':'TASK_INPUT_WAIT','runtime_source':'input_dependency'})['settled']
    if fault in {'child_unknown_done_attempt','terminal_child_unknown_operation','active_child'}:
        child = repo.record_run_creation(owner_id='local/main',goal='synthetic child',run_id='child',
                                         role='worker',parent_run_id='root')
        if fault != 'active_child':
            assert repo.settle_agent_attempt(agent_run_id=child['agent_run_id'],attempt_id=child['attempt_id'])['settled']
        with repo.transaction() as conn:
            if fault == 'child_unknown_done_attempt':
                conn.execute("UPDATE agent_runs SET status='unknown' WHERE agent_run_id=?",(child['agent_run_id'],))
            elif fault == 'terminal_child_unknown_operation':
                conn.execute("UPDATE agent_runs SET status='done' WHERE agent_run_id=?",(child['agent_run_id'],))
                conn.execute("INSERT INTO tool_operations(operation_id,agent_run_id,attempt_id,attempt_generation,tool_operation_generation,operation_type,status,created_at,updated_at) VALUES('unknown-op',?,?,1,1,'synthetic','UNKNOWN',0,0)",
                             (child['agent_run_id'],child['attempt_id']))
    elif fault == 'new_attempt':
        repo.create_attempt(root['agent_run_id'])
    else:
        with repo.transaction() as conn:
            if fault == 'run_unknown':
                conn.execute("UPDATE agent_runs SET status='unknown' WHERE agent_run_id=?",(root['agent_run_id'],))
            elif fault == 'task_run_unknown':
                conn.execute("UPDATE task_runs SET status='unknown' WHERE task_run_id=?",(root['task_run_id'],))
            else:
                conn.execute("UPDATE agent_attempts SET status='unknown' WHERE attempt_id=?",(root['attempt_id'],))
    before = dict(repo.get_task_run(root['task_run_id']))
    assert not repo.settle_retired_input_execution(task_id=root['task_id'],run_id='root',attempt_id=root['attempt_id'])
    assert dict(repo.get_task_run(root['task_run_id'])) == before


def test_cancel_between_last_input_check_and_attempt_settle_has_no_orphan(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import runtime_mixin
    world = _queued_input(tmp_path, 'group')
    world.base.config.feishu_session_lock_enabled = False
    original = runtime_mixin._settle_main_agent_run_status
    seen = []

    def settle(agent, **kwargs):
        facts = kwargs['facts']
        if facts.runtime_reason == 'TASK_INPUT_WAIT' and not seen:
            seen.append(True)
            dep = json.loads(next((world.agent.conversation_store.session_tasks.storage.session_tasks_dir/'inputs').glob('*.json')).read_text())
            card = json.loads(credential_path(world.agent.home_paths.owner_home_dir,dep['reference']).read_text())
            assert resolve_private_interaction(world.base,{'reference':card['reference'],'decision':'cancel',
                'message_id':card['message_id'],'chat_id':card['chat_id'],
                'submitter':{'actor_id':'ou_synthetic','chat_id':'oc_synthetic','current_group_member':True}},
                channel='feishu',actor_id='ou_synthetic')['ok']
        return original(agent,**kwargs)

    monkeypatch.setattr(runtime_mixin,'_settle_main_agent_run_status',settle)
    assert _process_gateway_requests(world.base,world.paths) == 1
    assert seen == [True]
    run = world.agent.subagents.runtime_db.main_agent_run_for_task(world.request_id)
    assert run['status'] == 'cancelled'
    assert world.agent.subagents.runtime_db.get_task_run(run['task_run_id'])['status'] == 'cancelled'
    assert world.agent.conversation_store.tasks.load(world.request_id).status == 'active'
    assert world.agent.conversation_store.wakes.pending() == []
    assert world.backend.calls == 2


def test_cold_c_db_only_deadline_reaches_original_p3_p5_consumer(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.input_dependencies import input_dependency_access
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
    from agent_py_agent.agent.gateway_parts.request_transactions import (
        request_transaction_ports_scope,
    )
    from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case
    from agent_py_agent.tests.test_gateway_coordinator_loops import cold_context
    from agent_py_agent.tests.test_ingestion_task_credential_scope import _active

    with curator_case(tmp_path,monkeypatch) as (agent,_,backend,gate,old_claim,_):
        with coordinator_execution_scope(gate,old_claim,agent),request_transaction_ports_scope(agent):
            _active(agent)
            scope = tool_approval_scope(agent,agent._current_run_params.run_id)
            row = create_input_dependency(agent,scope,input_id='db-only',provider='synthetic',expires_at=time.time()+60)
            arm_input_dependency(agent,row['input_id'])
            input_dependency_access(agent).mutate(row['input_id'],lambda value:{**value,'expires_at':time.time()-1})
            agent.subagents.runtime_db.upsert_wake(root_task_id=scope.root_task_id,
                root_thread_id=scope.thread_id,next_due_at=time.time()-1,kind='task_input_deadline')
        assert agent.conversation_store.wakes.pending() == []
        context = cold_context(agent,gate)
        producer = CoordinatorBackgroundRequests(context.control_plane,drain_runtime=context.drain_runtime)
        from types import SimpleNamespace

        from agent_py_agent.agent.core import SimpleAgent
        with monkeypatch.context() as guard:
            guard.setattr(SimpleAgent,'__init__',lambda *_a,**_k:pytest.fail('C元数据发现不能构造Agent'))
            refs = [ref for ref in producer._owner_sources(SimpleNamespace(identity=OwnerIdentity.local_main(),home_dir=agent.home_paths.owner_home_dir))
                    if ref['kind'] == 'runtime_wake']
        assert len(refs) == 1
        path,_ = enqueue_source(gate.paths,agent.home_paths.root,refs[0])
        request = read_json_file(path)
        decision = gate.admit(path,(OwnerIdentity.local_main(),session_scope_for_request(request)),AdmissionLimits(8,8))
        assert decision['status'] == 'admitted'
        claim = claim_request(gate.paths,path,coordinator_snapshot=freeze_coordinator_claim(gate,decision))
        assert claim is not None
        with coordinator_execution_scope(gate,claim,agent):
            response = _handle_gateway_request(agent,claim.path)
        assert response['ok'] and response['background_result']['outcome'] == 'handled'
        assert backend.calls == 0
        wakes = agent.conversation_store.wakes.pending()
        assert len(wakes) == 1 and wakes[0].reason == 'task_input_resolved'
        assert agent.subagents.runtime_db.input_deadline_wakes(scope.root_task_id) == []


from agent_py_agent.tests.test_gateway_multiprocess_worker_agent import worker

_FIXTURES = (background, worker)


def test_db_deadline_is_consumed_by_original_worker_claim_and_real_owner_agent(worker):
    from dataclasses import asdict

    from agent_py_agent.agent.conversation.input_dependencies import input_dependency_access
    from agent_py_agent.agent.conversation.tool_approval_scope import ToolApprovalScope
    from agent_py_agent.agent.gateway_parts.io import (
        GATEWAY_REQUEST_FINGERPRINT_SCHEMA,
        gateway_request_fingerprint,
        read_json_file,
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
    from agent_py_agent.agent.gateway_parts.queue_service import (
        claim_request,
        ensure_gateway_folders,
    )
    from agent_py_agent.agent.gateway_parts.request_execution import _handle_gateway_request
    from agent_py_agent.agent.gateway_parts.request_owner_binding import REQUEST_OWNER_KEY
    from agent_py_agent.agent.gateway_parts.runtime_wake_source import (
        freeze_runtime_wake,
        runtime_wake_reader,
    )
    from agent_py_agent.agent.user_space.owner_resolver import ensure_owner_home
    frozen,config,root,owner,_ = worker
    ensure_owner_home(frozen.launch.owner_root,owner)
    agent = create_worker_pool(frozen,config,root).get(owner)
    agent.backend.generate = lambda *_args, **_kwargs: pytest.fail('期限事务不得调用模型')
    thread = agent.conversation_store.threads.get_or_create({'canonical_user_id':'synthetic-user',
        'channel':'test','channel_conversation_id':'deadline-owner-thread'})
    agent.conversation_store.tasks.bind({'task_id':'original-input-task','thread_id':thread.thread_id,'goal':'synthetic input','status':'active'})
    scope = ToolApprovalScope(run_id='original-input-task',root_task_id='original-input-task',thread_id=thread.thread_id,
                              agent_kind='main',agent_name='synthetic',active=True)
    row = create_input_dependency(agent,scope,input_id='original-w-input',provider='synthetic',expires_at=time.time()+60)
    arm_input_dependency(agent,row['input_id'])
    access = input_dependency_access(agent)
    access.mutate(row['input_id'],lambda value:{**value,'expires_at':time.time()-1})
    agent.subagents.runtime_db.upsert_wake(root_task_id=scope.root_task_id,root_thread_id=scope.thread_id,
                                         kind='task_input_deadline',next_due_at=time.time()-1)
    [observed] = runtime_wake_reader(agent.home_paths.owner_home_dir).due_runtime_wakes(now=time.time())
    ref = freeze_runtime_wake(agent.home_paths.owner_home_dir,owner,agent.conversation_store,observed)
    raw = {'id':'background-'+source_digest(ref),'kind':'background','status':'pending','source':'gateway_background',
        'user_id':owner.owner_id,REQUEST_OWNER_KEY:asdict(owner),'metadata':{'background_source':ref,
        'client_input_digest':source_digest(ref),'channel':owner.provider,'gateway_admin_binding_version':'binding-v1'},'priority':'background'}
    raw['request_fingerprint'] = gateway_request_fingerprint(raw,raw['id'])
    raw['request_fingerprint_schema'] = GATEWAY_REQUEST_FINGERPRINT_SCHEMA
    request = freeze_request_route(raw,owner,RouteStamp(2,frozen.launch.partition.c_run_id,100,'binding-v1'))
    paths = gateway_paths_from_root(frozen.launch.gateway_root)
    ensure_gateway_folders(paths)
    path = paths.inbox/(request['id']+'.json')
    write_json_file_atomic(path,request)
    from agent_py_agent.tests.support.multiprocess_dispatch_cases import reservation_for
    snapshot = freeze_claim_snapshot(frozen,ClaimCandidate(path,request,reservation_for(request)))
    p3 = reservation_file(paths,request['id'])
    p3.parent.mkdir(parents=True,exist_ok=True)
    p3.write_bytes(snapshot.reservation_bytes)
    claim = claim_request(paths,path,worker_snapshot=snapshot)
    binding = bind_claimed_request(snapshot,claim,read_json_file(claim.path))
    with worker_execution_scope(binding,agent):
        response = _handle_gateway_request(agent,claim.path)
    assert response['ok'] and response['background_result']['outcome'] == 'handled'
    [signal] = agent.conversation_store.wakes.pending()
    assert signal.reason == 'task_input_resolved' and signal.root_task_id == scope.root_task_id
    assert agent.subagents.runtime_db.input_deadline_wakes(scope.root_task_id) == []


@pytest.mark.parametrize('blocker', ['other_input','unknown_operation','new_attempt'])
def test_late_card_cancel_preserves_parallel_input_unknown_or_successor(tmp_path, blocker):
    from agent_py_agent.agent.conversation.tool_approval_scope import ToolApprovalScope
    world = _queued_input(tmp_path,'group')
    world.base.config.feishu_session_lock_enabled = False
    if blocker == 'other_input':
        generate = world.backend.generate

        def reply(prompt, **kwargs):
            response = generate(prompt, **kwargs)
            if world.backend.calls == 2:
                path = next((world.agent.conversation_store.session_tasks.storage.session_tasks_dir/'inputs').glob('*.json'))
                dependency = json.loads(path.read_text())
                create_input_dependency(world.agent,ToolApprovalScope(**dependency['scope']),
                    input_id='parallel-input',provider='synthetic',expires_at=dependency['expires_at'])
                arm_input_dependency(world.agent,'parallel-input')
            return response

        world.backend.generate = reply
    assert _process_gateway_requests(world.base,world.paths) == 1
    repo = world.agent.subagents.runtime_db
    original = repo.main_agent_run_for_task(world.request_id)
    if blocker == 'unknown_operation':
        with repo.transaction() as conn:
            conn.execute("INSERT INTO tool_operations(operation_id,agent_run_id,attempt_id,attempt_generation,tool_operation_generation,operation_type,status,created_at,updated_at) VALUES('late-unknown',?,?,1,1,'synthetic','UNKNOWN',0,0)",
                (original['agent_run_id'],original['current_attempt_id']))
    elif blocker == 'new_attempt':
        repo.create_attempt(original['agent_run_id'])
    dep = next(json.loads(p.read_text()) for p in (world.agent.conversation_store.session_tasks.storage.session_tasks_dir/'inputs').glob('*.json') if json.loads(p.read_text()).get('provider') == 'credential')
    card = json.loads(credential_path(world.agent.home_paths.owner_home_dir,dep['reference']).read_text())
    assert resolve_private_interaction(world.base,{'reference':card['reference'],'decision':'cancel',
        'message_id':card['message_id'],'chat_id':card['chat_id'],
        'submitter':{'actor_id':'ou_synthetic','chat_id':'oc_synthetic','current_group_member':True}},
        channel='feishu',actor_id='ou_synthetic')['ok']
    current = repo.main_agent_run_for_task(world.request_id)
    assert current['status'] == 'created'
    assert repo.get_task_run(current['task_run_id'])['closed_at'] == 0
    assert world.agent.conversation_store.tasks.load(world.request_id).status == 'active'
    assert world.agent.conversation_store.wakes.pending() == []
    assert len(world.sent) == 1 and world.backend.calls == 2


@pytest.mark.parametrize('kind', ['sleep','resume_after_compact'])
def test_shared_single_row_preserves_non_input_business_wake(background, kind):
    from agent_py_agent.agent.conversation.input_dependencies import input_dependency_access
    from agent_py_agent.agent.conversation.runtime_wake import consume_runtime_wake
    agent = background.agent
    repo = agent.subagents.runtime_db
    row = dict(repo.upsert_wake(root_task_id=background.task_id,kind=kind,next_due_at=1))
    claim = repo.claim_runtime_wake(row,now=2)
    consume_runtime_wake(input_dependency_access(agent),claim,now=2,
        complete=lambda:repo.settle_runtime_wake(claim),task_link_status=lambda *_:'active')
    [signal] = background.store.wakes.pending()
    assert signal.reason == 'wake_queue_due' and signal.root_task_id == background.task_id
    assert repo.read_runtime_wake(row['wake_id']) is None


def test_claimed_replan_is_immediately_claimable_without_old_lease(tmp_path):
    repo = RuntimeRepository(tmp_path/'runtime.db')
    observed = dict(repo.upsert_wake(root_task_id='task',next_due_at=10,kind='task_input_deadline'))
    old = repo.claim_runtime_wake(observed,now=20)
    replanned = dict(repo.upsert_wake(root_task_id='task',next_due_at=21,kind='task_input_deadline'))
    assert replanned['wake_id'] == old['wake_id']
    assert replanned['status'] == 'pending' and replanned['lease_until'] == 0
    fresh = repo.claim_runtime_wake(replanned,now=22)
    assert fresh is not None and fresh['lease_until'] == 322
    assert not repo.settle_runtime_wake(old)
    assert not repo.settle_runtime_wake(old,retry_after=500)
    assert repo.read_runtime_wake(old['wake_id']) == fresh
    assert repo.settle_runtime_wake(fresh)


@pytest.mark.parametrize('kind', ['task_input_deadline','sleep'])
def test_late_old_consumer_does_not_expire_or_publish_after_same_id_replan(background, kind):
    from agent_py_agent.agent.conversation.input_dependencies import input_dependency_access
    from agent_py_agent.agent.conversation.runtime_wake import consume_runtime_wake
    access = input_dependency_access(background.agent)
    now = time.time()
    row = create_input_dependency(background.agent,tool_approval_scope(background.agent,background.task_id),
        input_id='late-consumer-input',provider='synthetic',expires_at=now+60)
    arm_input_dependency(background.agent,row['input_id'])
    access.mutate(row['input_id'],lambda value:{**value,'expires_at':now-1})
    repo = background.agent.subagents.runtime_db
    observed = dict(repo.upsert_wake(root_task_id=background.task_id,kind=kind,next_due_at=now-1))
    old = repo.claim_runtime_wake(observed,now=now)
    replanned = dict(repo.upsert_wake(root_task_id=background.task_id,kind=kind,next_due_at=now+30))
    before_input = access.path(row['input_id']).read_bytes()
    before_refs = background.store.tasks.load(background.task_id).input_dependency_refs
    completed = []
    assert consume_runtime_wake(access,old,now=now,
        complete=lambda:completed.append(True),task_link_status=lambda *_:'active') is False
    assert completed == [] and background.store.wakes.pending() == []
    assert access.path(row['input_id']).read_bytes() == before_input
    assert background.store.tasks.load(background.task_id).input_dependency_refs == before_refs
    assert repo.read_runtime_wake(old['wake_id']) == replanned


@pytest.mark.parametrize('fault', ['schema','owner','root','id','unknown'])
def test_deadline_retirement_refuses_noncanonical_or_unknown_input(background, fault):
    from agent_py_agent.agent.conversation.input_dependencies import input_dependency_access
    access = input_dependency_access(background.agent)
    row = create_input_dependency(background.agent,tool_approval_scope(background.agent,background.task_id),
        input_id='bad-deadline-input',provider='synthetic',expires_at=time.time()+60)
    changes = {'schema':{'schema_version':'unknown'},'owner':{'owner':{'provider':'local','owner_kind':'main','owner_id':'foreign'}},
        'root':{'scope':{**row['scope'],'root_task_id':'other-task'}},'id':{'input_id':'other-input'},'unknown':{'status':'unknown'}}
    access.mutate(row['input_id'],lambda value:{**value,**changes[fault]})
    before = access.runtime_db.input_deadline_wakes(background.task_id)
    with pytest.raises(ValueError):
        access.schedule(background.task_id)
    assert access.runtime_db.input_deadline_wakes(background.task_id) == before


@pytest.mark.parametrize('fault', ['schema','owner','root','id','unknown'])
def test_retired_closeout_refuses_bad_remaining_input(tmp_path, fault):
    from agent_py_agent.agent.conversation.input_dependencies import input_dependency_access
    from agent_py_agent.agent.conversation.task_run_closeout import settle_retired_input_run
    from agent_py_agent.agent.conversation.tool_approval_scope import ToolApprovalScope
    world = _queued_input(tmp_path,'group')
    world.base.config.feishu_session_lock_enabled = False
    assert _process_gateway_requests(world.base,world.paths) == 1
    access = input_dependency_access(world.agent)
    [input_id] = world.agent.conversation_store.tasks.load(world.request_id).input_dependency_refs
    row = access.read(input_id)
    scope = ToolApprovalScope(**row['scope'])
    create_input_dependency(world.agent,scope,input_id='remaining-input',provider='synthetic',expires_at=row['expires_at'])
    changes = {'schema':{'schema_version':'unknown'},'owner':{'owner':{'provider':'local','owner_kind':'main','owner_id':'foreign'}},
        'root':{'scope':{**row['scope'],'root_task_id':'other-task'}},'id':{'input_id':'other-input'},'unknown':{'status':'unknown'}}
    access.mutate('remaining-input',lambda value:{**value,**changes[fault]})
    access.mutate(input_id,lambda value:{**value,'status':'cancelled','armed':False,'resolution_delivery':'retire'})
    run = access.runtime_db.main_agent_run_for_task(world.request_id)
    assert settle_retired_input_run(access,access.runtime_db,input_id) is False
    assert access.runtime_db.main_agent_run_for_task(world.request_id)['status'] == 'created'
    assert access.runtime_db.get_task_run(run['task_run_id'])['closed_at'] == 0
    assert world.agent.conversation_store.tasks.load(world.request_id).status == 'active'
    assert world.agent.conversation_store.wakes.pending() == []


def test_replan_waits_for_authorized_effect_then_old_complete_cannot_delete_it(background, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.conversation.input_dependencies import input_dependency_access
    from agent_py_agent.agent.conversation.runtime_wake import consume_runtime_wake
    access = input_dependency_access(background.agent)
    repo = access.runtime_db
    observer = RuntimeRepository(repo.db_path)
    row = dict(repo.upsert_wake(root_task_id=background.task_id,next_due_at=1,kind='sleep'))
    old = repo.claim_runtime_wake(row,now=2)
    entered,release,replanned = threading.Event(),threading.Event(),threading.Event()
    original = background.store.wakes.raise_signal

    def publish(request, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(request,**kwargs)

    def replan():
        value = dict(observer.upsert_wake(root_task_id=background.task_id,next_due_at=3,kind='sleep'))
        replanned.set()
        return value

    def complete():
        assert replanned.wait(3)
        return repo.settle_runtime_wake(old)

    monkeypatch.setattr(background.store.wakes,'raise_signal',publish)
    with ThreadPoolExecutor(max_workers=2) as pool:
        consumption = pool.submit(consume_runtime_wake,access,old,now=2,complete=complete,task_link_status=lambda *_:'active')
        try:
            assert entered.wait(3)
            update = pool.submit(replan)
            assert not replanned.wait(0.05)
        finally:
            release.set()
        fresh = update.result(3)
        assert consumption.result(3) is True
    assert repo.read_runtime_wake(old['wake_id']) == fresh
    assert fresh['wake_id'] == old['wake_id'] and fresh['status'] == 'pending'
    assert len(background.store.wakes.pending()) == 1
    assert repo.claim_runtime_wake(fresh,now=3) is not None
