"""固定binder实际ingress失败不得在恢复后洗成completed；不改原issuer或来源登记资格。"""
import pytest

from agent_py_agent.agent.common.owner_write_errors import OwnerManagementIngressUnavailable
from agent_py_agent.tests.test_coordinator_agents_domain import submit
from agent_py_agent.tests.test_coordinator_control_forward import cold
from agent_py_agent.tests.test_coordinator_memory_candidates_source import install, ready


# LLM: 故障只在原第二核窗口临时关闭同issuer，由其原current_permission真正抛错，不注入同码替身。
# 函数用途: 复现首个current通过之后固定binder内的实际拒绝，并保存原异常。
def reject_original_ingress(monkeypatch):
    from agent_py_agent.agent.gateway_parts import coordinator_memory_candidates as domain

    original, failures = domain._bind_management_memory_ingress, []

    def reject(source):
        if failures:
            return original(source)
        ingress = source.http_origin[7]
        active, ingress.active = ingress.active, False
        try:
            return original(source)
        except OwnerManagementIngressUnavailable as error:
            failures.append(error)
            raise
        finally:
            ingress.active = active

    monkeypatch.setattr(domain, '_bind_management_memory_ingress', reject)
    return failures


def test_fixed_binder_retains_actual_ingress_error_after_restore_and_exit(cold, monkeypatch):  # noqa: F811
    failures = reject_original_ingress(monkeypatch)

    def exercise(source, scope):
        with pytest.raises(OwnerManagementIngressUnavailable) as first:
            source.bind_management_memory_ingress()
        assert first.value is failures[0] is source._native_source_failure
        _bound, result, thread_id = ready(source)
        return result, thread_id

    observed = install(monkeypatch, exercise)
    status, result = submit(cold, '/memory candidates', 'retain-binder')
    source = observed[0]
    assert status == 503 and observed[-1] is failures[0], (result, observed)
    assert source._receipt_state == 'executing' and source._management_memory_binding is None
    assert not source.lease._commit_pins
    with pytest.raises(OwnerManagementIngressUnavailable) as late:
        source.bind_management_memory_ingress()
    assert late.value is failures[0]


@pytest.mark.parametrize('kind', ['request', 'tool'])
def test_retained_binder_native_never_replaces_current_actual_outer(cold, monkeypatch, kind):  # noqa: F811
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
    )
    from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable

    failures = reject_original_ingress(monkeypatch)
    outer = (RequestExecutionContextUnavailable('synthetic current source') if kind == 'request'
             else ToolWriteContextUnavailable('synthetic current source'))
    cause = ValueError('synthetic original cause')
    outer.__cause__ = cause

    def exercise(source, scope):
        with pytest.raises(OwnerManagementIngressUnavailable):
            source.bind_management_memory_ingress()
        raise outer

    observed = install(monkeypatch, exercise)
    status, result = submit(cold, '/memory candidates', 'current-outer-'+kind)
    source = observed[0]
    assert status == 503 and observed[-1] is outer and outer.__cause__ is cause, (result, observed)
    assert outer.__context__ is None and source._native_source_failure is failures[0]
    assert source._receipt_state == 'executing' and not source.lease._commit_pins
