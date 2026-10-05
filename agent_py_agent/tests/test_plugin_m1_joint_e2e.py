# LLM: 联合用例只改变启动/安装适配与手工队列推进；handler/执行路径、握手、唯一执行器、B3/B2 与样例响应不替换。
# 模块用途: 把样例协议名字和真实链路一起核对；不等同启用链、沙箱或实际客户端验收。
"""M1 联合用例：隔离 handler/执行路径接真实样例，队列消费阶段手动推进。

只替换客户端启动/传输适配与安装表，不模拟样例响应；不安装启用真 owner 的插件，
不证明 B7 沙箱/断网或真实 TUI/IM。手动执行器只控制 B3 合并时机。
"""
from __future__ import annotations

import hashlib
import json
import os
import selectors
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import http_handlers, plugin_panels_http
from agent_py_agent.agent.gateway_parts.control_operation_service import (
    execute_gateway_control_operation,
)
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.request_execution import _handle_gateway_request
from agent_py_agent.agent.plugin_channel import PluginChannelPool
from agent_py_agent.agent.plugin_display.service import (
    DisplayWiring,
    PanelQuery,
    PluginDisplayService,
)
from agent_py_agent.agent.plugin_events.declarations import PLUGIN_EVENT_TYPES
from agent_py_agent.agent.plugin_events.hub import EventHubWiring, PluginEventHub
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.tooling.mcp_client import MCPServerConfig, MCPStdioClient
from agent_py_agent.agent.tooling.mcp_protocol import unwrap_jsonrpc
from agent_py_agent.tests.test_plugin_event_e2e import _FIELDS, _prepare_case
from agent_py_agent.tests.test_plugin_event_gateway import _Handler, gateway
from agent_py_agent.tests.test_plugin_event_hub import _ManualExecutor
from scripts.build_plugin_files_package import build_files_package

_FIXTURES = (gateway,)
_ROOT = Path(__file__).resolve().parents[2]
_PUBLISH = plugin_panels_http.publish_plugin_event


# LLM: 仅适配真实样例 stdio；不伪造握手或计数；帧限额与超时有界，EOF 自然退出，不 kill/ps/嵌套沙箱。
# 类用途: 让 B2 池可和临时样例进程对话；不代替产品 PluginMCPClient 的启用与资源管理验收。
class _SampleClient:
    # LLM: 构造不启动进程，激活只是测试冻结记录；不得把它写入真实安装表。
    # 函数用途: 保存临时样例与协议往返记录，供池和计数断言使用。
    def __init__(self, project, installation):
        self.project = project
        self.installation = installation
        self.activation_ref = SimpleNamespace(require=lambda: installation)
        self.capabilities = {}
        self.calls = []
        self.responses = []
        self.process = None
        self.identifier = 0
        self.buffer = b''

    # LLM: 只启动 tmp_path 样例；用生产 MCPStdioClient._handshake 核协议，不走产品安装/沙箱启动。
    # 函数用途: 建立真实 stdio 连接；握手数据从样例进程原样返回，不手工组能力。
    def start(self):
        entry = self.installation.manifest.entry
        executable = sys.executable if entry.interpreter == 'python3' else shutil.which(entry.interpreter)
        assert executable, entry.interpreter
        self.process = subprocess.Popen([executable, entry.command, *entry.args], cwd=self.project,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={'HOME': str(self.project), 'PYTHONDONTWRITEBYTECODE': '1'})
        handshake = MCPStdioClient(MCPServerConfig(name=self.installation.manifest.plugin_id,
            command=executable, connect_timeout=3))
        handshake._handshake(self)
        assert self.server_info['name'] == self.installation.manifest.plugin_id
        return self

    # LLM: 握手通知沿刚初始化的同一管道发送，通知不等待响应，不产工具或审批事件。
    # 函数用途: 让生产握手发送 initialized 帧，不复制宿主的握手裁定。
    def notify(self, method, params):
        frame = {'jsonrpc': '2.0', 'method': method, 'params': params}
        self.process.stdin.write((json.dumps(frame) + '\n').encode())
        self.process.stdin.flush()

    # LLM: before_send 由真池调用；此处仍执行原代次复查，用宿主协议解包真实响应，记录的是发出的结构化帧。
    # 函数用途: 在调用方给定期限内完成一帧请求；能力位或方法拼错不能靠假响应蒙混通过。
    def request(self, method, params, **options):
        authority_check = options.get('authority_check')
        if authority_check is not None:
            authority_check()
        self.identifier += 1
        frame = {'jsonrpc': '2.0', 'id': self.identifier, 'method': method, 'params': params}
        self.process.stdin.write((json.dumps(frame) + '\n').encode())
        self.process.stdin.flush()
        self.calls.append((method, json.loads(json.dumps(params))))
        response = self._response(time.monotonic() + options['timeout'])
        assert response['jsonrpc'] == '2.0' and response['id'] == self.identifier
        result = unwrap_jsonrpc(self.installation.manifest.plugin_id, response, method)
        self.responses.append((method, json.loads(json.dumps(result))))
        return result

    # LLM: fd 的读取有界，不做无限 readline；输入错误留给原协议解包或明确断言。
    # 函数用途: 在期限内收取真实样例的一行 JSON，超时或提前 EOF 直接失败。
    def _response(self, deadline):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b'\n' not in self.buffer:
                assert selector.select(max(0, deadline - time.monotonic())), '样例 stdio 响应超时'
                data = os.read(self.process.stdout.fileno(), 131072)
                assert data, '样例 stdio 提前 EOF'
                self.buffer += data
                assert len(self.buffer) <= 131072
        line, self.buffer = self.buffer.split(b'\n', 1)
        return json.loads(line)

    # LLM: EOF 自然退出后才置空句柄，池吞清理异常时 fixture 仍能发现未确认；不发终止信号。
    # 函数用途: 关闭本测试 stdin 并核退出码与 stderr，重复关闭已确认对象无副作用。
    def stop(self):
        if self.process is None:
            return
        process = self.process
        if not process.stdin.closed:
            process.stdin.close()
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b''
        process.stdout.close()
        process.stderr.close()
        self.process = None


