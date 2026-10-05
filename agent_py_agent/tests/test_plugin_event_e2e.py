"""B4 隔离真 Gateway/Agent/工具链 + 假模型、握手声明 events 的假插件。"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from http.client import HTTPConnection

import pytest

from agent_py_agent.agent.agent_core.tool_loop import round_execution
from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision, ToolApprovalRequest
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import http_handlers, plugin_panels_http, request_worker
from agent_py_agent.agent.gateway_parts.control_operation_service import (
    execute_gateway_control_operation,
)
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.permission_bridge import write_gateway_permission_decision
from agent_py_agent.agent.gateway_parts.request_execution import _handle_gateway_request
from agent_py_agent.agent.gateway_parts.stream_approval import StreamApproval
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.models import ApprovalPolicy
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, owner_identity_from_config
from agent_py_agent.tests.test_plugin_event_gateway import _Handler, gateway
from agent_py_agent.tests.test_plugin_event_hub import _Harness, _installation
from agent_py_agent.tests.test_tool_runtime_unification import _CountingTool

_FIXTURES = (gateway,)
_PUBLISH = plugin_panels_http.publish_plugin_event
_FIELDS = {
    'prompt_submitted': {'request_id', 'chars', 'has_attachments'},
    'turn_started': {'request_id', 'model_name'},
    'turn_ended': {'request_id', 'status', 'duration_ms', 'tool_calls', 'error_code'},
    'tool_call_started': {'call_id', 'tool', 'effect', 'args_hash'},
    'tool_call_finished': {'call_id', 'tool', 'ok', 'error_code', 'failure_stage', 'duration_ms', 'handler_executed'},
    'command_executed': {'command', 'operation_id', 'state'},
}


class _Backend:
    name = 'offline-event-test'

    def __init__(self, tool_name, arguments):
        self.tool_name = tool_name
        self.calls = 0
        self.arguments = arguments

    def probe_tool_capability(self):
        return ProviderToolCapability(provider=self.name, endpoint='local://event-test', model='',
            stream=False, native_supported=True, evidence='isolated-script')

    def generate(self, prompt, on_chunk=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(text='', backend=self.name, tool_use_blocks=[
                {'id': 'event-native-call', 'name': self.tool_name, 'input': self.arguments}])
        native_results = [block for message in kwargs['messages'] for block in message.get('content', [])
                          if isinstance(block, dict) and block.get('type') == 'tool_result']
        assert len(native_results) == 1 and native_results[0]['tool_use_id'] == 'event-native-call'
        return ModelResponse(text='finished', backend=self.name)


# 只在客户端交互边界提交拒绝文件；真实发布、等待、binding 核验和执行器不替换。
def _reply_with_user_denial(monkeypatch, client_events):
    original = StreamApproval.request
    def request_with_reply(stream, request_value, **kwargs):
        publish = stream.publish
        def reply(event):
            publish(event)
            client_events.append(event)
            if event['kind'] == 'permission_requested':
                request = ToolApprovalRequest.from_mapping(event['permission'])
                write_gateway_permission_decision(stream.chunk_path, request,
                    ToolApprovalDecision(request.permission_id, 'denied'))
        stream.publish = reply
        try:
            return original(stream, request_value, **kwargs)
        finally:
            stream.publish = publish
    monkeypatch.setattr(StreamApproval, 'request', request_with_reply)


# 只读真实工具轮交回的裁决，不从模型文案判断成功或拒绝。
def _assert_canonical_result(execution, client_events, case):
    canonical = execution.result
    rejected = case != 'normal'
    expected_code = {'normal': '', 'type_invalid': 'TOOL_PARAMETER_TYPE_INVALID',
                     'policy_denied': 'COMMAND_DANGEROUS_PATTERN_BLOCKED', 'user_denied': 'APPROVAL_REJECTED'}[case]
    expected_stage = '' if case == 'normal' else 'validation' if case == 'type_invalid' else 'authorization'
    assert (canonical.error_code or '', canonical.failure_stage or '', canonical.handler_executed, canonical.ok) == (
        expected_code, expected_stage, not rejected, not rejected)
    if rejected:
        assert canonical.status == 'failed' and execution.decision.status == 'deny'
        assert execution.decision.reason_codes == (expected_code,)
    if case != 'user_denied':
        assert client_events == []
        return
    assert [event['kind'] for event in client_events] == ['permission_requested', 'permission_resolved']
    permission, resolved = client_events
    assert permission['permission']['binding']['args_hash'] == execution.call.args_hash
    assert resolved['permission_id'] == permission['permission']['permission_id']
    assert resolved['decision'] == canonical.metadata['approval_decision']['decision'] == 'denied'
    assert 'approval_pending' in execution.states and 'user_denied' in execution.states


# v2（tref2）：turn 类与 tool 类事件的 thread_ref 同源（本系统会话线程），command_executed 也带线程；
#   渠道会话哈希只在 Gateway 侧事件上有值。独立成函数，避免主用例超尺寸。
def _assert_v2_thread_refs(events, rejected):
    by_type = {event['type']: event for event in events}
    turn_ref = by_type['turn_started']['thread_ref']
    assert turn_ref and by_type['turn_ended']['thread_ref'] == turn_ref
    assert by_type['command_executed']['thread_ref'] == turn_ref
    if not rejected:
        assert by_type['tool_call_started']['thread_ref'] == turn_ref
        assert by_type['tool_call_finished']['thread_ref'] == turn_ref
        assert by_type['tool_call_started']['channel_conversation_ref'] == ''
        assert by_type['tool_call_finished']['channel_conversation_ref'] == ''
    channel_ref = by_type['turn_started']['channel_conversation_ref']
    assert channel_ref and by_type['turn_ended']['channel_conversation_ref'] == channel_ref
    assert by_type['prompt_submitted']['channel_conversation_ref'] == channel_ref
    assert by_type['command_executed']['channel_conversation_ref'] == channel_ref
    # 新会话第一次提交：提交时点也解析会话线程（与执行路径同一预检入口，get_or_create 幂等），
    # prompt_submitted 与回合事件同一非空引用。
    assert by_type['prompt_submitted']['thread_ref'] == turn_ref


def _prepare_case(agent, monkeypatch, case):
    tool = _CountingTool(command=case == 'policy_denied')
    if case in {'policy_denied', 'user_denied'}:
        tool.runtime_policy = replace(tool.runtime_policy,
            approval_policy=ApprovalPolicy('never' if case == 'policy_denied' else 'always'))
    agent.tools.register(tool)
    arguments = {'command': 'rm -rf /'} if case == 'policy_denied' else {'value': 7 if case == 'type_invalid' else 'output-private-marker'}
    agent.backend = _Backend(tool.model_spec.name, arguments)
    recorded, client_events = [], []
    original_record = round_execution._record_execution
    def record_probe(*args):
        recorded.append(args[-1])
        return original_record(*args)
    monkeypatch.setattr(round_execution, '_record_execution', record_probe)
    _reply_with_user_denial(monkeypatch, client_events)
    return tool, recorded, client_events


@pytest.mark.parametrize('case', ['normal', 'type_invalid', 'policy_denied', 'user_denied'])
def test_six_events_reach_handshake_plugin_on_real_gateway_turn(gateway, monkeypatch, case):
    agent, paths, server, _observed = gateway
    # 总开关由 gateway 夹具在配置实例上打开（B7 起是正式配置字段）。
    tool, recorded, client_events = _prepare_case(agent, monkeypatch, case)
    harness = _Harness(rows=[_installation(event_types=tuple(_FIELDS))])
    server.plugin_event_hub = harness.hub
    server.plugin_channel_pool = harness.pool
    monkeypatch.setattr(plugin_panels_http, 'publish_plugin_event', _PUBLISH)
    try:
        handler = _Handler({'prompt': 'prompt-private-marker', 'conversation_id': 'e2e-thread',
                            'client_capabilities': {'tool_approval': True}})
        http_handlers.handle_ask(handler, server, lambda: 'event-e2e')
        assert handler.replies[-1][0] == 202
        request_id = handler.replies[-1][1]['request_id']
        path = paths.inbox / f'{request_id}.json'
        request = json.loads(path.read_text())
        request.update(status='processing', turn_phase='open', execution_attempt_id='e2e-attempt')
        path = paths.processing / path.name
        path.write_text(json.dumps(request))
        result = _handle_gateway_request(agent, path)
        assert result['ok'], json.dumps(result, ensure_ascii=False)
        assert agent.backend.calls >= 2
        rejected = case != 'normal'
        assert tool.executions == (0 if rejected else 1)
        assert len(recorded) == 1
        _assert_canonical_result(recorded[0], client_events, case)
        command = parse_conversation_control('/verbose on', reject_unknown_slash=True)
        receipt = execute_gateway_control_operation(agent, paths, command,
            GatewayControlScope(user_id='admin', channel='chat', conversation_id='e2e-thread',
                metadata={'message_id': 'e2e-control'}), command_text='/verbose on')
        assert receipt.state == 'completed'
        harness.drain()
        events = [event for plugin in harness.plugins for _, body in plugin.calls for event in body['events']]
        expected_types = set(_FIELDS) - ({'tool_call_started', 'tool_call_finished'} if rejected else set())
        assert {event['type'] for event in events} == expected_types
        public = {'event_id', 'type', 'seq', 'occurred_at', 'dropped_before', 'channel', 'thread_ref',
                  'channel_conversation_ref', 'actor', 'facts'}
        assert all(set(event) == public and set(event['facts']) == _FIELDS[event['type']] for event in events)
        # v2（tref2）：thread_ref 同源与渠道哈希断言集中在 helper（主用例控尺寸）。
        _assert_v2_thread_refs(events, rejected)
        assert 'private-marker' not in json.dumps(events)
        ended = next(event['facts'] for event in events if event['type'] == 'turn_ended')
        assert ended['status'] == 'done' and ended['tool_calls'] == 1
    finally:
        harness.hub.close()


# LLM: 只访问本测试服务绑定的随机回环端口；身份经真实 AuthMiddleware，不从请求正文选 owner。
# 函数用途: 用短连接提交或查询隔离 HTTP 服务，关闭连接且不访问生产端口。
def _http_json(port, path, *, body=None, identity=('local-agent', 'local')):
    connection = HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        headers = {'Content-Type': 'application/json', 'X-User-Id': identity[0], 'X-Channel': identity[1]}
        connection.request('GET' if body is None else 'POST', path,
                           body=None if body is None else json.dumps(body), headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


# LLM: 只轮询结构化完成条件，单调期限有界；短暂等待不是用固定睡眠假定后台已完成。
# 函数用途: 等待线程或队列真实完成，超时保留失败而不是放宽事件断言。
def _wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    pause = threading.Event()
    while not predicate():
        remaining = deadline - time.monotonic()
        assert remaining > 0, '隔离 Gateway 的结构化完成条件超时'
        pause.wait(min(0.01, remaining))


# LLM: 探针原样转交 claim 和 owner 解析，只记录返回事实，不手工搬队列、不伪造 attempt 或终态。
# 函数用途: 保留真实请求 worker 的认领和 owner 证据，供 HTTP 组合断言核对。
def _observe_http_worker(monkeypatch):
    claims, resolved = [], []
    original_claim = request_worker.claim_request
    original_resolve = request_worker._resolve_request_agent
    def claim_probe(*args):
        result = original_claim(*args)
        if result is not None:
            claims.append(result)
        return result
    def resolve_probe(agent, payload):
        result = original_resolve(agent, payload)
        resolved.append(owner_identity_from_config(result.config))
        return result
    monkeypatch.setattr(request_worker, 'claim_request', claim_probe)
    monkeypatch.setattr(request_worker, '_resolve_request_agent', resolve_probe)
    return claims, resolved


@pytest.fixture
def http_gateway(tmp_path, monkeypatch):
    baseline_threads = set(threading.enumerate())
    # B7 起 plugin_events_enabled 是正式配置字段，类属性会被实例默认值 False 盖住；按真实配置入口打开总开关。
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home'),
                        gateway_per_user_owner_scoping=True, plugin_events_enabled=True), tmp_path)
    paths = gateway_paths(agent)
    request_worker.ensure_gateway_folders(paths)
    harness = _Harness(rows=[_installation(event_types=('prompt_submitted', 'turn_started', 'turn_ended'))])
    # 只有基础 owner 安装了假插件，不能靠全 owner 共用安装表掩盖串户。
    monkeypatch.setattr(harness.hub, '_installations', lambda owner:
        tuple(harness.rows) if owner.home_dir == agent.home_paths.owner_home_dir else ())
    published = []
    def publish_probe(server, owner, event):
        published.append((owner, event))
        return _PUBLISH(server, owner, event)
    monkeypatch.setattr(plugin_panels_http, 'publish_plugin_event', publish_probe)
    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(
        agent=agent, auth_middleware=AuthMiddleware(AuthManager())))
    server.plugin_event_hub, server.plugin_channel_pool = harness.hub, harness.pool
    workers = []
    try:
        server.start()
        port = server.server.server_address[1]
        assert port != 8420 and server.server.server_address[0] == '127.0.0.1'
        yield agent, paths, server, harness, published, workers
    finally:
        for worker in workers:
            worker.join(timeout=10)
        server.stop()
        _wait_for(lambda: not (set(threading.enumerate()) - baseline_threads))
        assert not any(worker.is_alive() for worker in workers)
        assert server.server is None and server._thread is None


def _assert_http_events(events, request_id, prompt):
    expected = ['prompt_submitted', 'turn_started', 'turn_ended']
    assert [event['type'] for event in events] == expected
    public = {'event_id', 'type', 'seq', 'occurred_at', 'dropped_before', 'channel', 'thread_ref',
              'channel_conversation_ref', 'actor', 'facts'}
    assert all(set(event) == public and set(event['facts']) == _FIELDS[event['type']] for event in events)
    assert [event['seq'] for event in events] == [1, 2, 3]
    assert len({event['event_id'] for event in events}) == 3
    # v2（tref2b）：提交时点也解析会话线程，三个事件同一非空 thread_ref；渠道会话哈希三个事件都有且一致。
    assert events[0]['thread_ref'] and events[0]['thread_ref'] == events[1]['thread_ref'] == events[2]['thread_ref']
    assert len({event['channel_conversation_ref'] for event in events}) == 1
    assert events[0]['channel_conversation_ref'] != ''
    assert all(event['actor'] == 'main' and event['channel'] == 'local' and event['dropped_before'] == 0 for event in events)
    assert all(event['facts']['request_id'] == request_id for event in events)
    assert events[0]['facts'] == {'request_id': request_id, 'chars': len(prompt), 'has_attachments': False}
    assert events[-1]['facts']['status'] == 'done' and events[-1]['facts']['tool_calls'] == 0
    assert events[-1]['facts']['error_code'] == '' and events[-1]['facts']['duration_ms'] >= 0
    assert 'private-marker' not in json.dumps(events)


@pytest.mark.parametrize(('identity', 'owner'), [
    (('local-agent', 'local'), OwnerIdentity.local_main()),
    (('alice', 'local'), OwnerIdentity.provider_user('local', 'alice')),
    (('alice', 'tui'), OwnerIdentity.provider_user('tui', 'alice')),
    (('alice', 'feishu'), OwnerIdentity.provider_user('feishu', 'alice')),
])
def test_real_http_worker_claim_and_prompt_owner_isolation(http_gateway, monkeypatch, identity, owner):
    agent, paths, server, harness, published, workers = http_gateway
    claims, resolved = _observe_http_worker(monkeypatch)
    port = server.server.server_address[1]
    prompt = 'http-private-marker'
    status, receipt = _http_json(port, '/ask', body={'prompt': prompt, 'conversation_id': 'http-private-marker',
        'metadata': {'user_id': 'local-agent', 'channel': 'chat'}}, identity=identity)
    assert status == 202 and receipt['status'] == 'queued'
    request_id = receipt['request_id']
    # 请求已由真实 HTTP 持久排队；随后启动真实扫描 worker，不直接调用回合处理或编辑队列记录。
    worker = threading.Thread(target=request_worker._process_gateway_requests,
        args=(agent, paths), kwargs={'worker_id': 'm1b4-http-worker'}, name='m1b4-http-worker')
    workers.append(worker)
    worker.start()
    _wait_for(lambda: not worker.is_alive())
    status, result = _http_json(port, '/result/' + request_id, identity=identity)
    assert status == 200 and result['ok'] and result['status'] == 'done', result
    assert len(claims) == 1 and claims[0].request_id == request_id
    assert claims[0].execution_attempt_id and claims[0].lease_epoch == 1
    assert resolved == [owner]
    assert not (paths.inbox / f'{request_id}.json').exists()
    assert not (paths.processing / f'{request_id}.json').exists()
    assert (paths.terminal / f'{request_id}.json').exists()
    harness.drain()
    events = [event for plugin in harness.plugins for _, body in plugin.calls for event in body['events']]
    if owner != OwnerIdentity.local_main():
        assert not [event for _, event in published if event.type == 'prompt_submitted']
        assert events == []
        return
    assert [event.type for _, event in published] == ['prompt_submitted', 'turn_started', 'turn_ended']
    _assert_http_events(events, request_id, prompt)
