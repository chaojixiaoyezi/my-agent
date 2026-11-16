"""总控只放宽同Owner空间相等门，授权拒绝先于控制写入。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.conversation.business_contracts import (
    BusinessContextIntent,
    BusinessExecutionContext,
)
from agent_py_agent.agent.conversation.session_messaging import (
    _cancellation_binding,
    _validate_cancellation_task,
    cancel_owned_session_task,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
from agent_py_agent.tests.test_business_v12_f7_observation import fixture


def test_owner_controller_cross_space_and_bound_root_rejected(tmp_path):
    case = fixture(tmp_path)
    host = SimpleNamespace(conversation_store=case.store, home_paths=SimpleNamespace(owner_id=case.owner))
    root = case.store.session_tasks.create(SessionTaskDraft('controller', 'controller', '总控', dedupe_key='root'),
        business_context_intent=BusinessContextIntent(case.owner, None, None, None),
        business_context_factory=lambda tid: BusinessExecutionContext(case.owner, None, None, tid,
            None, None, (), (), 'business-context.v1'))
    root_context = root.metadata['business_context']
    assert _validate_cancellation_task(host, case.task, _cancellation_binding(case.task), root_context) == case.task
    bound = {**root_context, 'space_id': 'different'}
    with pytest.raises(PermissionError, match='SPACE_MISMATCH'):
        _validate_cancellation_task(host, case.task, _cancellation_binding(case.task), bound)


def test_authorization_business_error_does_not_write_intent(tmp_path):
    case = fixture(tmp_path)
    host = SimpleNamespace(conversation_store=case.store, home_paths=SimpleNamespace(owner_id=case.owner))
    before = case.store.session_tasks.require(case.task.task_id).to_dict()
    def denied(task):
        raise BusinessError('BUSINESS_ACCESS_DENIED')
    with pytest.raises(BusinessError) as caught:
        cancel_owned_session_task(host, case.task, include_descendants=True, authorize_task=denied)
    assert caught.value.effect_outcome == 'not_started'
    assert case.store.session_tasks.require(case.task.task_id).to_dict() == before


def test_space_bound_closure_mismatch_is_not_started(tmp_path):
    case = fixture(tmp_path)
    host = SimpleNamespace(conversation_store=case.store, home_paths=SimpleNamespace(owner_id=case.owner))
    child = case.store.session_tasks.create(SessionTaskDraft('background', 'controller', '跨空间',
        dedupe_key='different-space', origin_task_id=case.task.task_id),
        business_context_intent=BusinessContextIntent(case.owner, 'space-b', None, None),
        business_context_factory=lambda tid: BusinessExecutionContext(case.owner, 'space-b', None, tid,
            1, None, (), (), 'business-context.v1'))
    before = [case.store.session_tasks.require(tid).to_dict() for tid in (case.task.task_id, child.task_id)]
    with pytest.raises(BusinessError) as caught:
        cancel_owned_session_task(host, case.task, include_descendants=True, authorize_task=lambda task: True)
    assert caught.value.code == 'SPACE_MISMATCH'
    assert caught.value.effect_outcome == 'not_started'
    assert caught.value.details['reason'] == 'CANCELLATION_SCOPE_MISMATCH'
    assert [case.store.session_tasks.require(tid).to_dict() for tid in (case.task.task_id, child.task_id)] == before


def test_failure_after_control_commit_retains_unknown(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import session_messaging

    case = fixture(tmp_path)
    host = SimpleNamespace(conversation_store=case.store, home_paths=SimpleNamespace(owner_id=case.owner))
    def revoked(*args, **kwargs):
        raise BusinessError('BUSINESS_ACCESS_DENIED')
    monkeypatch.setattr(session_messaging, '_cancel_selected_session_task', revoked)
    with pytest.raises(BusinessError) as caught:
        cancel_owned_session_task(host, case.task, authorize_task=lambda task: True)
    assert caught.value.effect_outcome == 'unknown'
    assert 'cancellation_control' in case.store.session_tasks.require(case.task.task_id).metadata


def test_retire_identity_commit_keeps_cascade_failure_unknown(business_host, monkeypatch):
    from agent_py_agent.agent.conversation import background_agents
    from agent_py_agent.tests.test_business_v12_f3_agents import _service

    service, host, actor = _service(business_host)
    view = service.create({'space_id': 'space-A', 'name': 'worker'}, actor=actor,
        idempotency_key='create-unknown')['agent']
    def unreadable(*args, **kwargs):
        raise BusinessError('STORE_UNAVAILABLE')
    monkeypatch.setattr(background_agents, 'identity_outstanding', unreadable)
    with pytest.raises(BusinessError) as caught:
        service.transition(view['agent_id'], 'retire', actor=actor, expected_revision=view['revision'],
            idempotency_key='retire-unknown', disposition='cancel_tasks')
    assert caught.value.effect_outcome == 'unknown'
    assert host.conversation_store.threads.require(view['agent_id']).metadata['background_agent']['identity_state'] in {'retiring', 'retired'}


from agent_py_agent.tests.test_business_v12_f3_agents import business_host  # noqa: E402

_FIXTURES = (business_host,)
