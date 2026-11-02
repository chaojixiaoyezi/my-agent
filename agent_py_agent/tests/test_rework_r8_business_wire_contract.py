"""R8-C2（P1-R8-02）原生业务线缆合同。

生产 business_client_for_agent → post_gateway_json → 真实临时 Gateway；原样回执交给 Python BusinessClient，
Node 参考 Web 经真实 fetch 访问同一回环 Gateway。只在 urllib 出口旁路记录离开客户端的字节，不替换生产运输；
Node 侧只补原认证头充当同源认证层。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from agent_py_agent.agent.gateway_parts.client_credentials import gateway_client_credentials
from agent_py_agent.agent.plugin_host.contracts import DomainResult
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli import chat_client_context
from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent, post_gateway_json
from agent_py_agent.cli.chat_parts.tui_background_agents import TuiBackgroundAgents
from agent_py_agent.cli.space_commands import (
    BusinessClient,
    BusinessCommandSession,
    PreparedBusinessCall,
    business_client_for_agent,
    consume_business_command,
)

APP = Path(__file__).resolve().parents[2] / 'examples/business_space_ui/app.js'
ALICE = OwnerIdentity.provider_user('feishu', 'business-alice')
GROUP = OwnerIdentity.provider_group('feishu', 'business-group')
MAIN = OwnerIdentity.local_main()
LOST = '结果丢失'
RECEIPT_FIELDS = ['allowed_actions', 'data', 'effect_outcome', 'error', 'operation_id', 'request_id', 'state', 'warnings']


class MainOwnerPort:
    """local/main 组合根注入的 W2 端口；回执仍由真实 execute_business 据 DomainResult(202) 生成。"""

    def __init__(self):
        self.keys = []

    def execute(self, action, params, *, actor, context, expected_revision=None, idempotency_key=''):
        self.keys.append(idempotency_key)
        if params['text'] == LOST:
            raise OSError('dispatch outcome lost after hand-off')
        return DomainResult(202, {'request_id': 'domain-' + idempotency_key, 'operation_id': 'op-' + idempotency_key,
                                  'target_thread_id': params['agent_id']})


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.settings import AgentConfig

    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'gateway-home'))
    agent = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=False, business_spaces_enabled=True,
                                    memory_path='memory.jsonl'), tmp_path / 'workspace')
    agent.business_background = MainOwnerPort()
    server = GatewayHTTPServer(0, gateway_paths_from_root(agent.home_paths.root / 'gateway'),
                               params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        yield SimpleNamespace(server=server, agent=agent, port=server.server.server_address[1], tmp=tmp_path)
    finally:
        server.stop()


@pytest.fixture
def wire(monkeypatch):
    """原样转发到真实 Gateway，只记录生产 urlopen 送出的路径、头和正文。"""
    sent = []
    forward = chat_client_context.urllib.request.urlopen

    def record(request, *args, **kwargs):
        sent.append({'path': urlsplit(request.full_url).path,
                     'headers': {key.lower(): value for key, value in request.header_items()},
                     'body': json.loads(request.data) if request.data else None})
        return forward(request, *args, **kwargs)

    monkeypatch.setattr(chat_client_context.urllib.request, 'urlopen', record)
    return sent


def thin_client(gateway, identity, *, port=None, token=''):
    """与 TUI 相同的薄客户端：凭据取宿主数据根的本机凭据，身份只来自 owner_identity。"""
    return GatewayChatClientAgent(SimpleNamespace(), SimpleNamespace(gateway_port=port or gateway.port,
                                  gateway_auth_token=token), gateway.tmp, [gateway.tmp],
                                  SimpleNamespace(root=gateway.agent.home_paths.root), owner_identity=identity)


def closed_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def run_node(gateway, script, *, identity=ALICE, **fixtures):
    node = shutil.which('node')
    assert node, '线缆合同必须由 Node 执行前端协议断言'
    auth = chat_client_context._gateway_headers(identity, credentials=gateway_client_credentials(thin_client(gateway, identity)))
    prelude = (f'const ui = require({json.dumps(str(APP))});\n'
               "const assert = require('node:assert/strict');\n"
               'const GATEWAY = process.env.R8_GATEWAY_URL, AUTH = JSON.parse(process.env.R8_GATEWAY_HEADERS);\n'
               'const live = (path, options) => fetch(GATEWAY + path, {...options, headers: {...options.headers, ...AUTH}});\n'
               f'const FIELDS = {json.dumps(RECEIPT_FIELDS)};\n')
    constants = ''.join(f'const {name} = {json.dumps(value, ensure_ascii=False)};\n' for name, value in fixtures.items())
    source = (prelude + constants + '(async () => {\n' + script
              + '\n})().then(() => process.exit(0), error => { console.error(error); process.exit(1); });\n')
    result = subprocess.run([node, '-e', source], capture_output=True, text=True, timeout=60,
                            env={**os.environ, 'R8_GATEWAY_URL': f'http://127.0.0.1:{gateway.port}',
                                 'R8_GATEWAY_HEADERS': json.dumps(auth)})
    assert result.returncode == 0, result.stdout + result.stderr


def command(session, raw, **body):
    return consume_business_command(raw + (' ' + json.dumps(body, ensure_ascii=False) if body else ''), session=session)


def test_production_client_space_create_list_get_and_same_key_replay(gateway, wire):
    session = BusinessCommandSession(business_client_for_agent(thin_client(gateway, ALICE)))
    created = command(session, '/space create', params={'name': '线缆空间'}, idempotency_key='wire-r8-space-create')
    assert (created['ok'], created['http_status'], created['state']) == (True, 200, 'completed')
    assert (created['error'], created['effect_outcome']) == (None, 'committed')
    sent = wire[-1]
    assert sent['path'] == '/client/business'
    assert sent['body'] == {'action': 'spaces.create', 'params': {'name': '线缆空间'},
                            'request_id': created['request_id'], 'idempotency_key': 'wire-r8-space-create'}
    assert (sent['headers']['x-user-id'], sent['headers']['x-channel']) == ('business-alice', 'feishu')
    assert sent['headers']['x-gateway-token']
    space_id = created['data']['space_id']
    listed = command(session, '/space list')
    assert listed['ok'] is True and [row['space_id'] for row in listed['data']['items']] == [space_id]
    fetched = command(session, '/space get ' + space_id)
    assert fetched['ok'] is True and fetched['data']['space']['space_id'] == space_id
    replay = session.client.prepare('spaces.create', {'name': '线缆空间'}, idempotency_key='wire-r8-space-create')
    for _ in range(2):
        again = session.client.send(replay)
        assert again['ok'] is True and again['data'] == created['data']
        assert wire[-1]['body'] == replay.payload()
    assert [row['space_id'] for row in command(session, '/space list')['data']['items']] == [space_id]


@pytest.mark.parametrize('extra', [{'user_id': 'business-alice'}, {'channel': 'feishu'},
                                   {'metadata': {'channel_chat_type': 'group'}}, {'params': {'owner_id': 'business-bob'}}])
def test_extra_root_or_param_fields_still_fail_closed(gateway, wire, extra):
    agent = thin_client(gateway, ALICE)
    payload = {'action': 'spaces.list', 'request_id': 'wire-r8-invalid', 'params': {}, **extra}
    original = json.loads(json.dumps(payload))
    status, body = post_gateway_json(agent, '/client/business', payload, timeout=10.0)
    assert (status, body['state'], body['error']['code'], body['effect_outcome']) == (400, 'failed', 'INVALID_SCHEMA', 'not_started')
    assert payload == original and wire[-1]['body'] == original, '原样发送，既不补身份也不删字段'
    result = business_client_for_agent(agent).send(PreparedBusinessCall(json.dumps(payload)))
    assert (result['ok'], result['http_status'], result['state'], result['effect_outcome']) == (False, 400, 'failed', 'not_started')
    assert result['error']['code'] == 'INVALID_SCHEMA'
    before = len(wire)
    with pytest.raises(HostAPIError):
        business_client_for_agent(agent).prepare('spaces.list', {'owner_id': 'business-bob'})
    assert len(wire) == before


@pytest.mark.parametrize('identity', [ALICE, GROUP], ids=['personal', 'group'])
def test_identity_stays_in_headers_while_other_posts_keep_body_identity(gateway, wire, identity):
    agent = thin_client(gateway, identity)
    result = business_client_for_agent(agent).call('spaces.list', {})
    assert (result['ok'], result['state'], result['http_status']) == (True, 'completed', 200)
    expected = {'x-user-id': identity.owner_id, 'x-channel': identity.provider}
    if identity.owner_kind == 'group':
        expected.update({'x-channel-chat-type': 'group', 'x-channel-chat-id': identity.owner_id})
    business = wire[-1]
    assert {key: business['headers'].get(key) for key in expected} == expected
    assert sorted(business['body']) == ['action', 'params', 'request_id']
    agent.request_chat_history('wire-r8-session', max_turns=1)
    other = wire[-1]
    assert other['path'] == '/client/history'
    assert (other['body']['user_id'], other['body']['channel']) == (identity.owner_id, identity.provider)
    assert other['body'].get('metadata') == ({'channel_chat_type': 'group', 'channel_chat_id': identity.owner_id}
                                             if identity.owner_kind == 'group' else None)
    assert {key: other['headers'].get(key) for key in expected} == expected


def test_python_projection_keeps_real_receipt_facts(gateway):
    alice = business_client_for_agent(thin_client(gateway, ALICE))
    main = business_client_for_agent(thin_client(gateway, MAIN))
    prepared = main.prepare('background_agents.messages.send', {'agent_id': 'wire-agent', 'text': '原样受理', 'input_kind': 'message'})
    accepted = main.send(prepared)
    assert (accepted['ok'], accepted['http_status'], accepted['state'], accepted['effect_outcome']) == (True, 202, 'accepted', 'running')
    key = prepared.payload()['idempotency_key']
    assert accepted['operation_id'] == accepted['data']['operation_id'] == 'op-' + key
    assert (accepted['request_id'], accepted['idempotency_key'], accepted['warnings']) == (prepared.payload()['request_id'], key, [])
    unknown = main.send(main.prepare('background_agents.messages.send', {'agent_id': 'wire-agent', 'text': LOST, 'input_kind': 'message'}))
    assert (unknown['ok'], unknown['http_status'], unknown['state'], unknown['effect_outcome']) == (False, 503, 'failed', 'unknown')
    assert (unknown['error']['code'], unknown['error']['retryable']) == ('OUTCOME_UNKNOWN', True)
    denied = alice.send(alice.prepare('approvals.decide', {'approval_id': 'wire-approval', 'decision': 'approved',
                                                           'response_ticket': 'wire-ticket'}, expected_revision=0))
    assert (denied['ok'], denied['http_status'], denied['state'], denied['effect_outcome']) == (False, 403, 'failed', 'not_started')
    assert denied['error']['code'] == 'CAPABILITY_DENIED'
    space = alice.call('spaces.create', {'name': '冲突空间'})
    assert space['ok'] is True and space['effect_outcome'] == 'committed'
    conflict = alice.send(alice.prepare('spaces.update', {'space_id': space['data']['space_id'], 'name': '改名'}, expected_revision=99))
    assert (conflict['ok'], conflict['http_status'], conflict['state'], conflict['effect_outcome']) == (False, 409, 'failed', 'not_started')
    assert conflict['error']['code'] == 'REVISION_CONFLICT'
    forged = business_client_for_agent(thin_client(gateway, ALICE, token='not-the-gateway-credential')).call('spaces.list', {})
    assert (forged['ok'], forged['http_status'], forged['state'], forged['effect_outcome']) == (False, 403, 'unknown', 'unknown')
    assert forged['error'] == {'code': 'BUSINESS_RESPONSE_INVALID'} and forged['body']['error'] == 'forbidden'
    offline = business_client_for_agent(thin_client(gateway, ALICE, port=closed_port()))
    lost = offline.prepare('spaces.create', {'name': '离线空间'})
    result = offline.send(lost)
    assert (result['ok'], result['http_status'], result['state'], result['effect_outcome']) == (False, 0, 'unknown', 'unknown')
    assert result['error'] == {'code': 'BUSINESS_TRANSPORT_UNKNOWN'}
    assert (result['request_id'], result['idempotency_key']) == (lost.payload()['request_id'], lost.payload()['idempotency_key'])
    assert gateway.agent.business_background.keys == [key, unknown['idempotency_key']]


def test_node_consumes_real_200_202_and_error_envelopes(gateway):
    run_node(gateway, r'''
 const api = new ui.BusinessClient(live);
 const listed = await api.call('spaces.list', {});
 assert.deepEqual(Object.keys(listed).sort(), FIELDS, '回执原样返回，没有 ok 字段');
 assert.equal(listed.state, 'completed'); assert.equal(listed.error, null); assert.equal(listed.effect_outcome, 'none');
 const created = await api.call('spaces.create', {name: 'Node 空间'}, {mutating: true});
 assert.equal(created.state, 'completed'); assert.equal(created.effect_outcome, 'committed');
 const rejected = async (promise, status, code) => {
   const error = await promise.then(() => null, e => e);
   assert.ok(error, code + ' 被当成功'); assert.equal(error.status, status); assert.ok(error.message.includes('拒绝'));
   assert.deepEqual(Object.keys(error.body).sort(), FIELDS, 'HTTP 错误正文仍可读');
   assert.equal(error.body.state, 'failed'); assert.equal(error.body.error.code, code);
   assert.equal(error.body.effect_outcome, 'not_started');
 };
 await rejected(api.call('spaces.list', {owner_id: 'business-bob'}), 400, 'INVALID_SCHEMA');
 await rejected(api.call('approvals.decide', {approval_id: 'wire-approval', decision: 'approved', response_ticket: 'wire-ticket'},
   {mutating: true, expected_revision: 0}), 403, 'CAPABILITY_DENIED');
 await rejected(api.call('spaces.update', {space_id: created.data.space_id, name: '改名'}, {mutating: true, expected_revision: 99}),
   409, 'REVISION_CONFLICT');
 const forged = new ui.BusinessClient((path, options) => fetch(GATEWAY + path,
   {...options, headers: {...options.headers, ...AUTH, 'X-Gateway-Token': 'not-the-gateway-credential'}}));
 const denied = await forged.call('spaces.list', {}).then(() => null, e => e);
 assert.equal(denied.status, 403); assert.equal(denied.body.error, 'forbidden');
''')
    run_node(gateway, r'''
 const api = new ui.BusinessClient(live);
 const accepted = await api.call('background_agents.messages.send', {agent_id: 'wire-agent', text: '原样受理', input_kind: 'message'},
   {mutating: true});
 assert.deepEqual(Object.keys(accepted).sort(), FIELDS);
 assert.equal(accepted.state, 'accepted'); assert.equal(accepted.effect_outcome, 'running'); assert.equal(accepted.error, null);
 assert.equal(accepted.operation_id, accepted.data.operation_id);
 const params = {agent_id: 'wire-agent', text: LOST, input_kind: 'message'};
 const first = api.prepare('background_agents.messages.send', params, {mutating: true});
 const unknown = await api.send(first).then(() => null, e => e);
 assert.equal(unknown.status, 503); assert.ok(unknown.message.includes('未确认'));
 assert.equal(unknown.body.error.code, 'OUTCOME_UNKNOWN'); assert.equal(unknown.body.effect_outcome, 'unknown');
 assert.equal(api.prepare('background_agents.messages.send', params, {mutating: true}), first, '未知结果只复用原准备对象');
''', identity=MAIN, LOST=LOST)


def test_unknown_create_reconciles_with_the_exact_prepared_request(gateway, wire):
    production = business_client_for_agent(thin_client(gateway, ALICE))
    dropped = []

    def lose_first_response(path, payload, *, timeout):
        reply = production.transport(path, payload, timeout=timeout)
        if not dropped:
            dropped.append(reply)
            raise OSError('response lost after the Gateway committed')
        return reply

    api = BusinessClient(lose_first_response)
    prepared = api.prepare('spaces.create', {'name': '未知结果空间'})
    unknown = api.send(prepared)
    assert (unknown['ok'], unknown['http_status'], unknown['state'], unknown['effect_outcome']) == (False, 0, 'unknown', 'unknown')
    assert (unknown['request_id'], unknown['idempotency_key']) == (prepared.payload()['request_id'],
                                                                   prepared.payload()['idempotency_key'])
    assert dropped[0][0] == 200, '服务端已提交，只是回执在路上丢失'
    settled = api.send(prepared)
    assert settled['ok'] is True and settled['data'] == dropped[0][1]['data']
    assert wire[-2]['body'] == wire[-1]['body'] == prepared.payload(), '核对只重送同一准备对象'
    names = [row['name'] for row in production.call('spaces.list', {})['data']['items']]
    assert names.count('未知结果空间') == 1
    run_node(gateway, r'''
 let lose = true;
 const api = new ui.BusinessClient(async (path, options) => {
   const response = await live(path, options);
   if (lose) { lose = false; await response.text(); throw new Error('response lost after commit'); }
   return response;
 });
 const params = {name: 'Node 未知结果空间'};
 const first = api.prepare('spaces.create', params, {mutating: true});
 const unknown = await api.send(first).then(() => null, e => e);
 assert.equal(unknown.status, 0); assert.equal(unknown.body.request_id, first.request_id);
 const retry = api.prepare('spaces.create', params, {mutating: true});
 assert.equal(retry, first, '未知结果只复用原准备对象');
 assert.equal((await api.send(retry)).state, 'completed');
 const listed = await api.call('spaces.list', {});
 assert.equal(listed.data.items.filter(row => row.name === params.name).length, 1, '没有静默新建第二个操作');
 assert.notEqual(api.prepare('spaces.create', params, {mutating: true}).body, first.body);
''')


def test_tui_roster_and_workflow_numbers_consume_real_rows(gateway):
    agent = thin_client(gateway, ALICE)
    session = BusinessCommandSession(business_client_for_agent(agent))
    space = command(session, '/space create', params={'name': '名册空间'}, idempotency_key='wire-r8-roster-space')['data']
    member = command(session, '/team create', params={'space_id': space['space_id'], 'name': '名册成员'},
                     idempotency_key='wire-r8-roster-member')
    assert member['ok'] is True
    roster = TuiBackgroundAgents(business_client_for_agent(agent))
    roster.refresh()
    assert [row['agent_id'] for row in roster.rows] == [member['data']['agent']['agent_id']]
    assert (roster.count, roster.coverage, roster.read_denied) == (1, 'complete', False)
    assert any('名册成员' in line and space['space_id'] in line for line in roster.lines())
    definition = {'descriptor': {'id': 'wire-flow', 'kind': 'workflow', 'name': '线缆流程', 'access_targets': [],
                                 'data_sources': [], 'credential_refs': [], 'io_contract': {}}, 'steps': []}
    assert command(session, '/workflow create', params={'definition': definition, 'scope': {
        'scope_mode': 'spaces', 'space_ids': [space['space_id']]}}, idempotency_key='wire-r8-workflow')['ok'] is True
    listed = command(session, '/workflow list')
    assert listed['ok'] is True and listed['display_rows'] == (('1 线缆流程', space['space_id']),)
    fetched = command(session, '/workflow get 1')
    assert fetched['ok'] is True
    assert fetched['data']['workflow']['workflow_ref'] == listed['data']['items'][0]['workflow_ref'] == 'wire-flow'


def test_web_recovery_reads_real_events_with_bounded_canonical_continuation(gateway):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path

    client = business_client_for_agent(thin_client(gateway, ALICE))
    session = BusinessCommandSession(client)
    space = command(session, '/space create', params={'name': '观察空间'}, idempotency_key='wire-r8-events-space')['data']
    other = command(session, '/space create', params={'name': '过滤空间'}, idempotency_key='wire-r8-events-other')['data']
    member = command(session, '/team create', params={'space_id': space['space_id'], 'name': '观察成员'},
                     idempotency_key='wire-r8-events-member')['data']['agent']
    assigned = client.send(client.prepare('tasks.assign', {'agent_id': member['agent_id'], 'space_id': space['space_id'],
                                                           'instruction': '产生真实事件'}, idempotency_key='wire-r8-events-task'))
    assert assigned['ok'] is True
    [services] = [item for item in gateway.server._business_services.values() if item.owner_id.endswith('/business-alice')]
    task = services.host.conversation_store.session_tasks.require(assigned['data']['task_id'])
    runtime = RuntimeRepository(runtime_db_path(services.owner_home))
    run = runtime.record_run_creation(owner_id=services.owner_id, run_id='wire-' + task.task_id, thread_id=task.target_thread_id,
                                      task_run_metadata={'business_context': task.metadata.get('business_context'),
                                                         'session_task_ref': task.task_id})
    for _ in range(120):
        runtime.append_event(event_type='attempt.progress', attempt_id=run['attempt_id'],
                             agent_run_id=run['agent_run_id'], task_run_id=run['task_run_id'])
    with runtime._runtime_connection() as conn:
        event_ids = [row[0] for row in conn.execute('SELECT event_id FROM runtime_events ORDER BY seq')]
    assert len(event_ids) > 100
    run_node(gateway, r'''
 const served = [];
 const api = new ui.BusinessClient(async (path, options) => {
   const response = await live(path, options);
   served.push({params: JSON.parse(options.body).params, data: (await response.clone().json()).data});
   return response;
 });
 const ids = items => items.filter(item => item.stream_item === 'event').map(item => item.event_id);
 const updates = [];
 const observation = new ui.BusinessObservation(api, item => updates.push(item), {space_id: SPACE});
 observation.maxCatchUpPages = 2;
 await observation.recover();
 assert.equal(served.length, 2, '积压超出预算时按页数收口');
 assert.deepEqual(ids(updates), EVENT_IDS.slice(0, 100));
 assert.equal(observation.coverage, 'partial'); assert.equal(observation.cursor, served[1].data.next_cursor);
 await observation.recover();
 assert.equal(served[2].params.cursor, served[1].data.next_cursor, '从服务续点继续，不解析游标');
 assert.deepEqual(ids(updates), EVENT_IDS, '原 event_id 与顺序完整');
 assert.equal(served.length, 4); assert.deepEqual(served[3].data.events, []);
 assert.ok(served[3].data.next_cursor, '当前末尾仍返回非空游标');
 assert.equal(observation.cursor, served[3].data.next_cursor); assert.equal(observation.coverage, served[3].data.coverage);
 await observation.recover();
 assert.equal(served.length, 5, '持续非空游标在当前末尾一次请求收口');
 assert.equal(served[4].params.cursor, served[3].data.next_cursor); assert.deepEqual(served[4].data.events, []);
 const filtered = new ui.BusinessObservation(api, item => updates.push(item), {space_id: OTHER});
 await filtered.recover();
 assert.deepEqual(served[5].data.events, []);
 assert.equal(filtered.cursor, served[5].data.next_cursor, '过滤后为空的页也保存扫描续点');
 await filtered.recover();
 assert.equal(served[6].params.cursor, served[5].data.next_cursor);
 observation.cursor = null; observation.maxCatchUpPages = 3;
 const before = updates.length;
 await observation.recover();
 assert.equal(served.length, 10); assert.deepEqual(ids(updates.slice(before)), [], '重扫已见事件按 event_id 去重');
''', SPACE=space['space_id'], OTHER=other['space_id'], EVENT_IDS=event_ids)
