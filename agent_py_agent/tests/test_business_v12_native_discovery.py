# 模块用途: BIZ-004/V10-05 回归：原生组合里对象详情 allowed_actions 只能公布 system.capabilities 同样承认且确实可调用的动作。
from __future__ import annotations

from agent_py_agent.tests.test_business_v12_background_scheduling import (  # noqa: F401 - pytest fixture
    call,
    native,
    ok,
)


def test_space_allowed_actions_never_advertise_unwired_actions(native):  # noqa: F811
    space = ok(native, 'spaces.create', {'name': 'discovery'}, idempotency_key='discovery-space-0001')
    supported = set(ok(native, 'system.capabilities')['supported_actions'])
    allowed = ok(native, 'spaces.get', {'space_id': space['space_id']})['allowed_actions']
    assert allowed and set(allowed) <= supported, sorted(set(allowed) - supported)
    # 未注入原端口时，动作既不被公布，调用也明确 HOST_NOT_READY/not_started，而不是假装可用。
    assert 'interfaces.list' not in supported and 'interfaces.list' not in allowed
    status, payload = call(native, 'interfaces.list', {'space_id': space['space_id']})
    assert (status, payload['error']['code'], payload['effect_outcome']) == (503, 'HOST_NOT_READY', 'not_started')