# LLM: 复制只在 pytest tmp_path 内；默认字节来自真实样例，不修改仓库样例。
#   B2 按 owner/activation_id 复用连接，不同插件的临时激活必须唯一，不能让收紧连接冒充观察连接。
# 函数用途: 用 B1 实际构建/读包校验恢复 manifest；冻结每个样例独立的临时激活供共用池复核。
def _sample(tmp_path, name):
    project = tmp_path / name
    shutil.copytree(_ROOT / 'plugins' / name, project)
    declaration = json.loads((project / 'declaration.json').read_text())
    package = build_files_package(declaration, project, tmp_path / f'{name}.zip')
    manifest = inspect_plugin_package(package.read_bytes()).manifest
    assert manifest.to_payload()['schema_version'] == 'plugin_package.v8'
    row = SimpleNamespace(manifest=manifest,
        activation=SimpleNamespace(activation_id=f'joint-{manifest.plugin_id}-activation'), enabled=True)
    return project, row


@pytest.fixture
def watch(tmp_path):
    project, row = _sample(tmp_path, 'event-watch')
    clients = []
    projects = {row.manifest.plugin_id: project}
    def create(_owner, installation):
        client = _SampleClient(projects[installation.manifest.plugin_id], installation)
        clients.append(client)
        return client
    pool = PluginChannelPool(client_factory=create)
    executor = _ManualExecutor()
    hub = PluginEventHub(wiring=EventHubWiring(installations=lambda _owner: (row,), executor=executor, pool=pool))
    display_jobs = _ManualExecutor()
    display = PluginDisplayService(wiring=DisplayWiring(installations=lambda _owner: (row,),
        executor=display_jobs, pool=pool))
    bundle = SimpleNamespace(project=project, row=row, projects=projects, clients=clients, pool=pool, hub=hub,
        executor=executor, display=display, display_jobs=display_jobs)
    try:
        yield bundle
    finally:
        display.close()
        hub.close()
        pool.close()
        # 池会吞 stop 异常；由测试单独核自然退出，不能把未清理误写为成功。
        assert all(client.process is None for client in clients)


