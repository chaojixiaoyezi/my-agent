# LLM: 真实仓库/配额/会话链路，模型和外发只用确定替身；不触真实账号或网络。
# 模块用途: 覆盖缺失来源兜底、认证目标及挂起对账有界写入。
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.user_space import owner_quota as quota


# LLM: 初始完整扫描后显式置脏，模拟已知权威计数但正在等待下一轮对账。
# 函数用途: 创建可重启的挂起对账夹具。
def quota_pending(root):
    for index in range(160):
        (root / str(index)).write_bytes(b'x')
    while not quota.reconcile_owner_quota_slice(root, max_entries=1024, max_ms=1000)['complete']:
        pass
    ledger = quota._read_quota_ledger(root)
    ledger['dirty_since'] = 1
    quota._write_quota_ledger(root, ledger)
    return ledger['used_bytes']


def test_other_callers_unchanged(tmp_path):
    quota_pending(tmp_path)
    with quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1000).admission() as admission:
        with pytest.raises(quota.OwnerQuotaUnavailable) as error:
            admission.check([quota.OwnerQuotaChange(tmp_path / 'x', 1)])
    assert error.value.reason_code == 'reconciliation_pending'
    assert quota.OwnerQuotaUnavailable('x').reason_code == ''


def test_reconciliation_pending_allows_bounded_scheduler_write(tmp_path):
    from agent_py_agent.agent.scheduler.repository import (
        SchedulerJobCreateRequest,
        SchedulerRepository,
    )
    repo = SchedulerRepository(tmp_path / 'scheduler', owner_provider='local', owner_kind='user', owner_id='test',
        quota_enforcer=quota.OwnerQuotaEnforcer(tmp_path, max_bytes=100000))
    base = quota_pending(tmp_path)
    job, _ = repo.create_job(SchedulerJobCreateRequest(name='合成', prompt='合成通知', thread_id='missing',
        schedule={'kind': 'every', 'every_seconds': 600, 'anchor_at': 1000, 'timezone': 'UTC'}, now=900))
    run = repo.reserve_due_runs(now=1000)[0]
    assert run['job_id'] == job['job_id']
    ledger = quota._read_quota_ledger(tmp_path)
    assert ledger['used_bytes'] == base + repo.store_path.stat().st_size
    assert ledger['dirty_since'] is not None and not ledger['reservations']
    while not (result := quota.reconcile_owner_quota_slice(tmp_path, max_entries=1024, max_ms=1000))['complete']:
        pass
    assert result['used_bytes'] == quota.owner_logical_usage_bytes(tmp_path, exact=True)


def test_reconciliation_pending_still_refuses_large_growth(tmp_path):
    quota_pending(tmp_path)
    with quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1000).admission() as admission:
        with pytest.raises(quota.OwnerQuotaUnavailable) as error:
            admission.check([quota.OwnerQuotaChange(tmp_path / 'x', 11)], bounded_control_growth_bytes=10)
    assert error.value.reason_code == 'reconciliation_pending'


def test_bounded_growth_is_cumulative_and_quota_is_hard(tmp_path):
    base = quota_pending(tmp_path)
    enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=base + 10)
    for index in range(2):
        path = tmp_path / f'new{index}'
        with enforcer.admission() as admission:
            projection = admission.check([quota.OwnerQuotaChange(path, 5)], bounded_control_growth_bytes=5)
            assert projection.projected_bytes == base + (index + 1) * 5
            path.write_bytes(b'12345')
    assert quota._read_quota_ledger(tmp_path)['used_bytes'] == base + 10
    with enforcer.admission() as admission:
        with pytest.raises(quota.OwnerQuotaExceeded):
            admission.check([quota.OwnerQuotaChange(tmp_path / 'overflow', 1)], bounded_control_growth_bytes=5)


def test_bounded_growth_requires_authoritative_ledger(tmp_path):
    for index in range(160):
        (tmp_path / str(index)).write_bytes(b'x')
    with quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1000).admission() as admission:
        with pytest.raises(quota.OwnerQuotaUnavailable):
            admission.check([quota.OwnerQuotaChange(tmp_path / 'unknown', 1)], bounded_control_growth_bytes=10)


