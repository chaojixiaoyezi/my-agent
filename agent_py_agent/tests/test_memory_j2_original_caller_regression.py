"""J2 调用方前像反例：真实候选/Promotion/Lesson 仓库，受控实际类只从原正文 writer 抛出。不是现有 J2 writer 或真实 HTTP 验收。"""
import pytest

from agent_py_agent.agent.common.global_write_commit import global_write_commit_scope
from agent_py_agent.agent.common.owner_write_errors import OwnerManagementIngressUnavailable
from agent_py_agent.agent.memory_store import lessons
from agent_py_agent.tests.memory_confirmation_fixtures import confirm_candidate
from agent_py_agent.tests.test_memory_j1_write_ports import promotion_resources
from agent_py_agent.tests.test_memory_promotion_v2 import _lesson


def test_j2_lesson_actual_valueerror_source_must_survive_original_promotion_caller(tmp_path, monkeypatch):
    service, quota, door = promotion_resources(tmp_path)
    with global_write_commit_scope(door):
        candidate = _lesson(service, content='原隔离 lesson 候选正文', runs=('run-1','run-2'), tasks=('task-1','task-2'))
    error = OwnerManagementIngressUnavailable('controlled original owner source rejection')
    cause = OSError('controlled cause')
    error.__cause__ = cause
    seen = []
    def reject_raw(path, text, **kwargs):
        seen.append(path)
        raise error
    monkeypatch.setattr(lessons, 'write_private_text_file_atomic_unlocked', reject_raw)
    with global_write_commit_scope(door):
        try:
            result = confirm_candidate(service, candidate.candidate_id)
        except OwnerManagementIngressUnavailable as caught:
            assert caught is error and caught.__cause__ is cause
            assert len(seen) == 1
        else:
            assert seen, '反例必须实际到达原 lesson 正文 writer'
            pytest.fail(f'原同对象被吞：{result.reason_code}; status={result.status}; promoted={result.promoted}')