def _wire_gateway(gateway, watch, monkeypatch):
    agent, _paths, server, _observed = gateway
    monkeypatch.setattr(type(agent.config), 'plugin_events_enabled', True, raising=False)
    server.plugin_event_hub = watch.hub
    server.plugin_channel_pool = watch.pool
    monkeypatch.setattr(plugin_panels_http, 'publish_plugin_event', _PUBLISH)
    return _prepare_case(agent, monkeypatch, 'normal')[0]


# LLM: 不调用真实 request worker/claim；入队后手动复制为 processing，不能将此 helper 的结果外推到自动队列消费。
# 函数用途: 运行隔离 handler 与原执行路径，让真实样例收到事件；只用于联合组件测试。
def _normal_turn(gateway, tool):
    agent, paths, server, _observed = gateway
    handler = _Handler({'prompt': 'prompt-private-marker', 'conversation_id': 'joint-thread'})
    http_handlers.handle_ask(handler, server, lambda: 'joint-request')
    assert handler.replies[-1][0] == 202
    request_id = handler.replies[-1][1]['request_id']
    request = json.loads((paths.inbox / f'{request_id}.json').read_text())
    request.update(status='processing', turn_phase='open', execution_attempt_id='joint-attempt')
    processing = paths.processing / f'{request_id}.json'
    processing.write_text(json.dumps(request))
    result = _handle_gateway_request(agent, processing)
    assert result['ok'], result
    assert tool.executions == 1 and agent.backend.calls == 2
    _control(gateway, 'joint-control')


def _control(gateway, message_id):
    agent, paths, _server, _observed = gateway
    command = parse_conversation_control('/verbose on', reject_unknown_slash=True)
    receipt = execute_gateway_control_operation(agent, paths, command,
        GatewayControlScope(user_id='admin', channel='chat', conversation_id='joint-thread',
            metadata={'message_id': message_id}), command_text='/verbose on')
    assert receipt.state == 'completed'
    return receipt


def _owner(gateway):
    return SimpleNamespace(home_dir=gateway[0].home_paths.owner_home_dir)


def _panel(watch, owner):
    # 每次用新观察线程键绕过展示缓存，证明实际 render 只读，而非两次读了同一缓存。
    query = PanelQuery(owner, str(owner.home_dir), f'view-{len(watch.clients[0].calls)}', {},
        ((watch.row.manifest.plugin_id, watch.row.manifest.panels[0].id),))
    watch.display.panels(query)
    watch.display_jobs.run_all()
    panel = watch.display.panels(query)[0]
    assert panel['state'] == 'ready', panel
    method, raw = watch.clients[0].responses[-1]
    assert method == 'my-agent/display.render' and set(raw) == {'display'}
    assert set(raw['display']) == {'columns', 'rows'}
    assert all(len(row) == 3 and type(row[1]) is int and type(row[2]) is int for row in raw['display']['rows'])
    assert set(panel['display']) == {'kind', 'columns', 'rows', 'truncated'}
    assert panel['display']['kind'] == 'table' and panel['display']['truncated'] is False
    return panel['display']


def _counts(table):
    assert len(table['columns']) == 3
    assert len(table['rows']) == len(PLUGIN_EVENT_TYPES)
    return {row[0]: (int(row[1]), int(row[2])) for row in table['rows']}


def _delivered(watch):
    assert len(watch.clients) == 1
    return [event for method, params in watch.clients[0].calls
        if method == 'my-agent/events.observe' for event in params['events']]


def _assert_counts(watch, owner, events):
    received = Counter(event['type'] for event in events)
    dropped = Counter()
    for event in events:
        dropped[event['type']] += event['dropped_before']
    expected = {name: (received[name], dropped[name]) for name in PLUGIN_EVENT_TYPES}
    assert _counts(_panel(watch, owner)) == expected
    stats = watch.hub.stats(str(owner.home_dir))[watch.row.manifest.plugin_id]
    assert set(stats) == set(PLUGIN_EVENT_TYPES)
    for name, counts in expected.items():
        assert (stats[name]['delivered'], stats[name]['coalesced']) == counts
        assert stats[name]['failed'] == stats[name]['unavailable'] == 0