# LLM: 原管理员认证账和当前ThreadStore生成binding_id；源线程与显式投递线程分开。
# 函数用途: 创建真实后台执行/运输替身及缺失来源计划。
def fallback_case(tmp_path, *, target=True, mode='isolated', group=True, im_creator=False):
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.scheduler.delivery_policy import available_delivery_targets
    from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
    from agent_py_agent.agent.user_space.identity_store import (
        ProviderIdentityRecord,
        link_provider_identity,
    )
    from agent_py_agent.tests.test_scheduler_runtime import _Backend, _RetryDelivery

    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / 'home'), enable_tools=False,
        memory_path='memory.jsonl', orphan_supervision_interval_seconds=0,
        **({'my_agent_owner_provider': 'feishu', 'my_agent_owner_kind': 'group', 'my_agent_owner_id': 'oc_synthetic'}
            if group else {})), tmp_path)
    agent.backend = _Backend()
    store = agent.conversation_store
    fields = {'owner_id': agent.home_paths.owner_id, 'owner_home': str(agent.home_paths.owner_home_dir),
        'canonical_user_id': agent.home_paths.owner_id}
    source = store.threads.get_or_create({**fields, 'channel': 'chat', 'channel_user_id': 'local',
        'channel_conversation_id': 'source'})
    if group:
        link_provider_identity(agent.home_paths, ProviderIdentityRecord(provider='feishu',
            provider_subject_id='oc_synthetic', owner_kind='group', owner_id='oc_synthetic'))
    else:
        bind_admin_channel_identity(agent.home_paths.root, 'feishu', 'ou_synthetic')
    destination = store.threads.get_or_create({**fields, 'channel': 'feishu', 'channel_user_id': 'ou_synthetic',
        'channel_conversation_id': 'oc_synthetic'})
    row = next(row for row in available_delivery_targets(agent) if row['channel'] == 'feishu')
    if im_creator:
        source = store.threads.get_or_create({**fields, 'channel': 'feishu', 'channel_user_id': 'ou_synthetic',
            'channel_conversation_id': 'oc_source_synthetic'})
    delivery = {'mode': 'bound', 'targets': [{'target_kind': 'channel_binding', 'binding_id': row['binding_id'],
        'channel': 'feishu', 'chat_id': 'oc_synthetic'}]} if target else {'mode': 'origin'}
    job, _ = agent.scheduler_repository.create_job(SchedulerJobCreateRequest(name='缺失源通知', prompt='合成检查',
        thread_id=source.thread_id, execution={'mode': mode}, delivery=delivery,
        creator={'channel': 'feishu', 'actor_id': 'ou_synthetic'} if im_creator else None,
        schedule={'kind': 'every', 'every_seconds': 600, 'anchor_at': 1000, 'timezone': 'UTC'}, now=900))
    store.storage.thread_path(source.thread_id).unlink()
    channels = _RetryDelivery()
    channels.status = 'sent'
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store, 'scheduler_service': agent.scheduler_service})
    return SimpleNamespace(agent=agent, store=store, source=source, destination=destination, channels=channels,
        scheduler=scheduler, job=job)


@pytest.mark.parametrize('mode', ['followup', 'isolated'])
def test_missing_thread_with_feishu_target_uses_fallback(tmp_path, mode):
    case = fallback_case(tmp_path, mode=mode)
    assert case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    signal = case.store.wakes.pending()[0]
    run = case.agent.scheduler_repository.get_active_run(signal.root_task_id)
    assert run['thread_fallback'] == {'reason': 'source_thread_unavailable',
        'original_thread_id': case.source.thread_id, 'fallback_thread_id': run['thread_id']}
    thread = case.store.threads.require(run['thread_id'])
    assert thread.owner_id == case.agent.home_paths.owner_id
    assert thread.owner_home == str(case.agent.home_paths.owner_home_dir)
    case.scheduler.tick(now=1000)
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]['status'] == 'done'
    assert case.channels.attempts[0][0].target == 'oc_synthetic'
    assert history[0]['thread_fallback'] == run['thread_fallback']


