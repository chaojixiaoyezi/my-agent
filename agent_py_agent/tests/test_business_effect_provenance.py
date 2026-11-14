"""领域默认拒绝与业务主体显式效果证明不得互相冒充。"""
import pytest

from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError
from agent_py_agent.agent.tooling.business_spaces_tool import BusinessSpacesTool


@pytest.mark.parametrize('declared,expected', [(None, 'unknown'), ('not_started', 'not_started'), ('unknown', 'unknown')])
def test_business_body_only_trusts_declared_effect(declared, expected):
    calls = []

    class Service:
        owner_id = 'local/test'

        def authorize_call(self, action, params, actor):
            pass

        def call(self, action, params, **kwargs):
            calls.append(action)
            options = {} if declared is None else {'effect_outcome': declared}
            raise BusinessError('REVISION_CONFLICT', **options)

    service = Service()
    actor = BusinessActor(service.owner_id, 'test', service, lambda *_: True)
    result = BusinessSpacesTool(service, actor=actor).execute({'action': 'spaces.create',
        'params': {'name': 'synthetic'}, 'request_id': 'provenance-request',
        'idempotency_key': 'provenance-operation-0001'})
    assert calls == ['spaces.create']
    assert not result.ok and result.reported_error_code == 'REVISION_CONFLICT'
    assert result.effect_outcome == expected


def test_business_error_default_stays_rejection_but_is_not_body_proof():
    error = BusinessError('BUSINESS_ACCESS_DENIED')
    assert error.effect_outcome == 'not_started'
    assert error.effect_outcome_declared is False
    error.effect_outcome = 'not_started'
    assert error.effect_outcome_declared is True


def test_explicit_effect_is_preserved_and_invalid_constructor_effect_rejected():
    for value in ('not_started', 'committed', 'unknown'):
        error = BusinessError('TEST', effect_outcome=value)
        assert error.effect_outcome == value and error.effect_outcome_declared
    for value in (None, 'failed', '', 'DONE'):
        with pytest.raises(ValueError):
            BusinessError('TEST', effect_outcome=value)