def _files(project):
    return {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in project.rglob('*') if path.is_file()}


def test_event_watch_real_gateway_six_counts_and_readonly(gateway, watch, monkeypatch):
    tool = _wire_gateway(gateway, watch, monkeypatch)
    before_files = _files(watch.project)
    _normal_turn(gateway, tool)
    watch.executor.run_all()
    client = watch.clients[0]
    assert client.capabilities['experimental'] == {
        'my-agent/events': {'versions': ['1']}, 'my-agent/display': {'versions': ['1']}}
    events = _delivered(watch)
    assert Counter(event['type'] for event in events) == Counter(dict.fromkeys(PLUGIN_EVENT_TYPES, 1))
    public = {'event_id', 'type', 'seq', 'occurred_at', 'dropped_before', 'channel', 'thread_ref',
              'channel_conversation_ref', 'actor', 'facts'}
    assert all(set(event) == public for event in events)
    assert all(set(event['facts']) == _FIELDS[event['type']] for event in events)
    assert all(event['dropped_before'] == 0 for event in events)
    assert 'private-marker' not in json.dumps(events)
    owner = _owner(gateway)
    _assert_counts(watch, owner, events)
    before_stats = watch.hub.stats(str(owner.home_dir))
    first = _panel(watch, owner)
    first['rows'][0][1] = '999'
    assert _counts(_panel(watch, owner)) == dict.fromkeys(PLUGIN_EVENT_TYPES, (1, 0))
    assert watch.hub.stats(str(owner.home_dir)) == before_stats
    assert _files(watch.project) == before_files
    assert tool.executions == 1
    assert sum(method == 'my-agent/display.render' for method, _ in client.calls) == 3
    assert len(watch.pool.connections) == 1


def test_event_watch_counts_hub_generated_dropped_before(gateway, watch, monkeypatch):
    tool = _wire_gateway(gateway, watch, monkeypatch)
    _normal_turn(gateway, tool)
    watch.executor.run_all()  # 先建立真实槽；启用前的历史不能冒充 dropped_before。
    receipts = [_control(gateway, f'coalesce-{index}') for index in range(3)]
    assert len({receipt.operation_id for receipt in receipts}) == 3
    watch.executor.run_all()
    events = _delivered(watch)
    commands = [event for event in events if event['type'] == 'command_executed']
    assert [event['dropped_before'] for event in commands] == [0, 2]
    assert commands[-1]['facts']['operation_id'] == receipts[-1].operation_id
    _assert_counts(watch, _owner(gateway), events)
    assert _counts(_panel(watch, _owner(gateway)))['command_executed'] == (2, 2)


def test_rm_guard_handshake_and_b1_manifest_only(tmp_path, gateway):
    project, row = _sample(tmp_path, 'rm-guard')
    assert row.manifest.to_payload()['tool_gates'] == json.loads((project / 'declaration.json').read_text())['tool_gates']
    gate = next(gate for gate in row.manifest.tool_gates if gate.id == 'guard-rm')
    assert (gate.tools, gate.effects, gate.arguments) == (('run_command',), (), 'full')
    assert 'run_command' in gateway[0].tools.tools
    client = _SampleClient(project, row)
    try:
        client.start()
        assert client.capabilities['experimental'] == {'my-agent/tool-gate': {'versions': ['1']}}
    finally:
        client.stop()


def test_node_event_watch_handshake_when_sample_exists(tmp_path):
    if shutil.which('node') is None:
        pytest.skip('本机没有 node')
    if not (_ROOT / 'plugins/event-watch-node/declaration.json').is_file():
        pytest.skip('仓库没有 Node event-watch 样例；rm-guard-node 不能冒充事件观察样例')
    project, row = _sample(tmp_path, 'event-watch-node')
    client = _SampleClient(project, row)
    try:
        client.start()
        assert client.capabilities['experimental'] == {
            'my-agent/events': {'versions': ['1']}, 'my-agent/display': {'versions': ['1']}}
    finally:
        client.stop()