def test_missing_thread_without_target_still_unavailable(tmp_path):
    case = fallback_case(tmp_path, target=False)
    assert case.agent.scheduler_service.enqueue_ready_runs(now=1000) == []
    rows, errors = case.agent.scheduler_repository.history()
    assert not errors and rows[0]['error_code'] == 'SCHEDULER_THREAD_UNAVAILABLE'


def test_unverified_target_does_not_create_fallback(tmp_path):
    from agent_py_agent.agent.scheduler.delivery_policy import ScheduleDeliveryTargetUnauthorized
    from agent_py_agent.agent.user_space.admin_channel_identity import remove_admin_channel_identity
    case = fallback_case(tmp_path, group=False)
    remove_admin_channel_identity(case.agent.home_paths.root, 'feishu', 'ou_synthetic')
    before = list(case.store.storage.threads_dir.glob('*.json'))
    with pytest.raises(ScheduleDeliveryTargetUnauthorized):
        case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    assert list(case.store.storage.threads_dir.glob('*.json')) == before


def test_fallback_preparation_and_explicit_route_without_wake_publication(tmp_path):
    from agent_py_agent.agent.scheduler.execution import prepare_execution, resolve_delivery

    case = fallback_case(tmp_path)
    repo = case.agent.scheduler_repository
    run = repo.reserve_due_runs(now=1000)[0]
    run = prepare_execution(case.agent.scheduler_service, run, now=1000)
    assert prepare_execution(case.agent.scheduler_service, run, now=1001) == run
    request = SimpleNamespace(thread_id=run['thread_id'], task_id=run['run_id'], wake_signal=None)
    thread = case.store.threads.require(run['thread_id'])
    target_thread, channel, target = resolve_delivery(case.agent, thread, request, 'scheduler', '')
    assert (target_thread.thread_id, channel, target) == (case.destination.thread_id, 'feishu', 'oc_synthetic')
    assert run['source_thread_id'] == case.source.thread_id
    assert repo.get_job(case.job['job_id'])['thread_id'] == case.source.thread_id


# LLM: 缺源后仍经原 TaskLink/HostActor 恢复同一布置者，两种执行模式和 owner 类型都走真实后台入口。
# 函数用途: 验证认证兜底不把 IM 任务降成本地身份，也不重写原计划来源。
@pytest.mark.parametrize('mode', ['followup', 'isolated'])
@pytest.mark.parametrize('group', [False, True])
def test_missing_source_with_im_creator_keeps_frozen_identity(tmp_path, mode, group):
    from agent_py_agent.agent.conversation.host_actor import background_host_actor

    case = fallback_case(tmp_path, mode=mode, group=group, im_creator=True)
    assert case.job['creator'] == {'channel': 'feishu', 'actor_id': 'ou_synthetic'}
    assert case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    case.scheduler.tick(now=1000)
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]['status'] == 'done'
    run = history[0]
    assert run['creator'] == case.job['creator']
    actor = background_host_actor(case.agent, run['thread_id'], run['run_id'])
    assert actor is not None
    assert actor.owner_id == case.agent.home_paths.owner_id
    assert actor.source_thread_id == case.destination.thread_id
    assert actor.binding() == {'channel': 'feishu', 'conversation_id': 'oc_synthetic', 'actor_id': 'ou_synthetic'}
    assert run['source_thread_id'] == case.source.thread_id
    assert case.agent.scheduler_repository.get_job(case.job['job_id'])['thread_id'] == case.source.thread_id
    # 现有运输合同：个人 owner 投给原用户，群 owner 投给群；会话地址仍须保留，不能互换。
    assert len(case.channels.attempts) == 1
    delivery = case.channels.attempts[0][0]
    assert delivery.target == ('oc_synthetic' if group else 'ou_synthetic')
    assert delivery.conversation_id == 'oc_synthetic'


