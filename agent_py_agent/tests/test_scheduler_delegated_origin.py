"""原委派谱系创建定时工作：真实Store/创建/调度，供应商与模型不连接生产。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.scheduler.tool import ScheduleTool
from agent_py_agent.tests.test_host_message_context import delegated_case, select_child


# LLM: 复用原G1与typed actor夹具，只在临时owner中建计划，不用attrs伪造根身份。
# 函数用途: 在真实委派孩子中创建隔离计划，返回原聊天和工作来源供迁移合同核验。
def delegated_plan(tmp_path, monkeypatch, *, owner_kind='group', depth=1, mode='isolated', event=False):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation.im_delegation import prepare_delegated_execution
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
        HierarchyChildSpec,
        HierarchyScheduleRequest,
    )
    from agent_py_agent.tests.test_im_delegation_runtime import runtime_fixture

    if owner_kind == 'group':
        agent, actor, refs, task, _context, source = delegated_case(tmp_path, monkeypatch,
            conversation_id='oc_a:thread:future_topic')
    else:
        owned = SimpleAgent(AgentConfig(model_backend='echo', enable_plugins=False, my_agent_home=str(tmp_path/'home'),
            my_agent_owner_provider='local' if owner_kind == 'main' else 'feishu', my_agent_owner_kind=owner_kind,
            my_agent_owner_id='main' if owner_kind == 'main' else 'ou_a'), tmp_path/'workspace')
        monkeypatch.setitem(runtime_fixture.__globals__, 'SimpleAgent', lambda *_: owned)
        agent, params, task, _context, source = runtime_fixture(tmp_path, monkeypatch, conversation_id='oc_a:thread:future_topic')
        prepare_delegated_execution(agent, params)
        actor, refs = agent._current_run_params.host_channel_actor, ()
    if depth < 0:
        agent._current_run_params = RunParams(request_id=actor.entry_request_id, source='gateway',
            host_channel_actor=actor, host_message_references=refs, task_attributes={'conversation_thread_id': source.thread_id})
    elif depth:
        child = agent.subagents.list_runs()[0]
        select_child(agent, child)
        if depth > 1:
            created = agent.subagents.hierarchy.schedule_child_runs(params=HierarchyScheduleRequest(parent_run_id=child.id,
                child_specs=[HierarchyChildSpec(goal='合成孙工作', allowed_tools=['schedule'])], apply=True, requested_by=child.id))
            select_child(agent, agent.subagents.load(created.created_run_ids[0]))
    args = {'action': 'create', 'name': '合成来源合同', 'prompt': '合成工作',
        'schedule_kind': 'every', 'every_seconds': 600, 'execution': {'mode': mode}}
    if event:
        from agent_py_agent.agent.scheduler import event_source_tool as sources

        monkeypatch.setattr(sources, 'group_authority_snapshot', lambda *_: {'provider': 'feishu', 'chat_id': 'oc_a',
            'app_id': 'cli_receiver', 'tenant_key': 'tenant', 'bot_is_member': True, 'members_complete': True})
        monkeypatch.setattr(sources, 'resolve_bot_source', lambda _agent, chat, message: {'app_id': 'cli_receiver',
            'chat_id': chat, 'source_message_id': message, 'sender_id': 'cli_other_bot', 'sender_id_type': 'app_id'})
        args.pop('every_seconds')
        args.update(schedule_kind='event_source', event_source={'source_message_refs': [refs[0][0]]})
    result = ScheduleTool(agent).execute(args)
    assert result.ok, result.output
    job = agent.scheduler_repository.list_jobs()[0][0]
    return SimpleNamespace(agent=agent, actor=actor, task=task, source=source, job=job)


# LLM: 旧账只在临时仓库中按原mutation_scope回放已证旧版本字段，不改真实job、不新增恢复身份。
# 函数用途: 保存合法旧work来源或预留真实事件/手动run，供重启迁移与终态阴性测试。
def queued_run(case, *, legacy=False, event=False):
    repo = case.agent.scheduler_repository
    if legacy:
        with repo._mutation_scope() as (store, admission):
            store['jobs'][case.job['job_id']] = {**store['jobs'][case.job['job_id']], 'source_thread_id': case.task.target_thread_id}
            repo._write_store_unlocked(store, admission)
    if event:
        job = repo.get_job(case.job['job_id'])
        source = job['schedule']['source']
        now = job['created_at'] + 1
        observed = {key: source[key] for key in ['channel', 'event_type', 'app_id', 'tenant_key', 'chat_id', 'sender_type', 'sender_id_type']}
        observed.update(sender_id=source['sender_ids'][0], message_id='om_event', ingress_id='ingress_synthetic',
            created_at=now, content='合成事件')
        assert repo.accept_source_event(observed, now=now)['status'] == 'accepted'
        return repo.reserve_event_runs(now=now)[0]
    return repo.reserve_manual_run(case.job['job_id'])


def test_delegated_child_creates_job_with_exact_original_chat_source(tmp_path, monkeypatch):
    case = delegated_plan(tmp_path, monkeypatch)
    assert case.job['source_thread_id'] == case.source.thread_id
    assert case.job['source_task_id'] == case.task.task_id
    plan = case.agent.conversation_store.threads.require(case.job['thread_id'])
    assert plan.metadata['host_execution_origin.v1']['source_thread_id'] == case.source.thread_id


def test_legacy_delegated_job_prepares_queued_run_with_explicit_origin_migration(tmp_path, monkeypatch):
    from agent_py_agent.agent.scheduler.execution import prepare_execution

    case = delegated_plan(tmp_path, monkeypatch)
    repo = case.agent.scheduler_repository
    run = queued_run(case, legacy=True)
    prepared = prepare_execution(case.agent.scheduler_service, run, now=run['dispatch_after'])
    assert prepared['origin_thread_id'] == case.source.thread_id
    assert prepared['source_thread_id'] == case.task.target_thread_id
    assert prepared['execution_context']['origin_migration']['from_thread_id'] == case.task.target_thread_id
    assert prepared['execution_context']['origin_migration']['to_thread_id'] == case.source.thread_id
    assert repo.get_job(case.job['job_id'])['source_thread_id'] == case.task.target_thread_id


@pytest.mark.parametrize('owner_kind', ['main', 'user', 'group'])
@pytest.mark.parametrize('depth', [-1, 0, 1, 2])
@pytest.mark.parametrize('mode', ['followup', 'isolated'])
def test_direct_delegate_child_and_grandchild_claim_original_owner_topic(tmp_path, monkeypatch, owner_kind, depth, mode):
    from agent_py_agent.agent.conversation.host_actor import background_host_actor

    case = delegated_plan(tmp_path, monkeypatch, owner_kind=owner_kind, depth=depth, mode=mode)
    run = queued_run(case)
    service = case.agent.scheduler_service
    service.enqueue_ready_runs(now=run['dispatch_after']+1)
    signal = next(s for s in case.agent.conversation_store.wakes.pending() if s.root_task_id == run['run_id'])
    claim = service.claim_wake(signal, now=run['dispatch_after']+2)
    assert claim.status == 'claimed'
    assert claim.claim.run['origin_thread_id'] == case.source.thread_id
    assert claim.claim.run['execution_context']['origin_thread_id'] == case.source.thread_id
    actor = background_host_actor(case.agent, signal.thread_id, run['run_id'])
    assert actor.owner_id == case.actor.owner_id and actor.binding() == case.actor.binding()
    assert actor.conversation_id == 'oc_a:thread:future_topic'


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('restart', [False, True])
def test_event_from_real_delegate_grandchild_claims_after_restart_without_replacing_job(tmp_path, monkeypatch, legacy, restart):
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.scheduler.repository import SchedulerRepository
    from agent_py_agent.agent.scheduler.service import SchedulerService

    case = delegated_plan(tmp_path, monkeypatch, depth=2, event=True)
    run = queued_run(case, legacy=legacy, event=True)
    repo = case.agent.scheduler_repository
    before = repo.get_job(case.job['job_id'])
    if restart:
        repo = SchedulerRepository(repo.root, owner_provider=repo.owner['provider'], owner_kind=repo.owner['kind'],
            owner_id=repo.owner['id'], quota_enforcer=repo.quota_enforcer)
        store = ConversationStore(case.agent.conversation_store.storage.root)
        service = SchedulerService(repo, conversation_store=store)
    else:
        service, store = case.agent.scheduler_service, case.agent.conversation_store
    service.enqueue_ready_runs(now=run['dispatch_after']+1)
    signal = next(s for s in store.wakes.pending() if s.root_task_id == run['run_id'])
    claim = service.claim_wake(signal, now=run['dispatch_after']+2)
    assert claim.status == 'claimed' and claim.claim.run['origin_thread_id'] == case.source.thread_id
    assert repo.get_job(case.job['job_id']) == before
    assert bool(claim.claim.run['execution_context'].get('origin_migration')) == legacy
    assert claim.claim.run['event_batch'][0]['ingress_id'] == 'ingress_synthetic'


@pytest.mark.parametrize('fault', ['owner', 'topic', 'actor', 'missing', 'cycle', 'reverse'])
def test_new_plan_bad_host_or_origin_rejected_without_scheduler_write(tmp_path, monkeypatch, fault):
    case = delegated_plan(tmp_path, monkeypatch)
    store = case.agent.conversation_store
    work = store.threads.require(case.task.target_thread_id)
    if fault == 'owner':
        store.threads.update_atomic(case.source.thread_id, lambda row: replace(row, owner_id='foreign'))
    elif fault in {'actor', 'topic'}:
        actor = case.agent._current_run_params.host_channel_actor
        case.agent._current_run_params.host_channel_actor = replace(actor,
            **({'actor_id': 'ou_wrong'} if fault == 'actor' else {'conversation_id': 'oc_a:thread:other'}))
    elif fault == 'missing':
        store.storage.thread_path(case.source.thread_id).unlink()
    else:
        ref = {**work.metadata['host_execution_origin.v1'],
            'source_thread_id': work.thread_id if fault == 'cycle' else case.job['thread_id']}
        store.threads.update_atomic(work.thread_id, lambda row: replace(row,
            metadata={**row.metadata, 'host_execution_origin.v1': ref}))
    before = case.agent.scheduler_repository.store_path.read_bytes()
    result = ScheduleTool(case.agent).execute({'action': 'create', 'name': '不得建', 'prompt': '合成',
        'schedule_kind': 'every', 'every_seconds': 600})
    assert not result.ok
    assert case.agent.scheduler_repository.store_path.read_bytes() == before


@pytest.mark.parametrize('fault', ['owner', 'all_foreign_home', 'topic', 'actor', 'cycle', 'reverse', 'missing',
    'plan_actor', 'source_task', 'run_source', 'frozen_context', 'claimed', 'claim_released', 'started', 'paused', 'cancelled'])
def test_legacy_origin_migration_rejects_bad_proof_or_nonqueued_without_write(tmp_path, monkeypatch, fault):
    from agent_py_agent.agent.scheduler.execution import prepare_execution
    from agent_py_agent.agent.scheduler.repository import SchedulerRunFinish

    case = delegated_plan(tmp_path, monkeypatch)
    run = queued_run(case, legacy=True)
    repo, threads = case.agent.scheduler_repository, case.agent.conversation_store.threads
    work = threads.require(case.task.target_thread_id)
    if fault == 'owner':
        threads.update_atomic(case.source.thread_id, lambda row: replace(row, owner_id='foreign'))
    elif fault == 'all_foreign_home':
        for thread_id in [case.source.thread_id, work.thread_id, case.job['thread_id']]:
            threads.update_atomic(thread_id, lambda row: replace(row, owner_home=str(tmp_path/'foreign-home')))
    elif fault in {'topic', 'actor', 'cycle', 'reverse'}:
        ref = dict(work.metadata['host_execution_origin.v1'])
        if fault in {'cycle', 'reverse'}:
            ref['source_thread_id'] = work.thread_id if fault == 'cycle' else case.job['thread_id']
        else:
            ref['binding'] = {**ref['binding'], **({'actor_id': 'ou_wrong'} if fault == 'actor' else {'conversation_id': 'oc_a:thread:other'})}
        threads.update_atomic(work.thread_id, lambda row: replace(row, metadata={**row.metadata, 'host_execution_origin.v1': ref}))
    elif fault == 'plan_actor':
        plan = threads.require(case.job['thread_id'])
        ref = dict(plan.metadata['host_execution_origin.v1'])
        ref['binding'] = {**ref['binding'], 'actor_id': 'ou_wrong'}
        threads.update_atomic(plan.thread_id, lambda row: replace(row, metadata={**row.metadata, 'host_execution_origin.v1': ref}))
    elif fault == 'missing':
        case.agent.conversation_store.storage.thread_path(case.source.thread_id).unlink()
    elif fault in {'source_task', 'run_source'}:
        with repo._mutation_scope() as (ledger, admission):
            current = ledger['runs'][run['run_id']]
            current['source_task_id' if fault == 'source_task' else 'source_thread_id'] = 'foreign'
            repo._write_store_unlocked(ledger, admission)
    elif fault == 'frozen_context':
        repo.prepare_run_context(run['run_id'], {'origin_thread_id': 'foreign', 'origin_bindings': []})
    elif fault == 'paused':
        repo.pause_job(case.job['job_id'], expected_version=repo.get_job(case.job['job_id'])['version'])
    else:
        claim = repo.claim_run(run['run_id'], lease_seconds=60, now=run['dispatch_after']+1)
        if fault == 'started':
            repo.mark_run_running(run['run_id'], claim['claim_id'], now=run['dispatch_after']+2)
        elif fault == 'claim_released':
            assert repo.release_run_claim(run['run_id'], claim['claim_id'], now=run['dispatch_after']+2)
        elif fault == 'cancelled':
            repo.finish_run(run['run_id'], claim['claim_id'], SchedulerRunFinish(status='cancelled', now=run['dispatch_after']+2))
    before = repo.store_path.read_bytes()
    with pytest.raises((ValueError, RuntimeError, KeyError)):
        prepare_execution(case.agent.scheduler_service, run, now=run['dispatch_after']+3)
    assert repo.store_path.read_bytes() == before
    assert not case.agent.conversation_store.threads.load(run['thread_id'])


def test_migration_reentry_keeps_exact_context_and_one_wake(tmp_path, monkeypatch):
    from agent_py_agent.agent.scheduler.execution import prepare_execution

    case = delegated_plan(tmp_path, monkeypatch)
    run = queued_run(case, legacy=True)
    prepared = prepare_execution(case.agent.scheduler_service, run, now=run['dispatch_after'])
    before = case.agent.scheduler_repository.store_path.read_bytes()
    assert prepare_execution(case.agent.scheduler_service, run, now=run['dispatch_after']+1) == prepared
    assert case.agent.scheduler_repository.store_path.read_bytes() == before
    service = case.agent.scheduler_service
    service.enqueue_ready_runs(now=run['dispatch_after']+2)
    service.enqueue_ready_runs(now=run['dispatch_after']+3)
    assert len([s for s in case.agent.conversation_store.wakes.pending() if s.root_task_id == run['run_id']]) == 1


@pytest.mark.parametrize('change', ['claim', 'pause', 'creator', 'source'])
def test_before_context_cas_race_cannot_migrate_a_changed_ledger(tmp_path, monkeypatch, change):
    from agent_py_agent.agent.scheduler.execution import prepare_execution

    case = delegated_plan(tmp_path, monkeypatch)
    run = queued_run(case, legacy=True)
    repo = case.agent.scheduler_repository
    original, saved = repo.prepare_run_context, []

    # LLM: 只在临时原账制造CAS前竞争，保存竞争完成后的字节，不能把合法claim/pause本身算作迁移写入。
    # 函数用途: 验证锁内重查真实状态与来源，宿主锁外的旧证明不能覆盖刚发生的控制或归属变化。
    def compete(run_id, context, **kwargs):
        if change == 'claim':
            repo.claim_run(run_id, lease_seconds=60, now=run['dispatch_after']+1)
        elif change == 'pause':
            repo.pause_job(case.job['job_id'], expected_version=repo.get_job(case.job['job_id'])['version'])
        else:
            with repo._mutation_scope() as (ledger, admission):
                ledger['jobs'][case.job['job_id']]['creator' if change == 'creator' else 'source_thread_id'] = (
                    {'channel': 'feishu', 'actor_id': 'ou_wrong'} if change == 'creator' else 'foreign')
                repo._write_store_unlocked(ledger, admission)
        saved.append(repo.store_path.read_bytes())
        return original(run_id, context, **kwargs)

    monkeypatch.setattr(repo, 'prepare_run_context', compete)
    with pytest.raises((ValueError, RuntimeError)):
        prepare_execution(case.agent.scheduler_service, run, now=run['dispatch_after']+2)
    assert repo.store_path.read_bytes() == saved[0]


@pytest.mark.parametrize('legacy', [False, True])
def test_event_executes_fake_model_and_delivers_only_to_original_topic_group(tmp_path, monkeypatch, legacy):
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
    from agent_py_agent.tests.test_scheduler_runtime import _Backend, _RetryDelivery

    case = delegated_plan(tmp_path, monkeypatch, depth=2, event=True)
    run = queued_run(case, legacy=legacy, event=True)
    agent, store = case.agent, case.agent.conversation_store
    # 前置委派创建片已经由fixture实际执行，只收掉它自己的待办；本测试只执行精确新srun，不代做模型业务。
    for signal in store.wakes.pending():
        store.wakes.mark_handled(signal.wake_signal_id)
    agent._current_subagent_run_id = ''
    backend, channels = _Backend(), _RetryDelivery()
    monkeypatch.setattr(backend, 'probe_tool_capability', lambda: ProviderToolCapability(provider=backend.name,
        endpoint='local://scheduler-test', model='fake', stream=False, native_supported=True, evidence='test_probe'), raising=False)
    channels.status = 'sent'
    agent.backend = backend
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store, 'scheduler_service': agent.scheduler_service})
    scheduler.tick(now=run['dispatch_after']+1)
    history, errors = agent.scheduler_repository.history(job_id=case.job['job_id'])
    assert not errors and len(history) == 1 and history[0]['status'] == 'done'
    assert len(backend.prompts) == 1
    assert all(context.target == 'oc_a' for context, *_ in channels.attempts)
    assert all(context.thread_id == case.source.thread_id for context, *_ in channels.attempts)
