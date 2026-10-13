"""真实 manager/RuntimeDB/session；只验证宿主已退出片段的权利释放，不把等待当整个任务完成。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runner.session_pool import (
    RunnerSessionPoolLease,
    runner_session_lease,
)
from agent_py_agent.agent.agent_core.runner.slice_closeout import settle_resumable_worker_slice
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams


@pytest.mark.parametrize('status', ['BLOCKED', 'PENDING'])
def test_only_exited_slice_releases_exact_attempt_and_can_queue_immediate_continuation(tmp_path, status):
    manager = SubAgentManager(tmp_path/'agents', owner_id='providers/feishu/users/ou_test', owner_home_dir=str(tmp_path/'owner'))
    task = manager.create_run(goal='合成等待权限', thought='', plan=['等待'])
    task = manager.lifecycle.prepare_runner_attempt(task.id)
    attempt_id = task.runner_active_attempt_id
    worker = SimpleNamespace(subagents=manager)
    with runner_session_lease(RunnerSessionPoolLease(manager, task.id, attempt_id=attempt_id)):
        result = manager.runner_result.record_runner_result(RecordRunnerResultParams(run_id=task.id, attempt_id=attempt_id,
            dry_run=False, ok=False, status=status, message='合成待续片段', turn_end_reason='blocked'))
        settle_resumable_worker_slice(worker, task.id, result, attempt_id=attempt_id, dry_run=False)
        assert manager.runtime_db.get_attempt(attempt_id)['status'] == 'running'
    # 无需等待进程死亡或孤儿超时，逻辑任务仍非终态。
    settle_resumable_worker_slice(worker, task.id, result, attempt_id=attempt_id, dry_run=False)
    authority = manager.runtime_db.agent_run_for_run_id(task.id)
    assert authority['status'] == 'created'
    assert manager.runtime_db.get_attempt(attempt_id)['status'] == 'done'
    assert manager.runtime_db.get_attempt(attempt_id)['ended_at'] > 0
    assert not manager.runtime_db.has_active_exec_lock(authority['agent_run_id'])
    next_attempt = manager.runtime_db.queue_pending_attempt(authority['agent_run_id'], source='synthetic-capability-granted')
    assert next_attempt['attempt_id'] != attempt_id and next_attempt['status'] == 'pending'
    settle_resumable_worker_slice(worker, task.id, result, attempt_id=attempt_id, dry_run=False)
    assert manager.runtime_db.get_attempt(next_attempt['attempt_id'])['status'] == 'pending'


@pytest.mark.parametrize('guard', ['unknown-effect', 'dry-run', 'wrong-attempt'])
def test_unknown_effect_preview_and_stale_identity_cannot_release_authority(tmp_path, monkeypatch, guard):
    manager = SubAgentManager(tmp_path/'agents', owner_id='synthetic', owner_home_dir=str(tmp_path/'owner'))
    task = manager.create_run(goal='合成等待', thought='', plan=['等待'])
    task = manager.lifecycle.prepare_runner_attempt(task.id)
    attempt_id = task.runner_active_attempt_id
    with runner_session_lease(RunnerSessionPoolLease(manager, task.id, attempt_id=attempt_id)):
        pass
    if guard == 'unknown-effect':
        monkeypatch.setattr(manager.runtime_db, 'unsettled_attempt_operations', lambda _: [{'status': 'unknown'}])
    settle_resumable_worker_slice(SimpleNamespace(subagents=manager), task.id, SimpleNamespace(status='BLOCKED'),
        attempt_id='attempt-other' if guard == 'wrong-attempt' else attempt_id, dry_run=guard == 'dry-run')
    assert manager.runtime_db.get_attempt(attempt_id)['status'] == 'running'
