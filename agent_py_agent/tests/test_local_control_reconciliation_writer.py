# LLM: 使用原本机C/lease/source/op和普通受控工具/对账fixture，不运行模型/native；结果资料不能替代原来源与完整raw门。
# 模块用途: 覆盖合法插话对账三投递态、无结果不写以及普通/复制/错op写入拒绝。
import copy
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlResult,
    parse_conversation_control,
)
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts import multiprocess_local_control as local
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_coordinator_model_card_delivery import delivery_case


@pytest.mark.parametrize('delivery', ['accepted', 'rejected', 'unknown', None])
def test_actual_local_reconcile_only_consumes_original_result(delivery, tmp_path, monkeypatch):
    with delivery_case(tmp_path, monkeypatch, kind='main') as (case, _):
        agent, server, actor = case[0], case[5], case[3]['actor_id']
        text = '/btw 继续检查'
        scope = GatewayControlScope(user_id=actor, channel='feishu', conversation_id='local-reconcile',
            metadata={'message_id': 'local-reconcile-154'}, resolved_owner=OwnerIdentity.local_main())
        calls, commits = [], []

        def failed_effect(*_args):
            calls.append('effect')
            raise OSError('controlled delivery readback failure')

        def reconcile(*_args):
            calls.append('reconcile')
            if delivery is None:
                return None
            return ConversationControlResult('steer', delivery != 'rejected', '原结构化投递事实',
                request_id='synthetic-current-turn', delivery_status=delivery)

        original = local._commit_original_steer_reconciliation

        def commit(source, expected, result):
            path = ops.gateway_control_operation_path(source.paths, expected.operation_id)
            before = path.read_bytes()
            ordinary = replace(expected, state='completed', result=result.to_dict(), error={})
            with pytest.raises((ValueError, local.LocalControlCoordinatorUnavailable)):
                source.commit_control_receipt(ordinary.to_dict())
            with pytest.raises((ValueError, local.LocalControlCoordinatorUnavailable)):
                original(copy.copy(source), expected, result)
            last = '0' if expected.operation_id[-1] != '0' else '1'
            with pytest.raises((ValueError, local.LocalControlCoordinatorUnavailable)):
                original(source, replace(expected, operation_id=expected.operation_id[:-1] + last), result)
            with pytest.raises((ValueError, local.LocalControlCoordinatorUnavailable)):
                original(source, replace(expected, updated_at=expected.updated_at + 1), result)
            with pytest.raises((ValueError, local.LocalControlCoordinatorUnavailable)):
                original(source, expected, result.to_dict())
            assert path.read_bytes() == before
            prefix = source.controller._bootstrap_sources[:4]
            actual = original(source, expected, result)
            assert source.controller._bootstrap_sources[:4] == prefix
            commits.append(actual)
            return actual

        monkeypatch.setattr(ops, 'execute_gateway_conversation_control', failed_effect)
        monkeypatch.setattr(ops, 'reconcile_gateway_steer_delivery', reconcile)
        monkeypatch.setattr(local, '_commit_original_steer_reconciliation', commit)
        receipt = ops.execute_gateway_control_operation(agent, server.paths,
            parse_conversation_control(text, reject_unknown_slash=True), scope, command_text=text)
        assert calls == ['effect', 'reconcile']
        if delivery is None:
            assert receipt.state == 'terminal_unknown' and not commits
        else:
            assert receipt.state == 'completed' and receipt.result['delivery_status'] == delivery
            assert receipt.result['ok'] is (delivery != 'rejected')
            assert commits == [receipt]
        assert ops.read_gateway_control_operation(server.paths, receipt.operation_id, read_only=True) == receipt


@pytest.mark.parametrize('decision', ['approved', 'denied'])
def test_actual_local_private_reconcile_preserves_original_unknown_edges(tmp_path, monkeypatch, decision):
    from agent_py_agent.agent.gateway_parts import private_approval_control as private
    from agent_py_agent.tests.test_coordinator_approval_decision import (
        test_ready_local_c_uses_same_actual_coordinator_waiter_without_worker_or_foreign_agent as original_case,
    )

    original = local._commit_original_private_approval_reconciliation
    observed = []

    def commit(source, expected, updated):
        if type(source) is not local._LocalControlWriteSource or updated.state != 'completed':
            return original(source, expected, updated)
        path = ops.gateway_control_operation_path(source.paths, expected.operation_id)
        before = path.read_bytes()
        for bad_source, bad_expected, bad_updated in [
            (copy.copy(source), expected, updated),
            (source, replace(expected, updated_at=expected.updated_at + 1), updated),
            (source, expected, updated.to_dict()),
            (source, expected, replace(updated, owner_id='unrelated')),
        ]:
            with pytest.raises((ValueError, local.LocalControlCoordinatorUnavailable)):
                original(bad_source, bad_expected, bad_updated)
        assert path.read_bytes() == before
        prefix = source.controller._bootstrap_sources[:4]
        actual_reader = private._permission_record

        def unreadable(*_args):
            raise ValueError('controlled original permission readback error')

        with monkeypatch.context() as fault:
            fault.setattr(private, '_permission_record', unreadable)
            first = private.reconcile_private_approval_locked(source.paths, expected, agent=source.agent)
            assert first.state == 'terminal_unknown'
            original(source, expected, first)
            with pytest.raises((ValueError, local.LocalControlCoordinatorUnavailable)):
                source.commit_control_receipt(replace(first, state='completed', result=updated.result, error={}).to_dict())
            second = private.reconcile_private_approval_locked(source.paths, first, agent=source.agent)
            assert second.state == 'terminal_unknown'
            original(source, first, second)
        assert private._permission_record is actual_reader
        settled = private.reconcile_private_approval_locked(source.paths, second, agent=source.agent)
        assert settled.state == 'completed' and settled.result['details']['decision'] == decision
        result = original(source, second, settled)
        assert source.controller._bootstrap_sources[:4] == prefix
        observed.append(result)
        return result

    monkeypatch.setattr(local, '_commit_original_private_approval_reconciliation', commit)
    original_case(tmp_path, monkeypatch, decision)
    assert len(observed) == 1


@pytest.mark.parametrize('readiness', ['absent', 'fake', 'wrong_owner'])
def test_original_local_private_card_does_not_materialize_unready_agent(tmp_path, monkeypatch, readiness):
    from types import SimpleNamespace

    from agent_py_agent.tests.test_coordinator_approval_cancel import approval_case, seed

    original = seed.original_case

    def main_case(*args, **kwargs):
        return original(*args, **{**kwargs, 'kind': 'main'})

    monkeypatch.setattr(seed, 'original_case', main_case)
    with approval_case(tmp_path, monkeypatch) as (case, pending):
        agent, _peer, credential, _body, server, _runtime = case
        before = pending['path'].read_bytes()
        assert server.agent is None
        if readiness == 'fake':
            server.agent = SimpleNamespace(config=agent.config, home_paths=agent.home_paths)
        elif readiness == 'wrong_owner':
            monkeypatch.setattr(agent, 'config', replace(agent.config, my_agent_owner_provider='synthetic',
                my_agent_owner_kind='user', my_agent_owner_id='unrelated'))
            server.agent = agent
        status, result = seed.post(server, credential, {**pending['body'], 'decision': 'approved'})
        assert status == 503 and result['error_code'] == 'COORDINATOR_BUSINESS_NOT_READY', (status, result)
        assert pending['path'].read_bytes() == before
        assert not list((server.paths.root / 'control_operations').glob('*.json'))
