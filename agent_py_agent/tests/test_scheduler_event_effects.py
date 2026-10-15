"""事件监控取消屏障：原Agent/Store/后台运行，仅模型与外发运输替身，零网络。"""
from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_background_public_receipt_contract import _body
from agent_py_agent.tests.test_scheduler_runtime import _Backend, _RetryDelivery


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    def forbidden(*_args, **_kwargs):
        pytest.fail('取消屏障探针禁止网络')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


def _case(tmp_path):
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / 'home'), enable_tools=False,
        my_agent_owner_provider='feishu', my_agent_owner_kind='group', my_agent_owner_id='oc_a',
        memory_path='memory.jsonl', orphan_supervision_interval_seconds=0), tmp_path)
    backend = _Backend()
    def generate(prompt, *_args, **_kwargs):
        backend.prompts.append(prompt)
        return ModelResponse(text=_body(public='synthetic public reply'), backend='scheduler-test')
    backend.generate = generate
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create({'canonical_user_id': 'ou_a', 'channel': 'feishu',
        'channel_user_id': 'ou_a', 'channel_conversation_id': 'oc_a', 'owner_id': agent.home_paths.owner_id,
        'owner_home': str(agent.home_paths.owner_home_dir), 'cwd': str(tmp_path), 'now': 900})
    source = {'channel': 'feishu', 'event_type': 'message', 'app_id': 'cli_receiver', 'tenant_key': 'tenant_a',
        'chat_id': 'oc_a', 'sender_type': 'bot', 'sender_id_type': 'app_id', 'sender_ids': ['cli_source'],
        'proofs': [{'sender_id': 'cli_source', 'source_message_id': 'om_source'}]}
    repo = agent.scheduler_repository
    job = repo.create_job(SchedulerJobCreateRequest(name='synthetic', prompt='synthetic',
        thread_id=thread.thread_id, schedule={'kind': 'event_source', 'source': source},
        creator={'channel': 'feishu', 'actor_id': 'ou_a'},
        delivery={'mode': 'origin', 'reply_policy': 'explicit'}, now=900))[0]
    event = {k: source[k] for k in ('channel', 'event_type', 'app_id', 'tenant_key', 'chat_id', 'sender_type', 'sender_id_type')}
    event.update(sender_id='cli_source', message_id='om_event', ingress_id='ingress_event', content='synthetic', created_at=901)
    assert repo.accept_source_event(event, now=1000)['status'] == 'accepted'
    agent.scheduler_service.enqueue_ready_runs(now=1000)
    channels = _RetryDelivery()
    channels.status = 'sent'
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store, 'scheduler_service': agent.scheduler_service})
    return SimpleNamespace(agent=agent, repo=repo, job=job, backend=backend, channels=channels, scheduler=scheduler)


def _pause(case):
    current = case.repo.get_job(case.job['job_id'])
    case.repo.pause_job(current['job_id'], expected_version=current['version'], now=1002)


@pytest.mark.parametrize('timing', ['before_tick', 'after_mark_running'])
def test_E09_pause_committed_before_model_backend_entry_blocks_sampling(tmp_path, monkeypatch, timing):
    case = _case(tmp_path)
    if timing == 'before_tick':
        _pause(case)
    else:
        original = case.repo.mark_run_running
        def mark_then_pause(*args, **kwargs):
            result = original(*args, **kwargs)
            _pause(case)
            return result
        monkeypatch.setattr(case.repo, 'mark_run_running', mark_then_pause)
    from agent_py_agent.agent.contracts.background_public_receipt import (
        BackgroundPublicReceiptError,
    )
    try:
        case.scheduler.tick(now=1003)
    except BackgroundPublicReceiptError:
        # 合成backend故意只给普通文字，晚收口失败不能掩盖模型已被调用的安全断言。
        pass
    assert len(case.backend.prompts) == 0, '取消已提交且模型尚未开始，不得再调用模型backend'
    assert case.channels.attempts == []


@pytest.mark.parametrize('timing', ['before_origin_validation', 'after_origin_validation'])
def test_E11_pause_committed_before_transport_entry_blocks_send(tmp_path, monkeypatch, timing):
    from agent_py_agent.agent.scheduler import execution

    case = _case(tmp_path)
    original = execution.validate_delivery_origin
    called = []
    def validate(agent, request, context):
        if timing == 'before_origin_validation':
            _pause(case)
            return original(agent, request, context)
        valid = original(agent, request, context)
        assert valid and case.channels.attempts == []
        _pause(case)
        called.append(True)
        return valid
    monkeypatch.setattr(execution, 'validate_delivery_origin', validate)
    case.scheduler.tick(now=1003)
    assert len(case.backend.prompts) == 1
    if timing == 'after_origin_validation':
        assert called == [True]
    assert case.channels.attempts == [], '取消已提交且运输尚未开始，不得再进入发送运输'


def _running(case):
    run = case.repo.active_runs_by_job()[0][case.job['job_id']]
    claim = case.repo.claim_run(run['run_id'], lease_seconds=30, now=1001)
    case.repo.mark_run_running(run['run_id'], claim['claim_id'], now=1001)
    return case.repo.get_active_run(run['run_id'])


