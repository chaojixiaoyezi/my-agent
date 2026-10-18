# ruff: noqa: F811
"""真实HTTP固定binder的第二次原ingress核验；实际producer抛错，不注入同码替身。"""

import pytest

from agent_py_agent.tests.test_coordinator_agents_domain import submit
from agent_py_agent.tests.test_coordinator_control_forward import cold
from agent_py_agent.tests.test_coordinator_memory_candidates_source import install, ready


def test_actual_ingress_rejection_inside_fixed_binder_is_retained(cold, monkeypatch):
    from agent_py_agent.agent.common.owner_write_errors import OwnerManagementIngressUnavailable
    from agent_py_agent.agent.gateway_parts import coordinator_memory_candidates as domain

    original = domain._bind_management_memory_ingress
    first = []
    seen = []

    def revoke_at_original_leaf(source):
        # 前一个_require_current已经通过；只在原第二次require读取窗口让实际原issuer失活。
        ingress = source.http_origin[7]
        if not first:
            active = ingress.active
            ingress.active = False
            try:
                return original(source)
            except OwnerManagementIngressUnavailable as error:
                first.append(error)
                raise
            finally:
                ingress.active = active
        return original(source)

    monkeypatch.setattr(domain, "_bind_management_memory_ingress", revoke_at_original_leaf)

    def exercise(source, scope):
        with pytest.raises(OwnerManagementIngressUnavailable) as caught:
            source.bind_management_memory_ingress()
        assert caught.value is first[0]
        seen.append(source._native_source_failure)
        # 模拟原要求防住的领域捕获后继续；不能因为恢复当前条件签completed。
        _bound, result, thread_id = ready(source)
        return result, thread_id

    observed = install(monkeypatch, exercise)
    status, receipt = submit(cold, "/memory candidates", "bind-original-native")
    assert status == 503 and observed[-1] is first[0], (status, receipt, seen, observed)
    assert seen == [first[0]]
