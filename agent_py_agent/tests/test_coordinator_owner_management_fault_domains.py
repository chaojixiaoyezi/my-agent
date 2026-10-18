"""实际HTTP/work/source与原AgentsWriteService首故障回归；不将受控故障类型断言冒充真实物理排空验收。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.control_commands import ConversationControlResult
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.tests.test_coordinator_model_card_delivery import delivery_case
from agent_py_agent.tests.test_coordinator_model_menu import post_model
from agent_py_agent.tests.test_coordinator_owner_management_source import (
    install_source_contract_fixture,
    request_body,
)


def test_native_actor_failure_swallowed_by_original_agents_confirm_cannot_complete(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability.agents_source_host import build_agents_source_repository
    from agent_py_agent.agent.capability.agents_source_repository import AgentsSourceRepository
    from agent_py_agent.agent.capability.agents_write_service import (
        AgentsWriteService,
        _direct_request,
    )
    from agent_py_agent.agent.gateway_parts.agents_control_service import _conversation_key
    from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
        _LocalControlWriteSource,
    )
    from agent_py_agent.agent.gateway_parts.owner_management_control import (
        OwnerManagementIngressUnavailable,
    )

    actor, faults, armed, domain_results = [], [], [], []
    original_require = _LocalControlWriteSource._require_actor_current
    original_write = AgentsSourceRepository.write_text

    def require(source):
        if not armed:
            return original_require(source)
        ingress = source.http_origin[7]
        ingress.active = False
        try:
            return original_require(source)
        except OwnerManagementIngressUnavailable as error:
            faults.append(error)
            raise
        finally:
            ingress.active = True

    def write(repo, *args, **kwargs):
        armed.append(True)
        try:
            return original_write(repo, *args, **kwargs)
        finally:
            armed.clear()

    monkeypatch.setattr(_LocalControlWriteSource, '_require_actor_current', require)
    monkeypatch.setattr(AgentsSourceRepository, 'write_text', write)

    def exercise(common, carrier):
        source, receipt, scope = carrier
        writer = source.bind_resource_owner(actor[0].home_paths, actor=actor[0],
            subject=(source.http_origin[7], source.http_origin[3], scope))
        repo = build_agents_source_repository(actor[0], file_commit=writer)
        request = _direct_request(owner=repo.owner, actor=actor[0].home_paths.owner_id,
            thread_id=_conversation_key(scope.user_id, scope.channel, scope.conversation_id),
            request_id=scope.metadata['message_id'], raw=source.http_origin[3].value)
        details = AgentsWriteService(repo).execute(request)
        domain_results.append(details)
        return common.complete_owner_management_operation(source,
            ConversationControlResult('agents', details['ok'], '原领域结果', details=details))

    observed = install_source_contract_fixture(monkeypatch, exercise)
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        server, row, host = case[5], case[3], case[1]
        actor.append(host)
        host.config.agents_source_enabled = True
        preview = {**request_body('fault-preview'), 'command': '/agents write "protocol47-body"'}
        status, payload = post_model(server, preview, route='/control', actor=row['actor_id'])
        assert status == 200 and domain_results[-1]['state'] == 'preview', payload
        command = domain_results[-1]['confirm_command']
        status, payload = post_model(server, {**request_body('fault-confirm'), 'command': command},
                                     route='/control', actor=row['actor_id'])
        assert faults and isinstance(faults[0], OwnerManagementIngressUnavailable)
        assert domain_results[-1]['state'] == 'consumed_unknown'
        source = observed[-1][0] if isinstance(observed[-1], tuple) else observed[-2][0]
        persisted = ops.read_gateway_control_operation(server.paths, source.receipt.operation_id, read_only=True)
        # 当前B37会200/completed；B38必须503/executing且同首个真实native对象穿出。
        assert status == 503 and persisted.state == 'executing', (status, payload, persisted.state)
        assert observed[-1] is faults[0]
        assert not delivered


@pytest.mark.parametrize('exit_mode', ['complete', 'normal_exit'])
def test_swallowed_native_from_original_current_producer_is_rethrown(tmp_path, monkeypatch, exit_mode):
    faults = []
    def exercise(common, carrier):
        source = carrier[0]
        completed = (common.complete_owner_management_operation(source, ConversationControlResult('agents', False, '原业务已收口'))
                     if exit_mode == 'normal_exit' else None)
        ingress = source.http_origin[7]
        ingress.active = False
        try:
            source._require_http_current()
        except ValueError as error:
            faults.append(error)
        finally:
            ingress.active = True
        if exit_mode == 'complete':
            return common.complete_owner_management_operation(source, ConversationControlResult('agents', False, '业务吞掉拒绝'))
        # 已有真实completed事实不回滚；正常出口仍须先重抛后续被吞的原native。
        return completed
    observed = install_source_contract_fixture(monkeypatch, exercise)
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        status, payload = post_model(case[5], request_body(), route='/control', actor=case[3]['actor_id'])
        assert faults
        assert status == 503 and observed[-1] is faults[0], (status, payload)
        assert not delivered


def test_current_outer_physical_error_keeps_original_object_cause_and_context(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
    first, actual = [], BoundedCallExitUnconfirmedError('protocol47', 'worker_exit_unconfirmed')
    cause = OSError('original cause')
    def exercise(common, carrier):
        source = carrier[0]
        ingress = source.http_origin[7]
        ingress.active = False
        try:
            source._require_http_current()
        except ValueError as error:
            first.append(error)
        finally:
            ingress.active = True
        raise actual from cause
    observed = install_source_contract_fixture(monkeypatch, exercise)
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        status, payload = post_model(case[5], request_body(), route='/control', actor=case[3]['actor_id'])
        assert status == 503 and observed[-1] is actual and actual.__cause__ is cause, payload
        assert actual is not first[0]
        assert not delivered


@pytest.mark.parametrize('kind', ['ordinary_same_code', 'foreign_same_class', 'business_unknown'])
def test_business_failure_does_not_create_native_source_failure(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.gateway_parts.owner_management_control import (
        OwnerManagementIngressUnavailable,
    )
    def exercise(common, carrier):
        if kind != 'business_unknown':
            error = OSError('business failure') if kind == 'ordinary_same_code' else OwnerManagementIngressUnavailable('not_from_source_producer')
            error.error_code = 'GLOBAL_WRITE_REJECTED'
            try:
                raise error
            except Exception:
                pass
        return common.complete_owner_management_operation(carrier[0],
            ConversationControlResult('agents', False, '原业务未知', details={'state': 'consumed_unknown'}))
    install_source_contract_fixture(monkeypatch, exercise)
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        status, payload = post_model(case[5], request_body(), route='/control', actor=case[3]['actor_id'])
        assert status == 200 and payload['control_state'] == 'completed'
        assert payload['details']['state'] == 'consumed_unknown'
        assert not delivered
