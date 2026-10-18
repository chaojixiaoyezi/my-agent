# LLM: 使用原closing producer及实际临时HTTP/G1/model/management/local source，只有原provider运输fake，不调用真实服务/模型/native。
# 模块用途: 验证后继固定回执完整链不会自隔离，外部无来源JSON不能借此获准。
import subprocess
from dataclasses import replace

import pytest

from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts import daemon_metadata
from agent_py_agent.agent.gateway_parts import multiprocess_local_control as local
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_coordinator_model_card_delivery import delivery_case
from agent_py_agent.tests.test_coordinator_model_menu import post_model
from agent_py_agent.tests.test_gateway_role_lifecycle_closure import (
    closing as closing,
)
from agent_py_agent.tests.test_gateway_role_lifecycle_closure import (
    context as context,
)
from agent_py_agent.tests.test_gateway_role_lifecycle_closure import (
    coordinator_handles as coordinator_handles,
)
from agent_py_agent.tests.test_gateway_worker_runtime import physical as physical

_REAL_POPEN = subprocess.Popen


@pytest.mark.parametrize('context', [2], indirect=True)
@pytest.mark.parametrize('kind', ['management', 'model', 'local'])
def test_successor_actual_fixed_source_completes_all_receipt_stages(
    closing, monkeypatch, coordinator_handles, tmp_path, kind,
):
    closing.seal_generation()
    coordinator_handles['close']()
    monkeypatch.setattr(subprocess, 'Popen', _REAL_POPEN)
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: False)
    observed = []
    original = local._write_original_control_receipt

    def fixed(source, path, payload):
        prefix = source.controller._bootstrap_sources[:4]
        receipt = original(source, path, payload)
        assert source.controller._bootstrap_sources[:4] == prefix
        known = dict(source.controller._bootstrap_sources[4])
        assert known[str(path.relative_to(source.paths.root))] == path.read_bytes()
        observed.append((type(source).__name__, receipt.state, source))
        return receipt

    monkeypatch.setattr(local, '_write_original_control_receipt', fixed)
    with delivery_case(tmp_path, monkeypatch, kind='main' if kind == 'local' else 'user') as (case, _delivered):
        agent, server, actor = case[0], case[5], case[3]['actor_id']
        if kind == 'local':
            from agent_py_agent.agent.conversation.control_commands import (
                parse_conversation_control,
            )
            text = '/help'
            scope = GatewayControlScope(user_id=actor, channel='feishu', conversation_id='local-control',
                metadata={'message_id': 'local-136'}, resolved_owner=OwnerIdentity.local_main())
            result = ops.execute_gateway_control_operation(agent, server.paths,
                parse_conversation_control(text, reject_unknown_slash=True), scope, command_text=text)
            assert result.state == 'completed' and result.result['ok']
        else:
            body = {'command': '/agents show' if kind == 'management' else '/model',
                'conversation_id': 'same-home-136', 'metadata': {'message_id': kind + '-136', 'channel_chat_type': 'p2p'}}
            status, result = post_model(server, body, route='/control', actor=actor)
            assert status == 200 and result['ok'], (status, result)
            assert result['control_state'] == 'completed'
        assert [state for _type, state, _source in observed] == ['prepared', 'executing', 'completed']
        source = observed[-1][2]
        assert source.controller.launch_owner_writer(owner_digest(source.owner), lambda: None)['status'] == 'started'
        assert not source.controller._all_quarantined
        operation_id = result.operation_id if kind == 'local' else result['operation_id']
        path = ops.gateway_control_operation_path(source.paths, operation_id)
        original_raw = path.read_bytes()
        payload = ops.read_gateway_control_operation(source.paths, operation_id, read_only=True).to_dict()
        with pytest.raises((ValueError, local.LocalControlCoordinatorUnavailable)):
            if kind == 'model':
                ops._write_control_operation_json(path, payload, model_transaction=source)
            else:
                source.commit_control_receipt(payload)
        assert original_raw == path.read_bytes()



@pytest.mark.parametrize('context', [2], indirect=True)
def test_successor_actual_source_unknown_receipt_still_remains_exact(
    closing, monkeypatch, coordinator_handles, tmp_path,
):
    from agent_py_agent.tests.test_coordinator_owner_management_source import (
        install_source_contract_fixture,
    )

    closing.seal_generation()
    coordinator_handles['close']()
    monkeypatch.setattr(subprocess, 'Popen', _REAL_POPEN)
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: False)
    stages = []

    def exercise(_common, carrier):
        source, receipt, _scope = carrier
        assert source._receipt_state == receipt.state == 'executing'
        for state in ['terminal_unknown']:
            value = replace(receipt, state=state,
                error={'error_code': 'CONTROL_OPERATION_INTERRUPTED'} if state == 'terminal_unknown' else {})
            stages.append(source.commit_control_receipt(value.to_dict()))
            receipt = value
        return receipt

    install_source_contract_fixture(monkeypatch, exercise)
    with delivery_case(tmp_path, monkeypatch) as (case, _):
        server, actor = case[5], case[3]['actor_id']
        status, result = post_model(server, {'command': '/agents show', 'conversation_id': 'unknown-136',
            'metadata': {'message_id': 'unknown-136', 'channel_chat_type': 'p2p'}}, route='/control', actor=actor)
        assert status == 202 and result['control_state'] == 'terminal_unknown', (status, result)
        assert [value.state for value in stages] == ['terminal_unknown']
        controller = server.control_plane.controller()
        assert not controller._all_quarantined
        assert controller.launch_owner_writer(owner_digest(OwnerIdentity.provider_user('feishu', actor)), lambda: None)['status'] == 'started'