def _effects(case):
    return case.repo.get_job(case.job['job_id'], include_deleted=True)['event_source_state'][''].get('active_effects', {})


def test_begin_wins_then_pause_preserves_inflight_until_same_nonce_finishes(tmp_path):
    from agent_py_agent.agent.scheduler.repository import SchedulerConflictError

    case = _case(tmp_path)
    run = _running(case)
    lease = case.repo.begin_event_effect(run, 'original-call', kind='model')
    _pause(case)
    assert _effects(case)['model:original-call']['lease_id'] == lease['lease_id']
    assert case.repo.pending_cancellation_runs()[0]['run_id'] == run['run_id']
    with pytest.raises(SchedulerConflictError):
        case.repo.begin_event_effect(run, 'late-call', kind='model')
    case.repo.finish_event_effect({**lease, 'lease_id': 'wrong-nonce'})
    assert len(_effects(case)) == 1
    case.repo.finish_event_effect(lease)
    assert _effects(case) == {}
    # 移除开始租约不等于运输或整个run已经撤回/静止，原取消事实继续保留。
    assert case.repo.pending_cancellation_runs()[0]['run_id'] == run['run_id']


def test_archived_run_retry_uses_original_history_without_resurrection(tmp_path):
    from agent_py_agent.agent.scheduler.repository import SchedulerConflictError, SchedulerRunFinish

    case = _case(tmp_path)
    run = _running(case)
    terminal = case.repo.finish_run(run['run_id'], run['claim_id'], SchedulerRunFinish(status='done', now=1002))
    history = case.repo.history_path.read_bytes()
    with pytest.raises(SchedulerConflictError):
        case.repo.begin_event_effect(terminal, 'frozen-attempt', kind='delivery')
    lease = case.repo.begin_event_effect(terminal, 'frozen-attempt', kind='delivery', allow_terminal=True)
    assert case.repo.get_active_run(run['run_id']) is None
    assert case.repo.history_path.read_bytes() == history
    case.repo.finish_event_effect(lease)
    _pause(case)
    with pytest.raises(SchedulerConflictError):
        case.repo.begin_event_effect(terminal, 'late-frozen-attempt', kind='delivery', allow_terminal=True)
    assert case.repo.history_path.read_bytes() == history


@pytest.mark.parametrize('paused,external_sent,transport_calls,status', [
    (False, False, 1, 'sent'), (True, False, 0, 'failed'), (True, True, 0, 'sent')])
def test_archived_frozen_transport_keeps_external_sent_authority(tmp_path, paused, external_sent, transport_calls, status):
    from agent_py_agent.agent.conversation.background_delivery import (
        FrozenOwnerDelivery,
        _background_external_delivery,
        _ExternalDeliveryOptions,
    )
    from agent_py_agent.agent.conversation.channels import DeliveryContext, ReplyEnvelope
    from agent_py_agent.agent.scheduler.repository import SchedulerRunFinish

    case = _case(tmp_path)
    run = _running(case)
    case.repo.finish_run(run['run_id'], run['claim_id'], SchedulerRunFinish(status='done', now=1002))
    if paused:
        _pause(case)
    channels = Mock()
    channels.deliver.return_value = SimpleNamespace(delivery_status='sent')
    frozen = FrozenOwnerDelivery(content='原冻结正文', external_sent=external_sent, receipt_id='original-receipt')
    context = DeliveryContext(channel='feishu', target='oc_a', task_id=run['run_id'],
        thread_id=run['thread_id'], idempotency_key='original-frozen-attempt')
    history = case.repo.history_path.read_bytes()
    receipt, actual = _background_external_delivery(SimpleNamespace(agent=case.agent, channels=channels), context,
        ReplyEnvelope(content='原冻结正文'), _ExternalDeliveryOptions(frozen_retry=frozen, local_transcript_only=False))
    assert channels.deliver.call_count == transport_calls and actual == status
    if external_sent:
        assert receipt.receipt_id == 'original-receipt' and receipt.replayed is True
    assert case.repo.get_active_run(run['run_id']) is None and case.repo.history_path.read_bytes() == history