# LLM: 认证目标属于同 owner 不等于可借用其另一个账号；尚未发布 wake 时就必须拒绝 creator 不匹配。
# 函数用途: 阻止用可投递目标伪造已丢失来源的发起人。
def test_missing_source_with_other_creator_is_rejected_before_wake(tmp_path):
    from agent_py_agent.agent.conversation.host_actor import HostActorError
    from agent_py_agent.agent.scheduler.execution import prepare_execution

    case = fallback_case(tmp_path, group=False, im_creator=True)
    run = case.agent.scheduler_repository.reserve_due_runs(now=1000)[0]
    run = {**run, 'creator': {'channel': 'feishu', 'actor_id': 'ou_other'}}
    with pytest.raises(HostActorError):
        prepare_execution(case.agent.scheduler_service, run, now=1000)
    assert not case.store.wakes.pending() and not case.channels.attempts


# LLM: 原身份 resolver 必须复核冻结证明和现行认证；不因已有兜底线程而免验 owner、creator 或原来源。
# 函数用途: 验证改坏兜底证明及撤销认证都会拒绝，而不是返回本地空身份。
@pytest.mark.parametrize('corruption', ['owner', 'creator', 'creator_removed', 'original', 'fallback', 'binding', 'revoked'])
def test_fallback_actor_rejects_invalid_proof_or_revoked_auth(tmp_path, corruption):
    from copy import deepcopy

    from agent_py_agent.agent.conversation.host_actor import HostActorError
    from agent_py_agent.agent.scheduler.delivery_policy import ScheduleDeliveryTargetUnauthorized
    from agent_py_agent.agent.scheduler.execution import prepare_execution
    from agent_py_agent.agent.scheduler.execution_policy import scheduled_run_actor
    from agent_py_agent.agent.user_space.admin_channel_identity import remove_admin_channel_identity

    case = fallback_case(tmp_path, group=False, im_creator=True)
    run = case.agent.scheduler_repository.reserve_due_runs(now=1000)[0]
    run = deepcopy(prepare_execution(case.agent.scheduler_service, run, now=1000))
    if corruption == 'owner':
        run['owner']['id'] = 'local/user/other'
    elif corruption == 'creator':
        run['creator']['actor_id'] = 'ou_other'
    elif corruption == 'creator_removed':
        run['creator'] = None
    elif corruption == 'original':
        run['thread_fallback']['original_thread_id'] = case.destination.thread_id
    elif corruption == 'fallback':
        run['thread_fallback']['fallback_thread_id'] = case.destination.thread_id
    elif corruption == 'binding':
        for binding in run['execution_context']['origin_bindings']:
            binding['channel_conversation_id'] = 'oc_other'
    else:
        remove_admin_channel_identity(case.agent.home_paths.root, 'feishu', 'ou_synthetic')
    with pytest.raises((HostActorError, ScheduleDeliveryTargetUnauthorized)):
        scheduled_run_actor(case.agent.scheduler_service, run)
    assert not case.store.wakes.pending() and not case.channels.attempts

# LLM: 真目录漂移撤销旧权威但不能丢掉活跃对账；同epoch补正后恢复准确账本，始终不能绕过硬限额。
# 函数用途: 覆盖活跃对账期间两次有界增长拒绝及原扫描正常收口。
def test_active_pass_drift_rejects_repeated_bounded_growth_and_finishes(tmp_path):
    for number in range(40):
        directory = tmp_path / f'd{number:03d}'
        directory.mkdir()
        for index in range(10):
            (directory / f'{index:02d}').write_bytes(b'x')
    assert quota_rework_reconcile(tmp_path)['used_bytes'] == 400
    started = quota.reconcile_owner_quota_slice(tmp_path, max_entries=64, max_ms=5000)
    assert not started['complete']
    (tmp_path / 'd039/oob.bin').write_bytes(b'z' * 4096)
    target = tmp_path / 'd039/managed'
    enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=420)
    for _ in range(2):
        with enforcer.admission() as admission:
            with pytest.raises(quota.OwnerQuotaUnavailable) as error:
                admission.check([quota.OwnerQuotaChange(target, 1)], bounded_control_growth_bytes=10)
        assert error.value.reason_code == 'reconciliation_pending'
        ledger = quota._read_quota_ledger(tmp_path)
        assert ledger['dirty_since'] is not None and ledger['usage_authoritative'] is False
        assert ledger['reconcile_cursor']['epoch'] == started['cursor']['epoch']
    for _ in range(100):
        finished = quota.reconcile_owner_quota_slice(tmp_path, max_entries=64, max_ms=5000)
        if finished['complete']:
            break
    assert finished['complete'] and finished['cursor']['epoch'] == started['cursor']['epoch']
    assert finished['used_bytes'] == quota.owner_logical_usage_bytes(tmp_path, exact=True) == 4496
    ledger = quota._read_quota_ledger(tmp_path)
    assert ledger['dirty_since'] is None and ledger['usage_authoritative'] is True
    assert quota._verify_quota_metadata(tmp_path, finished['cursor'])
    with enforcer.admission() as admission:
        with pytest.raises(quota.OwnerQuotaExceeded):
            admission.check([quota.OwnerQuotaChange(target, 1)], bounded_control_growth_bytes=10)
    assert not target.exists()


