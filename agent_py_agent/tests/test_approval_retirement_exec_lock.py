"""原exec锁事实只读getter与活死判据共享读法；核收不借TTL清理。"""
import pytest

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.tests.test_tool_approval_foreground_scope import _foreground


def test_raw_exec_lock_getter_preserves_original_liveness_and_releases_only_by_settle(tmp_path, monkeypatch):
    world = _foreground(tmp_path, kind='user')
    repo = world.agent.subagents.runtime_db
    attempt = repo.get_attempt(world.params.attempt_id)
    run_id = attempt['agent_run_id']
    assert repo.get_agent_run_exec_lock(run_id) is None
    attempt = repo.create_attempt(run_id)
    lock = dict(repo.get_agent_run_exec_lock(run_id))
    assert lock['attempt_id'] == attempt['attempt_id']
    assert lock['attempt_generation'] == attempt['attempt_generation']
    assert repo.has_active_exec_lock(run_id, now=10**20)
    seen = []
    read = repo.get_agent_run_exec_lock

    def observe(agent_run_id):
        seen.append(agent_run_id)
        return read(agent_run_id)

    monkeypatch.setattr(repo, 'get_agent_run_exec_lock', observe)
    assert repo.has_active_exec_lock(run_id, now=0) and seen == [run_id]
    assert dict(repo.get_agent_run_exec_lock(run_id)) == lock
    repo.settle_agent_run(agent_run_id=run_id, attempt_id=attempt['attempt_id'],
                          expected_attempt_status='running', status='cancelled')
    assert repo.get_agent_run_exec_lock(run_id) is None
    assert not repo.has_active_exec_lock(run_id)
    new = repo.create_attempt(run_id)
    new_lock = dict(repo.get_agent_run_exec_lock(run_id))
    assert new_lock['attempt_id'] == new['attempt_id'] != lock['attempt_id']
    assert new_lock['attempt_generation'] > lock['attempt_generation']
    assert repo.get_attempt(attempt['attempt_id'])['status'] == 'cancelled'
    assert repo.has_active_exec_lock(run_id)
    repo.settle_agent_run(agent_run_id=run_id, attempt_id=new['attempt_id'], status='cancelled')


def test_raw_exec_lock_getter_does_not_turn_read_error_into_no_lock(tmp_path, monkeypatch):
    repo = RuntimeRepository(tmp_path / 'home/runtime.db')
    error = OSError('synthetic exact original read unavailable')

    def unavailable():
        raise error

    monkeypatch.setattr(repo, '_runtime_connection', unavailable)
    with pytest.raises(OSError) as caught:
        repo.get_agent_run_exec_lock('missing-run')
    assert caught.value is error
    with pytest.raises(OSError) as caught:
        repo.has_active_exec_lock('missing-run')
    assert caught.value is error
