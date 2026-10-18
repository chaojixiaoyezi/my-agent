"""通用控制状态只读原回执；使用真实本机HTTP工作片，不执行控制或修改领域账。"""
import http.client
import json
import time
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.control_commands import ConversationControlResult
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_coordinator_model_card_delivery import delivery_case


# LLM: 合成原v2普通控制回执并经唯一codec验证；仅测试准备，不伪造执行或授权效果，不用于真实模型任务。
# 函数用途: 为只读接口提供合法的原状态输入，避免为了测试GET先执行一次业务命令。
def original_receipt(server, row, *, state, command='/status'):
    owner = OwnerIdentity(**row['owner'])
    scope = GatewayControlScope(user_id=row['actor_id'], channel='feishu',
        conversation_id='ordinary-status-conversation', resolved_owner=owner,
        metadata={'message_id': 'ordinary-control-status', 'channel_chat_type': 'p2p'})
    operation_id, digest = ops.gateway_control_operation_identity(scope, command)
    kind = command.split()[0].removeprefix('/')
    receipt = ops.GatewayControlOperationReceipt(operation_id=operation_id, input_digest=digest,
        command_text=command, command_kind=kind, user_id=scope.user_id, channel=scope.channel,
        conversation_id=scope.conversation_id, channel_chat_type='p2p', channel_chat_id='',
        owner_provider=owner.provider, owner_kind=owner.owner_kind, owner_id=owner.owner_id,
        owner_ref_digest=ops._owner_ref_digest(owner), client_message_id='ordinary-control-status',
        expected_turn_id='', target_control_message_id='', all_user_access=False, state=state,
        input_digest_version=2, updated_at=time.time())
    if state == 'completed':
        receipt = replace(receipt, result=ConversationControlResult(kind, True, '原控制已完成').to_dict())
    elif state == 'terminal_unknown':
        receipt = replace(receipt, error={'error_code': 'ORIGINAL_RESULT_UNKNOWN'})
    receipt = ops.GatewayControlOperationReceipt.from_dict(receipt.to_dict())
    path = ops.gateway_control_operation_path(server.paths, operation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt.to_dict()))
    return receipt, path


# LLM: 原实际监听器与G1凭据/同actor；不调用handler函数或创建source替身，错误响应也读完整正文后关闭连接。
# 函数用途: 从真实HTTP路由读取一条原控制回执。
def read_status(server, row, operation_id):
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=3)
    try:
        connection.request('GET', '/control-status/' + operation_id, headers={
            'X-Gateway-Token': server._local_client_credential,
            'X-Channel': 'feishu', 'X-User-Id': row['actor_id'],
            'X-Conversation-Id': 'ordinary-status-conversation', 'X-Channel-Chat-Type': 'p2p'})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.parametrize('ready', [False, True])
@pytest.mark.parametrize('state', ['prepared', 'executing', 'completed', 'terminal_unknown'])
def test_positive_count_reads_ordinary_receipt_without_domain_reconciliation(tmp_path, monkeypatch, ready, state):
    from agent_py_agent.agent.gateway_parts import http_handlers
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        agent, _host, _path, row, _body, server, _runtime, _sent = case
        if ready:
            server.agent = agent
        receipt, path = original_receipt(server, row, state=state)
        before = {p.relative_to(server.paths.root): p.read_bytes() for p in server.paths.root.rglob('*') if p.is_file()}
        calls = []
        def forbidden(*args, **kwargs):
            calls.append(True)
            raise AssertionError('readonly GET cannot reconcile or execute control')
        monkeypatch.setattr(http_handlers, 'reconcile_gateway_control_operation', forbidden)
        status, result = read_status(server, row, receipt.operation_id)
        assert status == 200 and result == ops.gateway_control_operation_status_payload(receipt), result
        assert path.read_text() and not calls and not delivered
        assert server.agent is (agent if ready else None)
        after = {p.relative_to(server.paths.root): p.read_bytes() for p in server.paths.root.rglob('*') if p.is_file()}
        assert before == after


def test_model_http_wrapper_preserves_upper_changed_error_mapping(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import model_metadata_transaction as models
    from agent_py_agent.agent.gateway_parts.multiprocess_global_write import GlobalWriteError
    from agent_py_agent.agent.gateway_parts.multiprocess_http_transport import (
        HTTPHandlerWorkChanged,
    )
    def changed(*args, **kwargs):
        raise HTTPHandlerWorkChanged('original-upper-changed')
    monkeypatch.setattr('agent_py_agent.agent.gateway_parts.multiprocess_http_transport.require_http_handler_work', changed)
    with pytest.raises(GlobalWriteError) as actual:
        models.require_model_http_work(None, None, None)
    assert actual.value.reason == 'model_http_work_changed'


def test_model_http_wrapper_preserves_missing_work_error_mapping(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import model_metadata_transaction as models
    from agent_py_agent.agent.gateway_parts.multiprocess_global_write import GlobalWriteError
    def missing(*args, **kwargs):
        raise ValueError('original-work-unavailable')
    monkeypatch.setattr('agent_py_agent.agent.gateway_parts.multiprocess_http_transport.require_http_handler_work', missing)
    with pytest.raises(GlobalWriteError) as actual:
        models.require_model_http_work(None, None, None)
    assert actual.value.reason == 'model_http_work_unavailable'


def test_control_status_missing_http_work_preserves_legacy_public_code(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import multiprocess_http_transport as transport
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, _host, _path, row, _body, server, _runtime, _sent = case
        receipt, path = original_receipt(server, row, state='prepared')
        before = path.read_bytes()
        # LLM: 只模拟当前设施的普通失效，不创建权限或伪造source；原HTTP/G1仍完整执行。
        # 函数用途: 保证通用观察器重用后仍保留原模型状态接口公开错误码。
        def unavailable(*args, **kwargs):
            raise ValueError('original HTTP work unavailable')
        monkeypatch.setattr(transport, 'capture_http_handler_work', unavailable)
        status, result = read_status(server, row, receipt.operation_id)
        assert status == 503 and result == {'ok': False, 'error_code': 'GLOBAL_WRITE_REJECTED'}
        assert path.read_bytes() == before and not delivered and server.agent is None