# quota_rework_fragment_merged
"""真实大目录基线漂移的硬配额回归；独立交付供父级合并。"""
import json
import os
import subprocess
import sys

import pytest

from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.user_space import owner_quota as quota_rework


def quota_rework_reconcile(root):
    for _ in range(30):
        result = quota_rework.reconcile_owner_quota_slice(root, max_entries=1024, max_ms=1000)
        if result['complete']:
            return result
    pytest.fail('quota reconciliation did not converge')


def quota_rework_drift(root):
    # 真正超出前台128项预算，不改阈值、不伪造快照或基线判定。
    for index in range(160):
        (root / f'payload-{index:03d}').write_bytes(b'x')
    assert quota_rework_reconcile(root)['used_bytes'] == 160
    ledger = quota_rework._read_quota_ledger(root)
    assert ledger['dirty_since'] is None
    assert quota_rework._quota_baseline_exceeds(ledger, 128)
    assert quota_rework._verified_quota_baseline(root, ledger)
    (root / 'unmanaged-billed').write_bytes(b'z' * 64)
    assert quota_rework.owner_logical_usage_bytes(root, exact=True) == 224
    assert not quota_rework._verified_quota_baseline(root, quota_rework._read_quota_ledger(root))
    return quota_rework.OwnerQuotaEnforcer(root, max_bytes=170)


def quota_rework_assert_invalid(root):
    ledger = quota_rework._read_quota_ledger(root)
    assert ledger['dirty_since'] is not None
    assert ledger['reconcile_cursor']['metadata_invalidated'] is True
    assert ledger['reserved_bytes'] == 0 and not ledger['reservations']
    return ledger


@pytest.mark.parametrize('first_bound', [0, 10])
def test_quota_rework_failed_baseline_cannot_use_bounded_growth(tmp_path, monkeypatch, first_bound):
    enforcer = quota_rework_drift(tmp_path)
    target = tmp_path / 'scheduler-state'
    real_verify = quota_rework._verified_quota_baseline
    verified = []

    def observe_verify(*args, **kwargs):
        result = real_verify(*args, **kwargs)
        verified.append(result)
        return result

    real_snapshot = quota_rework._bounded_quota_snapshot
    snapshots = []

    def observe_snapshot(*args, **kwargs):
        snapshots.append(True)
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(quota_rework, '_verified_quota_baseline', observe_verify)
    monkeypatch.setattr(quota_rework, '_bounded_quota_snapshot', observe_snapshot)
    with enforcer.admission() as admission:
        with pytest.raises(quota_rework.OwnerQuotaUnavailable) as error:
            projection = admission.check([quota_rework.OwnerQuotaChange(target, 1)],
                                        bounded_control_growth_bytes=first_bound)
            # 原代码走到这里会真实写入并超硬配额；红测保留投影与精确实际用量。
            target.write_bytes(b's')
            print('UNSAFE_ADMITTED', projection,
                  'actual_bytes=', quota_rework.owner_logical_usage_bytes(tmp_path, exact=True))
        assert error.value.reason_code == 'reconciliation_pending'
        assert verified == [False]
        assert snapshots == []  # 首次核验走大基线，未做128项小快照。
        quota_rework_assert_invalid(tmp_path)  # 不依赖context退出才持久化。
        with pytest.raises(quota_rework.OwnerQuotaUnavailable):
            admission.check([quota_rework.OwnerQuotaChange(target, 1)], bounded_control_growth_bytes=10)
    assert not target.exists()
    quota_rework_assert_invalid(tmp_path)
    # 冷Python进程既无准入对象也无root-stamp缓存，不能把失效降格为普通pending。
    code = '''import json,sys
from pathlib import Path
from agent_py_agent.agent.user_space import owner_quota as q
root=Path(sys.argv[1])
try:
    with q.OwnerQuotaEnforcer(root,max_bytes=170).admission() as a:
        a.check([q.OwnerQuotaChange(root/'cold-state',1)],bounded_control_growth_bytes=10)
except q.OwnerQuotaUnavailable as exc:
    print(json.dumps({'reason_code':exc.reason_code,'invalidated':q._read_quota_ledger(root)['reconcile_cursor'].get('metadata_invalidated')}))
else:
    raise AssertionError('cold process trusted invalid baseline')
'''
    cold = subprocess.run([sys.executable, '-c', code, str(tmp_path)],
                          capture_output=True, text=True, env=os.environ.copy(), timeout=30)
    assert cold.returncode == 0, cold.stderr
    assert json.loads(cold.stdout) == {'reason_code': 'reconciliation_pending', 'invalidated': True}
    # 真正完整重扫后才能清除失效；此时用量已知超额，按硬quota而不是pending拒绝。
    assert quota_rework_reconcile(tmp_path)['used_bytes'] == 224
    assert quota_rework._read_quota_ledger(tmp_path)['dirty_since'] is None
    with enforcer.admission() as admission:
        with pytest.raises(quota_rework.OwnerQuotaExceeded):
            admission.check([quota_rework.OwnerQuotaChange(target, 1)], bounded_control_growth_bytes=10)


