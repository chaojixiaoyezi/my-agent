# ruff: noqa: F811
"""仅实际 Promotion/候选主链与受控原 apply_batch callee 异常；不代 J3 或 HTTP 装配。"""
from dataclasses import replace

import pytest

from agent_py_agent.agent.common.global_write_commit import global_write_commit_scope
from agent_py_agent.agent.common.owner_write_errors import OwnerManagementIngressUnavailable
from agent_py_agent.agent.memory_store import promotion
from agent_py_agent.agent.memory_store.owner_services import (
    ExplicitRememberServices,
    remember_explicit,
)
from agent_py_agent.tests.test_memory_j1_write_ports import promotion_resources
from agent_py_agent.tests.test_memory_j2_write_ports import lesson_candidate


@pytest.mark.parametrize('coordinator', [False, True])
def test_long_term_actual_apply_callee_keeps_original_ingress_object(tmp_path, monkeypatch, coordinator):
    service, quota, door = promotion_resources(tmp_path)
    service.coordinator_io = coordinator
    error = OwnerManagementIngressUnavailable('controlled original long_term source')
    cause = OSError('controlled cause')
    error.__cause__ = cause
    calls = []
    def fail_batch(*args, **kwargs):
        calls.append(True)
        raise error
    monkeypatch.setattr(service.long_term, 'apply_batch', fail_batch)
    with global_write_commit_scope(door), pytest.raises(OwnerManagementIngressUnavailable) as caught:
        remember_explicit(ExplicitRememberServices(service.candidates, service, service.long_term), '本次长记忆事实')
    assert calls == [True]
    assert caught.value is error and error.__cause__ is cause
    with global_write_commit_scope(door):
        assert {c.status for c in service.candidates.list()} == {'approved'}
    assert not service.long_term.all()

def test_c_ordinary_lesson_threshold_remains_original_blocked(tmp_path):
    service, quota, door = promotion_resources(tmp_path)
    candidate = replace(lesson_candidate(), occurrence_count=0)
    with global_write_commit_scope(door), pytest.raises(promotion._PromotionBlocked) as caught:
        promotion._commit_target_blocked(service, candidate, promotion._PromotionWriteContext('test',False,None))
    assert caught.value.reason_code == 'PROMOTION_THRESHOLD_NOT_MET'
