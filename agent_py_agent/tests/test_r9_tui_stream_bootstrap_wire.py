"""真实瘦客户端建档、模型菜单与状态只读往返，仅隔离echo执行，无外部模型调用。"""
import pytest

from agent_py_agent.tests import test_s2w2_tui_network_mode as network_tests

startup_gateway = network_tests.startup_gateway
_startup_args = network_tests._startup_args


@pytest.mark.parametrize('first', ['models', 'control_model', 'permissions'])
def test_tui_first_metadata_thread_is_stream(startup_gateway, first, monkeypatch):
    from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.cli.chat_client_context import make_gateway_chat_client
    from agent_py_agent.cli.chat_parts.control_runtime import (
        ChatControlExecution,
        ChatControlState,
        execute_chat_control,
    )
    from agent_py_agent.cli.chat_parts.tui_model_menu import _request_data_sync
    class Provider(EchoBackend):
        def generate(self, *args, **kwargs):
            return ModelResponse(text='synthetic complete reply', backend='echo')
    startup_gateway.host.backend = Provider()
    from agent_py_agent.cli.parser import build_parser
    workspace = startup_gateway.client_home.parent / 'new-project'
    workspace.mkdir(parents=True)
    arguments = build_parser().parse_args(['--config', str(startup_gateway.config),
        'chat', '--gateway', '--dir', str(workspace)])
    client = make_gateway_chat_client(arguments)
    session = 'fresh-' + first
    execution = ChatControlExecution(client, True, ChatControlState(False, 0, '', 0, session), tui_streaming=True)
    if first == 'control_model':
        assert execute_chat_control(execution, parse_conversation_control('/model')).ok
    elif first == 'permissions':
        assert client.request_permissions(session_id=session, operation='get')['ok']
    result = _request_data_sync(client, session, 'list', {})
    assert result['ok'], result
    thread_id = result.get('thread_id') or result.get('model_execution', {}).get('thread_id')
    store = startup_gateway.host.conversation_store
    thread = store.threads.load(thread_id) if thread_id else store.threads.resolve(channel='chat', channel_conversation_id=session, channel_user_id='local-agent')
    assert thread is not None, result
    assert thread.verbose_level == 'stream', thread.to_dict()
    assert thread.metadata.get('verbose_level_source') == 'tui_default'
    status = execute_chat_control(execution, parse_conversation_control('/status'))
    assert status.ok
    assert 'stream' in status.message, status.message
    from agent_py_agent.agent.gateway_parts import request_execution
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    from agent_py_agent.agent.gateway_parts.request_worker import _process_gateway_requests
    from agent_py_agent.cli.chat_parts.gateway_client import ChatRequestContent, submit_chat_request
    levels = []
    original_level = request_execution._set_gateway_verbose_level
    def observe_level(on_chunk, level, *args, **kwargs):
        levels.append(level)
        return original_level(on_chunk, level, *args, **kwargs)
    monkeypatch.setattr(request_execution, '_set_gateway_verbose_level', observe_level)
    request_id, _chunks, _response = submit_chat_request(gateway_paths(client),
        ChatRequestContent('synthetic echo request', [], [], False, False, False,
                           chat_session_id=session, tui_streaming=True), client)
    assert _process_gateway_requests(startup_gateway.host, startup_gateway.paths, worker_id='wire-test') == 1
    from agent_py_agent.cli.chat_parts.gateway_client import network_gateway_observer
    observer = network_gateway_observer(client)
    code, response = observer.request_json('GET', '/result/' + request_id, total_deadline_seconds=3)
    assert code == 200 and response.get('ok'), (response.get('error_code'), response.get('error'), response.get('response'))
    assert levels == ['stream'], '真实请求应将同代stream交给原过程输出writer'
    current = store.threads.load(thread.thread_id)
    assert current.verbose_level == 'stream' and current.metadata['verbose_level_source'] == 'tui_default'



def test_control_receipt_freezes_display_capability_and_preserves_legacy_replay(startup_gateway):
    import json
    from dataclasses import replace

    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts.control_operation_service import (
        GatewayControlOperationReceipt,
        _receipt_scope,
        control_input_matches_receipt,
        gateway_control_operation_identity,
        gateway_control_operation_path,
    )
    from agent_py_agent.agent.runtime_errors import DataCorruptionError
    from agent_py_agent.cli.chat_client_context import make_gateway_chat_client
    from agent_py_agent.cli.chat_parts.control_runtime import (
        ChatControlExecution,
        ChatControlState,
        execute_chat_control,
    )

    client = make_gateway_chat_client(_startup_args(startup_gateway))
    execution = ChatControlExecution(client, True, ChatControlState(False, 0, '', 0, 'receipt'), tui_streaming=True)
    command = parse_conversation_control('/status')
    first = execute_chat_control(execution, command, message_id='tui-display')
    assert first.ok and first.operation_id
    path = gateway_control_operation_path(startup_gateway.paths, first.operation_id)
    payload = json.loads(path.read_text())
    receipt = GatewayControlOperationReceipt.from_dict(payload)
    assert receipt.client_tui_streaming and _receipt_scope(receipt).metadata['client_capabilities'] == {'tui_streaming': True}
    assert execute_chat_control(execution, command, message_id='tui-display').operation_id == first.operation_id
    for replacement in (False, None, 1, 'true', {}):
        with pytest.raises(DataCorruptionError):
            GatewayControlOperationReceipt.from_dict({**payload, 'client_tui_streaming': replacement})
    changed = dict(payload)
    changed.pop('client_tui_streaming')
    with pytest.raises(DataCorruptionError):
        GatewayControlOperationReceipt.from_dict(changed)
    old_execution = replace(execution, tui_streaming=False)
    legacy = execute_chat_control(old_execution, command, message_id='legacy-display')
    assert legacy.ok
    old_payload = json.loads(gateway_control_operation_path(startup_gateway.paths, legacy.operation_id).read_text())
    assert 'client_tui_streaming' not in old_payload
    old_receipt = GatewayControlOperationReceipt.from_dict(old_payload)
    assert old_receipt.to_dict() == old_payload and not old_receipt.client_tui_streaming
    assert 'client_capabilities' not in _receipt_scope(old_receipt).metadata
    assert execute_chat_control(old_execution, command, message_id='legacy-display').operation_id == legacy.operation_id
    replay = execute_chat_control(execution, command, message_id='legacy-display')
    assert replay.ok and replay.operation_id == legacy.operation_id
    assert json.loads(gateway_control_operation_path(startup_gateway.paths, legacy.operation_id).read_text()) == old_payload
    assert not execute_chat_control(execution, parse_conversation_control('/model'), message_id='legacy-display').ok
    original_scope = _receipt_scope(old_receipt)
    retry_scope = replace(original_scope, metadata={**original_scope.metadata, 'client_capabilities': {'tui_streaming': True}})
    for key, value in (('expected_turn_id', 'different-turn'), ('target_control_message_id', 'different-target'),
                       ('expected_execution', {'thread_id': 'different-thread'})):
        changed_scope = replace(retry_scope, metadata={**retry_scope.metadata, key: value})
        _, changed_digest = gateway_control_operation_identity(changed_scope, '/status')
        assert not control_input_matches_receipt(old_receipt, changed_scope, '/status', changed_digest)
    changed_scope = replace(retry_scope, workspace={'cwd': '/different/workspace', 'roots': []})
    _, changed_digest = gateway_control_operation_identity(changed_scope, '/status')
    assert not control_input_matches_receipt(old_receipt, changed_scope, '/status', changed_digest)