@pytest.mark.parametrize('liveness', ['dead', 'alive', 'unverifiable'])
def test_full_prior_run_leases_only_reclaimed_with_original_death_proof(tmp_path, monkeypatch, liveness):
    from agent_py_agent.agent.scheduler import repository as repository_module
    from agent_py_agent.agent.scheduler.event_effects import MAX_ACTIVE_EVENT_EFFECTS
    from agent_py_agent.agent.scheduler.repository import SchedulerConflictError, SchedulerRunFinish

    case = _case(tmp_path)
    old = _running(case)
    first = case.repo.begin_event_effect(old, 'old-operation', kind='delivery')
    case.repo.finish_run(old['run_id'], old['claim_id'], SchedulerRunFinish(status='unknown', now=1002))
    # 同原store模拟kill遗留：旧run已终态、epoch过期，本身仍不能证明进程内的运输已结束。
    store = json.loads(case.repo.store_path.read_text())
    state = store['jobs'][case.job['job_id']]['event_source_state']['']
    row = state['active_effects'][first['operation_key']]
    state['active_effects'] = {f'delivery:old-{i}': {**row, 'lease_id': str(i), 'runner_pid': 99999999}
        for i in range(MAX_ACTIVE_EVENT_EFFECTS)}
    case.repo.store_path.write_text(json.dumps(store))
    event = deepcopy(old['event_batch'][0])
    event.update(message_id='om_next', ingress_id='ingress_next', created_at=1061)
    assert case.repo.accept_source_event(event, now=1061)['status'] == 'accepted'
    next_run = case.repo.reserve_event_runs(now=1061)[0]
    claim = case.repo.claim_run(next_run['run_id'], lease_seconds=30, now=1062)
    case.repo.mark_run_running(next_run['run_id'], claim['claim_id'], now=1062)
    candidate = case.repo.get_active_run(next_run['run_id'])
    monkeypatch.setattr(repository_module, '_runner_liveness', lambda _row: liveness)
    history = case.repo.history_path.read_bytes()
    if liveness == 'dead':
        lease = case.repo.begin_event_effect(candidate, 'new-operation', kind='model')
        assert list(_effects(case)) == ['model:new-operation']
        case.repo.finish_event_effect(lease)
    else:
        with pytest.raises(SchedulerConflictError):
            case.repo.begin_event_effect(candidate, 'new-operation', kind='model')
        assert len(_effects(case)) == MAX_ACTIVE_EVENT_EFFECTS
    assert case.repo.history_path.read_bytes() == history
    assert case.repo.get_active_run(old['run_id']) is None


@pytest.mark.parametrize('pause', [False, True])
def test_event_subagent_explicit_send_uses_original_root_and_actual_transport_boundary(tmp_path, pause):
    from agent_py_agent.agent.capability.channel_message_tool import _deliver_with_scheduler_scope
    from agent_py_agent.agent.conversation.channels import DeliveryContext, ReplyEnvelope
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )
    from agent_py_agent.agent.subagents.models import SubAgentTask

    case = _case(tmp_path)
    run = _running(case)
    child = SubAgentTask(id='child-original', goal='合成任务', thought='', plan=[], root_id=run['run_id'], parent_id=run['run_id'])
    case.agent.subagents.save(child)
    if pause:
        _pause(case)
    delivery = Mock()
    delivery.deliver.return_value = SimpleNamespace(status='sent')
    # 模型自报另一个task不能覆盖真实runner和原SubAgentTask.root_id。
    context = DeliveryContext(channel='feishu', target='oc_a', task_id='fake-foreground', idempotency_key='original-tool-call')
    previous = set_current_subagent_context(case.agent, run_id=child.id)
    try:
        result = _deliver_with_scheduler_scope(case.agent, delivery, context, ReplyEnvelope(content='合成回复'))
    finally:
        restore_current_subagent_context(case.agent, previous)
    assert delivery.deliver.call_count == (0 if pause else 1)
    if pause:
        assert result.error_code == 'SCHEDULER_EVENT_EFFECT_RETIRED' and result.effect_outcome == 'not_started'
    assert _effects(case) == {}


def test_ordinary_task_scope_does_not_read_or_write_scheduler():
    from agent_py_agent.agent.scheduler.effect_scope import scheduler_effect_scope

    repository = Mock()
    agent = SimpleNamespace(scheduler_repository=repository)
    with scheduler_effect_scope(agent, 'ordinary-task', 'original-operation', kind='model'):
        pass
    repository.assert_not_called()
    assert repository.mock_calls == []


def test_existing_timed_run_scope_reads_only_original_run():
    from agent_py_agent.agent.scheduler.effect_scope import scheduler_effect_scope

    repository = Mock()
    repository.effect_run.return_value = {'run_id': 'srun_timed', 'event_source': None}
    with scheduler_effect_scope(SimpleNamespace(scheduler_repository=repository), 'srun_timed', 'original-call', kind='model'):
        pass
    repository.effect_run.assert_called_once_with('srun_timed')
    repository.begin_event_effect.assert_not_called()
    repository.finish_event_effect.assert_not_called()


@pytest.mark.parametrize('source', ['missing_repository', 'missing_run', 'unreadable_run'])
def test_scheduler_source_cannot_silently_become_foreground_when_unavailable(source):
    from agent_py_agent.agent.scheduler.effect_scope import (
        SchedulerEffectRejected,
        scheduler_effect_scope,
    )

    repository = Mock()
    repository.effect_run.return_value = None
    if source == 'unreadable_run':
        repository.effect_run.side_effect = OSError('synthetic')
    agent = SimpleNamespace(scheduler_repository=None if source == 'missing_repository' else repository)
    with pytest.raises(SchedulerEffectRejected, match='SCHEDULER_EFFECT_SOURCE_UNAVAILABLE'):
        with scheduler_effect_scope(agent, 'srun_original', 'original-call', kind='model'):
            pytest.fail('来源未核验不能开始副作用')
