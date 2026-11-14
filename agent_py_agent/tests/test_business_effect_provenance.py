"""领域默认拒绝与业务主体显式效果证明不得互相冒充。"""
import pytest

from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.tests import test_business_model_tools as business

agent = business.agent


@pytest.mark.parametrize(
    'raised,expected',
    [
        (BusinessError('REVISION_CONFLICT', effect_outcome='not_started'), 'not_started'),
        (BusinessError('REVISION_CONFLICT', effect_outcome='committed'), 'unknown'),
        (BusinessError('REVISION_CONFLICT', effect_outcome='unknown'), 'unknown'),
        (RuntimeError('post-enter-untyped-error'), 'unknown'),
    ],
)
def test_business_body_only_trusts_declared_effect(agent, monkeypatch, raised, expected):
    calls = []

    def fail_invoke(self, action, params, context, **kwargs):
        calls.append(action)
        raise raised

    monkeypatch.setattr(api.BusinessServices, 'invoke', fail_invoke)
    result = agent.tools.tools['business_space'].execute(
        {
            'action': 'spaces.create',
            'params': {'name': 'synthetic'},
            'request_id': 'provenance-request',
            'idempotency_key': 'provenance-operation-0001',
        }
    )
    assert calls == ['spaces.create']
    assert not result.ok
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