def test_quota_rework_non_growth_preserves_failed_baseline(tmp_path):
    enforcer = quota_rework_drift(tmp_path)
    target = tmp_path / 'payload-000'
    with enforcer.admission() as admission:
        admission.check([quota_rework.OwnerQuotaChange(target, 0)], bounded_control_growth_bytes=10)
        target.write_bytes(b'')
    ledger = quota_rework_assert_invalid(tmp_path)
    assert ledger['used_bytes'] == 159
    with enforcer.admission() as admission:
        with pytest.raises(quota_rework.OwnerQuotaUnavailable):
            admission.check([quota_rework.OwnerQuotaChange(tmp_path / 'later', 1)], bounded_control_growth_bytes=10)
    assert quota_rework_reconcile(tmp_path)['used_bytes'] == 223


@pytest.mark.parametrize('phase', ['pre', 'post'])
def test_quota_rework_failed_invalidation_write_retries_without_losing_fact(tmp_path, monkeypatch, phase):
    enforcer = quota_rework_drift(tmp_path)
    real_write = quota_rework._write_quota_ledger
    attempts = []

    def fail_once(root, ledger):
        attempts.append(ledger['reconcile_cursor'].get('metadata_invalidated'))
        if len(attempts) == 1:
            if phase == 'post':
                real_write(root, ledger)
            raise OSError('invalidation write fault')
        return real_write(root, ledger)

    monkeypatch.setattr(quota_rework, '_write_quota_ledger', fail_once)
    with enforcer.admission() as admission:
        with pytest.raises(OSError, match='invalidation write fault'):
            admission.check([quota_rework.OwnerQuotaChange(tmp_path / 'state', 1)], bounded_control_growth_bytes=10)
        assert attempts == [True, True]
        quota_rework_assert_invalid(tmp_path)
    quota_rework_assert_invalid(tmp_path)


def test_quota_rework_verification_cancellation_is_not_quota_failure(tmp_path, monkeypatch):
    enforcer = quota_rework_drift(tmp_path)

    def cancelled(*args, **kwargs):
        raise ToolCancelled('verification cancelled')

    monkeypatch.setattr(quota_rework, '_verify_quota_metadata', cancelled)
    with pytest.raises(ToolCancelled):
        with enforcer.admission() as admission:
            admission.check([quota_rework.OwnerQuotaChange(tmp_path / 'state', 1)], bounded_control_growth_bytes=10)
    assert not (tmp_path / 'state').exists()
