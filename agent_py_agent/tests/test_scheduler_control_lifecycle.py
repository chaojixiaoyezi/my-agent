"""R07/R08 使用原仓库、宿主 actor、ThreadStore 与取消协议，模型和运输不参与确定性控制。"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation.channels import DeliveryContext
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.conversation.host_actor import HostActorError, actor_from_scope
from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.schedule_control import execute_schedule_control
from agent_py_agent.agent.scheduler.control import (
    cancel_scheduled_task,
    delete_monitor,
    takeover_notifications,
)
from agent_py_agent.agent.scheduler.execution import resolve_delivery, validate_delivery_origin
from agent_py_agent.agent.scheduler.repository import (
    SchedulerConflictError,
    SchedulerJobCreateRequest,
)
from agent_py_agent.agent.scheduler.tool import ScheduleTool
from agent_py_agent.agent.user_space.admin_channel_identity import (
    bind_admin_channel_identity,
    remove_admin_channel_identity,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_schedule_control import _agent, _job


# LLM: 隔离 home、明确 local creator 和原本机来源；私人目标来自真实 ThreadStore 和认证 scope，不手写收件 override。
# 函数用途: 为接管测试准备本机监控与绑定管理员入口。
def local_case(tmp_path):
    agent = _agent(tmp_path)
    source = agent.conversation_store.threads.get_or_create({'channel': 'tui', 'channel_conversation_id': 'local',
        'channel_user_id': 'local', 'canonical_user_id': 'local', 'owner_id': agent.home_paths.owner_id,
        'owner_home': str(agent.home_paths.owner_home_dir)})
    agent._current_run_params = RunParams(request_id='local-create', task_attributes={'conversation_thread_id': source.thread_id})
    result = ScheduleTool(agent).execute({'action': 'create', 'name': '本机监控', 'prompt': '合成',
        'schedule_kind': 'every', 'every_seconds': 600})
    assert result.ok, result.output
    job = agent.scheduler_repository.get_job(json.loads(result.output)['job']['job_id'])
    bind_admin_channel_identity(agent.home_paths.root, 'feishu', 'ou_admin', now=123)
    private = agent.conversation_store.threads.get_or_create({'channel': 'feishu', 'channel_conversation_id': 'oc_private',
        'channel_user_id': 'ou_admin', 'canonical_user_id': 'ou_admin', 'owner_id': agent.home_paths.owner_id,
        'owner_home': str(agent.home_paths.owner_home_dir)})
    actor = actor_from_scope(agent, SimpleNamespace(channel='feishu', channel_conversation_id='oc_private',
        channel_user_id='ou_admin'), private.thread_id, 'takeover-entry')
    scope = GatewayControlScope('ou_admin', 'feishu', 'oc_private', metadata={'channel_chat_type': 'p2p'},
        resolved_owner=OwnerIdentity.local_main())
    return agent, source, private, job, actor, scope


def test_delete_command_exact_creator_and_tombstone_idempotency(tmp_path):
    agent = _agent(tmp_path, group='oc_group')
    job = _job(agent, 'ou_a', '本人监控')
    other = _job(agent, 'ou_b', '另一监控')
    scope = GatewayControlScope('ou_a', 'feishu', 'oc_group', resolved_owner=OwnerIdentity.provider_group('feishu', 'oc_group'))
    bind_admin_channel_identity(agent.home_paths.root, 'feishu', 'ou_a')
    denied = execute_schedule_control(agent, parse_conversation_control('/schedule delete ' + other['job_id']), scope)
    assert not denied.ok
    command = parse_conversation_control('/schedule delete ' + job['alias'])
    result = execute_schedule_control(agent, command, scope)
    assert result.ok and result.details['cancellation_status'] == 'stopped'
    deleted = agent.scheduler_repository.get_job(job['job_id'], include_deleted=True)
    assert deleted['status'] == 'deleted' and deleted['source_thread_id'] == job['source_thread_id']
    assert execute_schedule_control(agent, command, scope).ok
    assert agent.scheduler_repository.get_job(job['job_id'], include_deleted=True)['version'] == deleted['version']
    with pytest.raises(SchedulerConflictError):
        agent.scheduler_repository.delete_job(job['job_id'], expected_version=deleted['version'] + 1)


def test_unknown_creator_delete_denied_and_no_state_change(tmp_path):
    agent, source, _private, job, actor, _scope = local_case(tmp_path)
    legacy = agent.scheduler_repository.create_job(SchedulerJobCreateRequest(name='旧任务', prompt='合成', thread_id=source.thread_id,
        schedule=job['schedule'], creator=None))[0]
    with pytest.raises(HostActorError):
        delete_monitor(agent, agent.scheduler_repository, legacy['job_id'], legacy['version'], actor=actor)
    assert agent.scheduler_repository.get_job(legacy['job_id'])['status'] == 'active'


@pytest.mark.parametrize('mode', ['followup', 'isolated'])
def test_takeover_freezes_future_only_and_survives_restart(tmp_path, mode):
    agent, source, private, job, actor, scope = local_case(tmp_path)
    repo = agent.scheduler_repository
    job = repo.update_job(job['job_id'], patch={'execution': {'mode': mode}}, expected_version=job['version'])
    old = repo.reserve_manual_run(job['job_id'])
    command = parse_conversation_control('/schedule takeover ' + job['alias'])
    result = execute_schedule_control(agent, command, scope)
    assert result.ok, result.message
    updated = repo.get_job(job['job_id'])
    assert updated['source_thread_id'] == source.thread_id and updated['creator'] == {'local': True}
    assert updated['thread_id'] == job['thread_id'] and repo.get_active_run(old['run_id'])['notification_binding'] is None
    assert execute_schedule_control(agent, command, scope).ok
    assert repo.get_job(job['job_id'])['version'] == updated['version']
    repo.claim_run(old['run_id'], lease_seconds=300, now=old['dispatch_after'])
    # 第一条仍在途时不另开 runner；接管只修改后续通知，而非创建替代监控。
    assert len(repo.list_jobs()[0]) == 1
    reopened = _agent(tmp_path)
    assert reopened.scheduler_repository.get_job(job['job_id'])['notification_binding'] == updated['notification_binding']
    # 原仓库预留函数禁止重叠，完成旧运行后才产生未来运行。
    from agent_py_agent.agent.scheduler.repository import SchedulerRunFinish
    claim = repo.get_active_run(old['run_id'])
    repo.finish_run(old['run_id'], claim['claim_id'], SchedulerRunFinish(status='done'))
    future = repo.reserve_manual_run(job['job_id'])
    agent.scheduler_service.enqueue_ready_runs(now=future['dispatch_after'])
    run = repo.get_active_run(future['run_id'])
    thread = agent.conversation_store.threads.require(run['thread_id'])
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id=run['run_id'], reason='scheduled_job_due')
    origin, channel, target = resolve_delivery(agent, thread, request, 'internal', source.thread_id)
    assert (origin.thread_id, channel, target) == (private.thread_id, 'feishu', 'ou_admin')
    context = DeliveryContext(thread_id=origin.thread_id, channel=channel, target=target)
    assert validate_delivery_origin(agent, request, context)
    assert not validate_delivery_origin(agent, request, replace(context, target='ou_other'))
    remove_admin_channel_identity(agent.home_paths.root, 'feishu', 'ou_admin')
    assert not validate_delivery_origin(agent, request, context)
    bind_admin_channel_identity(agent.home_paths.root, 'feishu', 'ou_admin', now=124)
    assert not validate_delivery_origin(agent, request, context)
    with pytest.raises(SchedulerConflictError):
        takeover_notifications(agent, repo, job['job_id'], job['version'], actor)


def test_takeover_tool_uses_frozen_actor_no_target_parameter(tmp_path):
    agent, _source, _private, job, actor, _scope = local_case(tmp_path)
    agent._current_run_params = RunParams(request_id='tool', host_channel_actor=actor,
        task_attributes={'conversation_thread_id': actor.source_thread_id})
    result = ScheduleTool(agent).execute({'action': 'takeover', 'job_id': job['job_id'], 'expected_version': job['version']})
    assert result.ok, result.output
    assert json.loads(result.output)['job']['notification_binding_version'] == 1
    schema = ScheduleTool(agent).model_spec.input_schema['properties']
    assert 'notification_binding' not in schema and 'recipient' not in schema


def test_takeover_group_even_bound_admin_and_nonprivate_denied(tmp_path):
    agent = _agent(tmp_path, group='oc_group')
    job = _job(agent, 'ou_admin', '群监控')
    bind_admin_channel_identity(agent.home_paths.root, 'feishu', 'ou_admin')
    scope = GatewayControlScope('ou_admin', 'feishu', 'oc_group', metadata={'channel_chat_type': 'group'},
        resolved_owner=OwnerIdentity.provider_group('feishu', 'oc_group'))
    assert not execute_schedule_control(agent, parse_conversation_control('/schedule takeover ' + job['alias']), scope).ok
    local, _source, _private, job, _actor, scope = local_case(tmp_path / 'local')
    scope = replace(scope, metadata={'channel_chat_type': 'group'})
    assert not execute_schedule_control(local, parse_conversation_control('/schedule takeover ' + job['alias']), scope).ok


@pytest.mark.parametrize('state', ['queued', 'claimed', 'running', 'waiting'])
def test_delete_cancels_exact_runs_retains_history_and_artifacts(tmp_path, state):
    agent, _source, _private, job, actor, _scope = local_case(tmp_path)
    repo = agent.scheduler_repository
    run = repo.reserve_manual_run(job['job_id'])
    if state != 'queued':
        agent.scheduler_service.enqueue_ready_runs(now=run['dispatch_after'])
        signal = next(row for row in agent.conversation_store.wakes.pending() if row.root_task_id == run['run_id'])
        claim = agent.scheduler_service.claim_wake(signal, now=run['dispatch_after']).claim
        assert claim is not None
        if state in {'running', 'waiting'}:
            repo.mark_run_running(run['run_id'], claim.claim_id)
        if state == 'waiting':
            agent.scheduler_service.park_waiting(claim)
    artifact = repo.store_path.parent / 'synthetic-result.txt'
    artifact.write_text('保留证据')
    facts = delete_monitor(agent, repo, job['job_id'], repo.get_job(job['job_id'])['version'], actor=actor)
    assert artifact.read_text() == '保留证据'
    assert repo.reserve_due_runs(now=job['next_run_at'] + 10000) == []
    remaining = repo.get_active_run(run['run_id'])
    if state == 'running':
        assert facts['cancellation_status'] == 'cancel_requested'
        assert remaining['cancel_requested_at'] and remaining['cancel_cleanup_confirmed']
    else:
        assert facts['cancellation_status'] == 'stopped', facts
        assert remaining is None
        history, errors = repo.history(job_id=job['job_id'])
        assert not errors and any(row['run_id'] == run['run_id'] and row['status'] == 'cancelled' for row in history)
    if state != 'queued':
        assert agent.conversation_store.tasks.load(run['run_id']).status == 'cancelled'


def test_waiting_live_claim_keeps_cancel_requested_until_original_release(tmp_path):
    agent, _source, _private, job, actor, _scope = local_case(tmp_path)
    repo = agent.scheduler_repository
    run = repo.reserve_manual_run(job['job_id'])
    agent.scheduler_service.enqueue_ready_runs(now=run['dispatch_after'])
    signal = agent.conversation_store.wakes.pending()[0]
    claim = agent.scheduler_service.claim_wake(signal, now=run['dispatch_after']).claim
    repo.mark_run_running(run['run_id'], claim.claim_id)
    agent.scheduler_service.park_waiting(claim)
    from agent_py_agent.agent.conversation.run_claim import detached_task_claim_scope_id
    link = agent.conversation_store.tasks.load(run['run_id'])
    lane = detached_task_claim_scope_id(run['thread_id'], run['run_id']) if link.cancellation_scope == 'detached' else run['thread_id']
    background = agent.conversation_store.claims.acquire({'thread_id': run['thread_id'], 'task_id': run['run_id'],
        'claim_scope_id': lane, 'reason': 'scheduled_job_due', 'lease_seconds': 300})
    facts = delete_monitor(agent, repo, job['job_id'], repo.get_job(job['job_id'])['version'], actor=actor)
    assert facts['cancellation_status'] == 'cancel_requested'
    assert repo.get_active_run(run['run_id'])['status'] == 'waiting'
    agent.conversation_store.claims.finish({'thread_id': run['thread_id'], 'claim_scope_id': lane,
        'claim_id': background['claim_id'], 'status': 'cancelled'})
    assert agent.scheduler_service.reconcile_waiting_run(run['run_id'])['status'] == 'cancelled'


def test_restart_retries_persisted_delete_intent_for_claimed_task(tmp_path):
    agent, _source, _private, job, _actor, _scope = local_case(tmp_path)
    repo = agent.scheduler_repository
    run = repo.reserve_manual_run(job['job_id'])
    agent.scheduler_service.enqueue_ready_runs(now=run['dispatch_after'])
    signal = agent.conversation_store.wakes.pending()[0]
    claim = agent.scheduler_service.claim_wake(signal, now=run['dispatch_after']).claim
    assert claim
    # 模拟已提交删除后、资源清理前宿主退出；原 tombstone 保存准确 pending ID。
    repo.delete_job(job['job_id'], expected_version=repo.get_job(job['job_id'])['version'])
    reopened = _agent(tmp_path)
    reopened.scheduler_service.cancellation_handler = lambda row: cancel_scheduled_task(reopened, reopened.scheduler_repository, row)
    reopened.scheduler_service.enqueue_ready_runs()
    assert reopened.conversation_store.tasks.load(run['run_id']).status == 'cancelled'
    assert reopened.scheduler_repository.get_job(job['job_id'], include_deleted=True)['cancel_pending_run_ids'] == []
    assert reopened.scheduler_repository.pending_cancellation_runs() == []


def test_cancel_current_preserves_schedule_and_recovers_only_selected_job(tmp_path):
    from agent_py_agent.agent.scheduler.control import (
        cancel_monitor,
        inspect_monitor_execution,
        recover_monitor,
    )
    agent, _source, _private, job, actor, scope = local_case(tmp_path)
    repo = agent.scheduler_repository
    run = repo.reserve_manual_run(job['job_id'])
    before = repo.get_job(job['job_id'])
    facts = cancel_monitor(agent, repo, job['job_id'], before['version'], actor=actor)
    assert facts['cancellation_status'] == 'stopped'
    kept = repo.get_job(job['job_id'])
    assert kept['status'] == 'active' and kept['next_run_at'] == before['next_run_at']
    assert repo.get_active_run(run['run_id']) is None
    assert inspect_monitor_execution(agent, repo, job['job_id'], actor=actor)['runs'] == []
    next_run = repo.reserve_manual_run(job['job_id'])
    claimed = repo.claim_run(next_run['run_id'], lease_seconds=1, now=next_run['dispatch_after'])
    repo.mark_run_running(next_run['run_id'], claimed['claim_id'])
    # 期限到期仍存活的 runner 不能被 recover 接管或重开执行。
    response = execute_schedule_control(agent, parse_conversation_control('/schedule recover ' + job['alias']), scope)
    assert response.ok and response.details['recovered_run_ids'] == []
    assert len(response.details['runs']) == 1 and response.details['runs'][0]['status'] == 'running'
    with pytest.raises(SchedulerConflictError):
        recover_monitor(agent, repo, job['job_id'], before['version'], actor=actor)


def test_delete_stops_descendants_and_waiting_inputs_without_touching_other_tree(tmp_path):
    import time

    from agent_py_agent.agent.conversation.input_dependencies import (
        create_input_dependency,
        input_dependency_path,
    )
    from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
    agent, source, _private, job, actor, _scope = local_case(tmp_path)
    repo = agent.scheduler_repository
    run = repo.reserve_manual_run(job['job_id'])
    agent.scheduler_service.enqueue_ready_runs(now=run['dispatch_after'])
    signal = agent.conversation_store.wakes.pending()[0]
    claim = agent.scheduler_service.claim_wake(signal, now=run['dispatch_after']).claim
    repo.mark_run_running(run['run_id'], claim.claim_id)
    agent.scheduler_service.park_waiting(claim)
    agent._current_run_params = RunParams(task_id=run['run_id'], request_id=run['run_id'],
        task_attributes={'conversation_thread_id': source.thread_id, 'conversation_task_id': run['run_id']})
    child = agent.subagents.create_run(goal='子任务合成', thought='测试', plan=['读资料'], parent_id=run['run_id'], root_id=run['run_id'],
        attributes={'conversation_task_id': run['run_id'], 'conversation_thread_id': source.thread_id})
    grandchild = agent.subagents.create_run(goal='孙任务合成', thought='测试', plan=['读资料'], parent_id=child.id, root_id=run['run_id'],
        attributes={'conversation_task_id': run['run_id'], 'conversation_thread_id': source.thread_id})
    agent._current_run_params = RunParams(request_id='other', task_attributes={'conversation_thread_id': source.thread_id})
    unrelated = agent.subagents.create_run(goal='无关合成', thought='测试', plan=['读资料'],
        attributes={'conversation_thread_id': source.thread_id})
    agent._current_run_params = RunParams(task_id=run['run_id'], request_id=run['run_id'],
        task_attributes={'conversation_thread_id': source.thread_id, 'conversation_task_id': run['run_id']})
    background = agent.conversation_store.claims.acquire({'thread_id': source.thread_id, 'task_id': run['run_id'],
        'reason': 'scheduled_job_due', 'lease_seconds': 300})
    scope = tool_approval_scope(agent, run['run_id'])
    create_input_dependency(agent, scope, input_id='synthetic-delete-input', provider='test-provider', expires_at=time.time() + 300)
    create_input_dependency(agent, tool_approval_scope(agent, child.id), input_id='synthetic-child-input', provider='test-provider', expires_at=time.time() + 300)
    agent.conversation_store.claims.finish({'thread_id': source.thread_id, 'task_id': run['run_id'],
        'claim_id': background['claim_id'], 'status': 'finished'})
    from agent_py_agent.agent.conversation.run_claim import detached_task_claim_scope_id
    if agent.conversation_store.tasks.load(run['run_id']).cancellation_scope == 'detached':
        lane = detached_task_claim_scope_id(source.thread_id, run['run_id'])
        detached = agent.conversation_store.claims.acquire({'thread_id': source.thread_id, 'task_id': run['run_id'],
            'claim_scope_id': lane, 'reason': 'subagent_runner_finished', 'lease_seconds': 300})
        agent.conversation_store.claims.finish({'thread_id': source.thread_id, 'task_id': run['run_id'],
            'claim_scope_id': lane, 'claim_id': detached['claim_id'], 'status': 'finished'})
    facts = delete_monitor(agent, repo, job['job_id'], repo.get_job(job['job_id'])['version'], actor=actor)
    assert facts['cancellation_status'] == 'stopped', facts
    assert agent.subagents.load(child.id).status == 'CANCELLED'
    assert agent.subagents.load(grandchild.id).status == 'CANCELLED'
    assert agent.subagents.load(unrelated.id).status != 'CANCELLED'
    row = json.loads(input_dependency_path(agent, 'synthetic-delete-input').read_text())
    assert row['status'] == 'cancelled'
    child_input = json.loads(input_dependency_path(agent, 'synthetic-child-input').read_text())
    assert child_input['status'] == 'cancelled'
    assert not agent.conversation_store.tasks.load(run['run_id']).input_dependency_refs


def test_cleanup_unconfirmed_preserves_cancel_intent_and_retry(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import task_resources
    agent, _source, _private, job, actor, _scope = local_case(tmp_path)
    repo = agent.scheduler_repository
    run = repo.reserve_manual_run(job['job_id'])
    agent.scheduler_service.enqueue_ready_runs(now=run['dispatch_after'])
    claim = agent.scheduler_service.claim_wake(agent.conversation_store.wakes.pending()[0], now=run['dispatch_after']).claim
    repo.mark_run_running(run['run_id'], claim.claim_id)
    agent.scheduler_service.park_waiting(claim)
    cleanup = task_resources.cleanup_task_resources
    monkeypatch.setattr(task_resources, 'cleanup_task_resources', lambda _batch: {'main': {'status': 'unknown'},
        'children': {'ok': False, 'cancelled': [], 'failed': [{'error_code': 'synthetic'}]}})
    facts = delete_monitor(agent, repo, job['job_id'], repo.get_job(job['job_id'])['version'], actor=actor)
    assert facts['cancellation_status'] == 'cancel_requested'
    assert repo.get_active_run(run['run_id'])['cancel_cleanup_confirmed'] is False
    monkeypatch.setattr(task_resources, 'cleanup_task_resources', cleanup)
    retried = delete_monitor(agent, repo, job['job_id'], repo.get_job(job['job_id'], include_deleted=True)['version'], actor=actor)
    assert retried['cancellation_status'] == 'stopped'


def test_delete_all_multirule_runs_and_keep_unrelated_job(tmp_path):
    agent, source, _private, job, actor, _scope = local_case(tmp_path)
    repo = agent.scheduler_repository
    rules = [dict(id='rule1', name='规则甲', schedule=job['schedule'], use_llm=True, prompt='合成甲'),
        dict(id='rule2', name='规则乙', schedule=job['schedule'], use_llm=True, prompt='合成乙')]
    job = repo.update_job(job['job_id'], patch={'rules': rules}, expected_version=job['version'])
    first = repo.reserve_manual_run(job['job_id'], rule_id='rule1')
    second = repo.reserve_manual_run(job['job_id'], rule_id='rule2')
    other = repo.create_job(SchedulerJobCreateRequest(name='独立监控', prompt='合成', thread_id=source.thread_id,
        schedule=rules[0]['schedule'], creator={'local': True}))[0]
    unrelated = repo.reserve_manual_run(other['job_id'])
    before = repo.get_active_run(unrelated['run_id'])
    facts = delete_monitor(agent, repo, job['job_id'], repo.get_job(job['job_id'])['version'], actor=actor)
    assert facts['cancellation_status'] == 'stopped'
    history, errors = repo.history(job_id=job['job_id'])
    assert not errors and {row['run_id'] for row in history} == {first['run_id'], second['run_id']}
    assert repo.get_active_run(unrelated['run_id']) == before
    assert repo.get_job(other['job_id'])['status'] == 'active'


def test_takeover_foreign_actor_and_expired_admin_binding_do_not_change_job(tmp_path):
    agent, _source, _private, job, actor, _scope = local_case(tmp_path)
    repo = agent.scheduler_repository
    before = repo.store_path.read_bytes()
    for changed in (replace(actor, owner_id='feishu/users/foreign'), replace(actor, actor_id='ou_other')):
        with pytest.raises(HostActorError):
            takeover_notifications(agent, repo, job['job_id'], job['version'], changed)
        assert repo.store_path.read_bytes() == before
    remove_admin_channel_identity(agent.home_paths.root, 'feishu', 'ou_admin')
    with pytest.raises(HostActorError):
        takeover_notifications(agent, repo, job['job_id'], job['version'], actor)
    assert repo.store_path.read_bytes() == before


def test_restart_preserves_cancel_intent_after_dead_running_run_archived(tmp_path):
    agent, source, _private, job, _actor, _scope = local_case(tmp_path)
    repo = agent.scheduler_repository
    run = repo.reserve_manual_run(job['job_id'])
    agent.scheduler_service.enqueue_ready_runs(now=run['dispatch_after'])
    claim = agent.scheduler_service.claim_wake(agent.conversation_store.wakes.pending()[0], now=run['dispatch_after']).claim
    repo.mark_run_running(run['run_id'], claim.claim_id)
    agent._current_run_params = RunParams(task_id=run['run_id'], request_id=run['run_id'],
        task_attributes={'conversation_thread_id': source.thread_id, 'conversation_task_id': run['run_id']})
    child = agent.subagents.create_run(goal='重启子任务', thought='测试', plan=['读资料'], parent_id=run['run_id'], root_id=run['run_id'],
        attributes={'conversation_task_id': run['run_id'], 'conversation_thread_id': source.thread_id})
    repo.delete_job(job['job_id'], expected_version=repo.get_job(job['job_id'])['version'])
    # 隔离原账本模拟已死亡出生身份；不启动、终止或修改真实进程。
    with repo._mutation_scope() as (ledger, admission):
        ledger['runs'][run['run_id']].update(runner_pid=2147483647, runner_start_time=None, claim_expires_at=1)
        repo._write_store_unlocked(ledger, admission)
    reopened = _agent(tmp_path)
    assert reopened.scheduler_repository.get_active_run(run['run_id']) is None
    assert any(row['run_id'] == run['run_id'] for row in reopened.scheduler_repository.pending_cancellation_runs())
    reopened.scheduler_service.enqueue_ready_runs()
    assert reopened.conversation_store.tasks.load(run['run_id']).status == 'cancelled'
    assert reopened.subagents.load(child.id).status == 'CANCELLED'
    history, errors = reopened.scheduler_repository.history(job_id=job['job_id'])
    assert not errors and history[0]['status'] == 'unknown'


@pytest.mark.parametrize('operation', ['delete', 'cancel'])
def test_malformed_active_run_never_reports_stopped_or_guesses_cancellation_scope(tmp_path, monkeypatch, operation):
    from agent_py_agent.agent.scheduler import control
    agent, _source, _private, job, actor, scope = local_case(tmp_path)
    repo = agent.scheduler_repository
    run = repo.reserve_manual_run(job['job_id'])
    claim = repo.claim_run(run['run_id'], lease_seconds=300, now=run['dispatch_after'])
    repo.mark_run_running(run['run_id'], claim['claim_id'])
    # 原隔离仓库注入坏 epoch；它无法成为可信停止身份，产品不得从其 PID/task 再猜范围。
    with repo._mutation_scope() as (ledger, admission):
        ledger['runs'][run['run_id']]['claim_epoch'] = -1
        repo._write_store_unlocked(ledger, admission)
    original = repo._store_snapshot()['runs'][run['run_id']]
    monkeypatch.setattr(control, 'cancel_scheduled_task', lambda *_: pytest.fail('坏 run 不能生成取消目标'))
    result = execute_schedule_control(agent, parse_conversation_control('/schedule ' + operation + ' ' + job['alias']), scope)
    assert result.ok
    assert result.details['cancellation_status'] == 'cancel_requested'
    assert result.details['load_error_codes'] == ['SCHEDULER_RUN_INVALID']
    assert result.details['cancellation'] == []
    assert '尚未确认' in result.message and '当前执行已停止' not in result.message
    assert repo._store_snapshot()['runs'][run['run_id']] == original
    if operation == 'delete':
        replay = execute_schedule_control(agent, parse_conversation_control('/schedule delete ' + job['alias']), scope)
        assert replay.ok and replay.details['cancellation_status'] == 'cancel_requested'
        assert repo.get_job(job['job_id'], include_deleted=True)['status'] == 'deleted'
    else:
        assert repo.get_job(job['job_id'])['status'] == 'active'
    with pytest.raises(SchedulerConflictError):
        control.delete_monitor(agent, repo, job['job_id'], 0, actor=actor)
