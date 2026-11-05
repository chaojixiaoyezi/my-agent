"""真实定时撤权后，只释放执行权，保留 UNKNOWN/DIRTY 与原取消证明。"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from threading import Lock

import pytest

from agent_py_agent.agent.common.heartbeat import process_start_time
from agent_py_agent.agent.conversation import BackgroundMainAgentScheduler
from agent_py_agent.agent.runtime_db.operations import attempt_status_is_terminal, exec_lock_scope
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.scheduler.control import cancel_monitor
from agent_py_agent.agent.workflows import script_runner
from agent_py_agent.tests.test_v15_rework_monitor_pool_interrupt import (
    _assert_done,
    _assert_occupied,
    _await_step,
    _fd_count,
    _join,
    _schedule,
    _start,
)
from agent_py_agent.tests.test_v15_rework_monitor_pool_interrupt import case as case


def _rows(repo, attempt_id):
    with repo.transaction() as conn:
        return {table: [dict(row) for row in conn.execute(
            f'SELECT * FROM {table} WHERE attempt_id = ? ORDER BY rowid', (attempt_id,))]
            for table in ('tool_operations', 'resource_locks', 'resource_mutations', 'runtime_events')}


def _assert_unknown_dirty(repo, attempt_id, before, reason):
    rows = _rows(repo, attempt_id)
    assert not rows['resource_locks']
    assert len(rows['tool_operations']) == len(before['tool_operations']) == 1
    operation = rows['tool_operations'][0]
    original = before['tool_operations'][0]
    assert original['status'] == 'EXECUTING' and original['handler_started_at'] > 0
    assert operation['status'] == 'UNKNOWN'
    assert operation['operation_id'] == original['operation_id']
    assert operation['handler_started_at'] == original['handler_started_at']
    expected = {**json.loads(original['outcome_json']), 'reason': reason, 'unknown_reason': reason}
    assert json.loads(operation['outcome_json']) == expected
    assert len(rows['resource_mutations']) == len(before['resource_mutations']) == 1
    mutation = rows['resource_mutations'][0]
    assert mutation['state'] == 'DIRTY' and mutation['dirty_reason'] == reason
    assert mutation['mutation_id'] == before['resource_mutations'][0]['mutation_id']
    assert attempt_status_is_terminal(repo.get_attempt(attempt_id)['status'])
    return rows


@pytest.mark.parametrize('release_error', [False, True], ids=['worker_exit', 'lease_backstop'])
def test_cancelled_pool_attempt_releases_without_replaying_and_fresh_run_completes(case, monkeypatch, release_error):
    warm = _schedule(case, 'warm')
    _join(*_start(case, warm))
    _assert_done(case, warm)
    fd_before = _fd_count()
    repo = case.agent.subagents.runtime_db
    execute = script_runner._execute
    guard, timing = Lock(), {'active': 0, 'peak': 0, 'starts': {}, 'ends': {}}

    # 只观察并委托原执行器，时间来自真实进程入口和回收出口。
    def observed_process(command, folder, environment, input_json, timeout, capture):
        label = json.loads(input_json)['run_label']
        with guard:
            timing['active'] += 1
            timing['peak'] = max(timing['peak'], timing['active'])
            timing['starts'].setdefault(label, []).append(time.monotonic())
        try:
            return execute(command, folder, environment, input_json, timeout, capture)
        finally:
            with guard:
                timing['ends'].setdefault(label, []).append(time.monotonic())
                timing['active'] -= 1

    monkeypatch.setattr(script_runner, '_execute', observed_process)
    failures = []
    if release_error:
        release = repo.release_terminal_attempt_operations

        def fail_once(attempt_id, *, reason, now=None):
            if not failures:
                failures.append(attempt_id)
                raise sqlite3.OperationalError('simulated release I/O error')
            return release(attempt_id, reason=reason, now=now)

        monkeypatch.setattr(repo, 'release_terminal_attempt_operations', fail_once)

    stopped = _schedule(case, 'stopped')
    thread, errors = _start(case, stopped)
    _await_step(stopped, thread, errors)
    binding = case.calls[-1][2]
    _assert_occupied(case, stopped, binding)
    run = repo.main_agent_run_for_task(stopped.signal.root_task_id)
    assert run is not None
    attempt_id = str(run['current_attempt_id'])
    before = _rows(repo, attempt_id)
    assert before['tool_operations'][0]['status'] == 'EXECUTING'
    assert before['resource_locks'] and before['resource_mutations'][0]['state'] == 'MUTATING'
    job = case.repo.get_job(stopped.job['job_id'])
    receipt = cancel_monitor(case.agent, case.repo, job['job_id'], job['version'])
    assert receipt['cancellation_status'] == 'cancel_requested'
    # 接受撤权不等于 worker 退出；当前工具锁不能提前消失。
    _assert_occupied(case, stopped, binding)
    in_flight = _rows(repo, attempt_id)
    assert in_flight['resource_locks']
    assert repo.get_agent_run_exec_lock(str(run['agent_run_id'])) is None
    _join(thread, errors)
    worker_exited_at = time.monotonic()
    assert case.store.tasks.load(stopped.signal.root_task_id).status == 'cancelled'
    assert case.repo.history(job_id=stopped.job['job_id']) == ([], [])
    assert len(timing['starts']['stopped']) == len(timing['ends']['stopped']) == 1

    if release_error:
        assert failures == [attempt_id]
        retained = _rows(repo, attempt_id)
        assert retained['tool_operations'][0]['status'] == 'EXECUTING'
        assert len(retained['resource_locks']) == 1
        lock = retained['resource_locks'][0]
        expiry = float(lock['lease_expires_at'])
        early = repo.cleanup_stale_runtime(now=expiry - 1)
        assert early['released_locks'] == []
        assert _rows(repo, attempt_id) == retained
        expired = repo.cleanup_stale_runtime(now=expiry + 1)
        assert expired['released_locks'] == [lock['lock_id']]
        released = _assert_unknown_dirty(repo, attempt_id, before, 'terminal_attempt_lease_expired')
    else:
        reason = 'scheduled_target_interrupted_after_authority_close'
        released = _assert_unknown_dirty(repo, attempt_id, before, reason)
    reason = 'terminal_attempt_lease_expired' if release_error else 'scheduled_target_interrupted_after_authority_close'
    events = [row for row in released['runtime_events']
        if row['event_type'] == 'agent_attempt.terminal_resources_released']
    assert len(events) == 1 and events[0]['agent_run_id'] == run['agent_run_id']
    assert json.loads(events[0]['payload_json']) == {
        'operation_ids': [before['tool_operations'][0]['operation_id']],
        'lock_ids': [row['lock_id'] for row in in_flight['resource_locks']], 'reason': reason}
    assert repo.release_terminal_attempt_operations(attempt_id, reason='repeat') == []
    assert _rows(repo, attempt_id) == released, '已确认清理不得重复写 UNKNOWN/DIRTY 或事件'

    # 不处理原 wake，不改操作账；新的真实运行只在原 worker 已退出之后开始。
    fresh = _schedule(case, 'fresh')
    case.scheduler = BackgroundMainAgentScheduler({'runtime': case.scheduler.runtime,
        'store': case.store, 'scheduler_service': case.service})
    _join(*_start(case, fresh, thread_only=True))
    _assert_done(case, fresh)
    assert timing['ends']['stopped'][0] <= worker_exited_at < timing['starts']['fresh'][0]
    assert timing['peak'] == 1 and timing['active'] == 0
    assert len(timing['starts']['stopped']) == 1 and len(timing['starts']['fresh']) == 3
    after_fresh = _rows(repo, attempt_id)
    assert after_fresh['tool_operations'] == released['tool_operations']
    assert after_fresh['runtime_events'] == released['runtime_events']
    assert not after_fresh['resource_locks']
    # mutation 是按 scope 唯一的现态，新运行成功后沿原结算确认 STABLE；旧 UNKNOWN 和事件仍保留。
    mutation = repo.mutation_for_scope(released['resource_mutations'][0]['canonical_scope'])
    fresh_run = repo.main_agent_run_for_task(fresh.signal.root_task_id)
    assert mutation is not None and fresh_run is not None
    assert mutation['state'] == 'STABLE' and mutation['attempt_id'] == fresh_run['current_attempt_id']
    assert case.store.tasks.load(stopped.signal.root_task_id).status == 'cancelled'
    assert _fd_count() - fd_before == 0


@pytest.mark.parametrize('status', ['running', 'unknown'])
def test_terminal_release_keeps_running_unknown_and_missing_attempts_unchanged(tmp_path, status):
    repo = RuntimeRepository(tmp_path / 'runtime.db')
    chain = repo.record_run_creation(owner_id='local/main', run_id='running', goal='guard')
    attempt_id = chain['attempt_id']
    operation = repo.create_tool_operation(agent_run_id=chain['agent_run_id'], attempt_id=attempt_id,
        operation_type='write', canonical_scope='owner:guard')
    repo.mark_operation_executing(operation['operation_id'])
    repo.begin_mutation(canonical_scope='owner:guard', attempt_id=attempt_id)
    if status == 'unknown':
        assert repo._mark_attempt_unknown(attempt_id, chain['agent_run_id'], reason='guard', operator='test')
    before = _rows(repo, attempt_id)
    assert repo.get_attempt(attempt_id)['status'] == status
    fd_before = _fd_count()
    assert repo.release_terminal_attempt_operations(attempt_id, reason='too_early') == []
    assert repo.release_terminal_attempt_operations('missing', reason='missing') == []
    assert _rows(repo, attempt_id) == before
    assert repo.cleanup_stale_resource_locks(now=time.time() + 3600) == []
    assert _rows(repo, attempt_id) == before
    assert _fd_count() - fd_before == 0


def test_backstop_waits_for_all_attempt_leases_and_keeps_confirmed_mutations(tmp_path):
    repo = RuntimeRepository(tmp_path / 'runtime.db')
    chain = repo.record_run_creation(owner_id='local/main', run_id='mixed_leases', goal='lease guard')
    attempt_id = chain['attempt_id']
    run = repo.get_agent_run(chain['agent_run_id'])
    attempt = repo.get_attempt(attempt_id)
    locks = []
    for scope, duration in [('owner:short', 10), ('owner:long', 60)]:
        operation = repo.create_tool_operation(agent_run_id=chain['agent_run_id'],
            attempt_id=attempt_id, operation_type='write', canonical_scope=scope)
        repo.mark_operation_executing(operation['operation_id'])
        locks.extend(repo.acquire_locks([scope], holder_instance=repo.instance_id,
            attempt_id=attempt_id, attempt_generation=attempt['attempt_generation'],
            workspace_epoch=run['workspace_epoch'], tool_operation_generation=operation['tool_operation_generation'],
            pid=os.getpid(), start_token=process_start_time(os.getpid()), lease_seconds=duration))
        repo.begin_mutation(canonical_scope=scope, attempt_id=attempt_id)
    repo.begin_mutation(canonical_scope='owner:stable', attempt_id=attempt_id)
    stable = dict(repo.mark_mutation_stable('owner:stable'))
    dirty = dict(repo.mark_mutation_dirty(canonical_scope='owner:dirty', reason='original_dirty', attempt_id=attempt_id))
    before = _rows(repo, attempt_id)
    fd_before = _fd_count()
    # 活 attempt 即使所有租约都过时，也不能仅凭时间清理，原 exec 锁仍在。
    assert repo.cleanup_stale_resource_locks(now=max(lock['lease_expires_at'] for lock in locks) + 1) == []
    assert _rows(repo, attempt_id) == before
    closed = repo.settle_agent_run(agent_run_id=chain['agent_run_id'], attempt_id=attempt_id, status='cancelled')
    assert closed['settled'] and attempt_status_is_terminal(repo.get_attempt(attempt_id)['status'])
    assert repo.get_agent_run_exec_lock(chain['agent_run_id']) is None
    retained = _rows(repo, attempt_id)
    assert len(retained['resource_locks']) == 2
    # 第一把到期不能把仍有效的第二把连带清掉，更不能提前把它的 operation 改成 UNKNOWN。
    assert repo.cleanup_stale_resource_locks(now=locks[0]['lease_expires_at'] + 1, bounded=True) == []
    assert _rows(repo, attempt_id) == retained
    released = repo.cleanup_stale_resource_locks(now=locks[1]['lease_expires_at'] + 1, bounded=True)
    assert set(released) == {lock['lock_id'] for lock in locks}
    after = _rows(repo, attempt_id)
    assert not after['resource_locks']
    assert [row['status'] for row in after['tool_operations']] == ['UNKNOWN', 'UNKNOWN']
    for scope in ('owner:short', 'owner:long'):
        mutation = repo.mutation_for_scope(scope)
        assert mutation['state'] == 'DIRTY' and mutation['dirty_reason'] == 'terminal_attempt_lease_expired'
    assert dict(repo.mutation_for_scope('owner:stable')) == stable
    assert dict(repo.mutation_for_scope('owner:dirty')) == dirty
    assert _fd_count() - fd_before == 0


def test_terminal_release_never_deletes_execution_lock_and_rolls_back_on_event_error(tmp_path, monkeypatch):
    repo = RuntimeRepository(tmp_path / 'runtime.db')
    chain = repo.record_run_creation(owner_id='local/main', run_id='exec_guard', goal='exec guard')
    attempt_id = chain['attempt_id']
    operation = repo.create_tool_operation(agent_run_id=chain['agent_run_id'], attempt_id=attempt_id,
        operation_type='write', canonical_scope='owner:exec_guard')
    repo.mark_operation_executing(operation['operation_id'])
    repo.begin_mutation(canonical_scope='owner:exec_guard', attempt_id=attempt_id)
    assert repo.settle_agent_run(agent_run_id=chain['agent_run_id'], attempt_id=attempt_id,
        status='cancelled')['settled']
    attempt, run = repo.get_attempt(attempt_id), repo.get_agent_run(chain['agent_run_id'])
    scopes = [exec_lock_scope(chain['agent_run_id']), 'owner:exec_guard']
    locks = repo.acquire_locks(scopes, holder_instance=repo.instance_id, attempt_id=attempt_id,
        attempt_generation=attempt['attempt_generation'], workspace_epoch=run['workspace_epoch'],
        tool_operation_generation=operation['tool_operation_generation'], pid=os.getpid(),
        start_token=process_start_time(os.getpid()), lease_seconds=60)
    before = _rows(repo, attempt_id)
    append = repo._append_event_conn

    def fail_event(*args, **kwargs):
        raise sqlite3.OperationalError('simulated event I/O error')

    fd_before = _fd_count()
    with monkeypatch.context() as patch:
        patch.setattr(repo, '_append_event_conn', fail_event)
        with pytest.raises(sqlite3.OperationalError, match='simulated event'):
            repo.release_terminal_attempt_operations(attempt_id, reason='event_error')
    assert _rows(repo, attempt_id) == before
    assert _fd_count() - fd_before == 0
    assert repo._append_event_conn == append
    assert repo.release_terminal_attempt_operations(attempt_id, reason='worker_exited') == [operation['operation_id']]
    after = _rows(repo, attempt_id)
    assert after['resource_locks'] == [row for row in before['resource_locks']
        if row['canonical_scope'] == scopes[0]]
    assert after['tool_operations'][0]['status'] == 'UNKNOWN'
    assert after['resource_mutations'][0]['state'] == 'DIRTY'
    events = [row for row in after['runtime_events']
        if row['event_type'] == 'agent_attempt.terminal_resources_released']
    assert len(events) == 1
    assert json.loads(events[0]['payload_json'])['lock_ids'] == [row['lock_id'] for row in locks
        if row['canonical_scope'] == scopes[1]]
    assert repo.get_attempt(attempt_id)['status'] == repo.get_agent_run(chain['agent_run_id'])['status'] == 'cancelled'
    assert repo.release_terminal_attempt_operations(attempt_id, reason='repeat') == []
    assert _rows(repo, attempt_id) == after
    assert _fd_count() - fd_before == 0
