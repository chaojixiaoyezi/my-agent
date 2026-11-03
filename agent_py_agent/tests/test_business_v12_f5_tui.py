"""W12 消费冻结协议的组件验收；不把协议 fake 当真实领域联调。"""
from __future__ import annotations

import importlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts.business_api import (
    _response,
    bind_business_context,
    execute_business,
)
from agent_py_agent.agent.plugin_host.business_action_specs import (
    business_action_spec,
    business_defs,
    business_schema_bundle,
    standalone_schema,
)
from agent_py_agent.agent.plugin_host.contracts import DomainResult
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import validate_object
from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

ENVELOPE = standalone_schema(business_schema_bundle()['response_envelope_schema'])
# 领域替身的宿主身份只供真实 execute_business 复核；它不读写这个路径。
FIXTURE_HOST = SimpleNamespace(home_paths=SimpleNamespace(owner_id='local/tui-fixture',
    owner_home_dir=Path('/nonexistent/tui-business-fixture')), config=SimpleNamespace(business_spaces_enabled=True))
# 领域端口抛原错误；HTTP 状态、state 与 effect_outcome 仍由真实生产者映射。
DOMAIN_ERRORS = {403: lambda: BusinessError('BUSINESS_ACCESS_DENIED'),
                 409: lambda: BusinessError('REVISION_CONFLICT'),
                 503: lambda: OSError('dispatch outcome lost')}


def module(name):
    path = 'agent_py_agent.cli.' + name
    assert importlib.util.find_spec(path) is not None, f'尚未实现 {path}'
    return importlib.import_module(path)


def minimal(schema, **fields):
    """冻结 defs 的最小合法实例，只覆盖测试关心的字段；结果仍须通过冻结 schema。"""
    defs = business_defs()

    def build(node):
        if '$ref' in node:
            return build(defs[node['$ref'].removeprefix('#/$defs/')])
        if 'const' in node:
            return node['const']
        if 'enum' in node:
            return node['enum'][0]
        branches = node.get('anyOf') or node.get('oneOf')
        if branches:
            return None if any(branch.get('type') == 'null' for branch in branches) else build(branches[0])
        kinds = node.get('type') if isinstance(node.get('type'), list) else [node.get('type')]
        if 'null' in kinds:
            return None
        if kinds[0] == 'object':
            return {key: build(node['properties'][key]) for key in node.get('required', [])}
        if kinds[0] == 'array':
            return [build(node['items']) for _ in range(node.get('minItems', 0))]
        if kinds[0] == 'string':
            if 'pattern' in node:
                return '0' * 64
            return '2026-01-01T00:00:00Z' if node.get('format') == 'date-time' else 'x' * node.get('minLength', 0)
        if kinds[0] in ('integer', 'number'):
            return node['exclusiveMinimum'] + 1 if 'exclusiveMinimum' in node else node.get('minimum', 0)
        return False

    return validate_object({**build(schema), **fields}, standalone_schema(schema))


def dto(definition, /, **fields):
    return minimal({'$ref': '#/$defs/' + definition}, **fields)


def pending_receipt(payload):
    """冻结封套允许、原生生产者暂不发出的未完成回执；只作消费端负例，先过封套 schema。"""
    return validate_object({**_response(payload['request_id'], effect='not_started'), 'state': 'pending'}, ENVELOPE)


class ProtocolTransport:
    """领域页经真实 execute_business/_response 生成回执并过冻结封套 schema；自身兼作领域端口替身。"""

    def __init__(self, pages=None, status=200, receipt=None):
        self.calls = []
        self.pages = pages or {}
        self.status = status
        self.receipt = receipt
        self.owner_id = FIXTURE_HOST.home_paths.owner_id
        self.owner_home = Path(FIXTURE_HOST.home_paths.owner_home_dir).resolve()

    def __call__(self, path, payload, *, timeout):
        assert path == '/client/business'
        assert timeout > 0
        validate_object(payload, business_action_spec(payload['action']).native_request_schema)
        self.calls.append(json.loads(json.dumps(payload)))
        if not self.status:
            return 0, {}
        if self.receipt is not None:
            return self.status, validate_object(self.receipt(payload), ENVELOPE)
        context = bind_business_context(FIXTURE_HOST, authorize=lambda _action, _facts: True)
        status, body = execute_business(json.loads(json.dumps(payload)), context, self)
        return status, validate_object(body, ENVELOPE)

    def ready(self, action):
        return True

    def invoke(self, action, params, context, *, expected_revision=None, idempotency_key=''):
        if self.status in DOMAIN_ERRORS:
            raise DOMAIN_ERRORS[self.status]()
        spec = business_action_spec(action)
        accepted = self.status == 202 and spec.accepted_result_schema is not None
        data = self.pages.get(action)
        if callable(data):
            data = data(params)
        if data is None:
            data = minimal(spec.metadata['accepted_result_schema' if accepted else 'result_schema'])
        return DomainResult(202, data) if accepted else data


def client(transport):
    return module('space_commands').BusinessClient(transport)


def produced(action, params, *, status=200, pages=None, receipt=None, **options):
    """为 Node 替身生成一份真实生产者回执；替身只把 request_id 按原回显规则换成本次请求。"""
    transport = ProtocolTransport(pages, status=status, receipt=receipt)
    return transport('/client/business', client(transport).prepare(action, params, **options).payload(), timeout=1.0)


@pytest.mark.parametrize(('raw', 'action', 'params'), [
    ('/space list', 'spaces.list', {}),
    ('/space get sp_1', 'spaces.get', {'space_id': 'sp_1'}),
    ('/team list', 'background_agents.list', {}),
    ('/team get ag_1', 'background_agents.get', {'agent_id': 'ag_1'}),
    ('/workflow get wf_1', 'workflows.get', {'workflow_ref': 'wf_1'}),
    ('/workflow run {"workflow_ref":"wf_1","space_id":"sp_1"}',
     'workflows.run', {'workflow_ref': 'wf_1', 'space_id': 'sp_1'}),
])
def test_commands_share_catalog_and_business_transport(raw, action, params):
    transport = ProtocolTransport()
    commands = module('space_commands')
    session = commands.BusinessCommandSession(client(transport))
    result = commands.consume_business_command(raw, session=session)
    assert result['ok'] is True
    assert transport.calls[-1]['action'] == action
    assert transport.calls[-1]['params'] == params
    assert commands.consume_business_command('/agents', session=session) is None


def test_prepared_mutation_freezes_identity_and_has_no_automatic_retry():
    transport = ProtocolTransport(status=0)
    api = client(transport)
    params = {'space_id': 'sp_1', 'name': '设计'}
    prepared = api.prepare('background_agents.create', params)
    params['space_id'] = 'sp_other'
    assert prepared.payload()['params']['space_id'] == 'sp_1'
    result = api.send(prepared)
    assert result['ok'] is False and result['state'] == 'unknown'
    assert len(transport.calls) == 1
    api.send(prepared)
    assert transport.calls[0] == transport.calls[1]


def test_schema_rejects_prose_identity_and_mutation_without_revision():
    transport = ProtocolTransport()
    api = client(transport)
    with pytest.raises(HostAPIError):
        api.prepare('spaces.get', {'space_id': 'sp_1', 'owner_id': 'someone'})
    with pytest.raises(HostAPIError):
        api.prepare('spaces.update', {'space_id': 'sp_1', 'name': '改名'})
    assert transport.calls == []
    api.send(api.prepare('spaces.update', {'space_id': 'sp_1', 'name': '改名'}, expected_revision=2))
    assert transport.calls[0]['expected_revision'] == 2


def test_workflow_two_columns_keep_number_bound_to_exact_object():
    workflows = module('workflow_commands')
    table = workflows.WorkflowTable()
    first = table.update([{'workflow_ref': 'wf_A', 'name': 'A', 'scope': {'scope_mode': 'spaces', 'space_ids': ['sp_1']}},
                          {'workflow_ref': 'wf_B', 'name': 'B', 'scope': {'scope_mode': 'owner_global', 'space_ids': []}}])
    number = first[0][0].split()[0]
    second = table.update([{'workflow_ref': 'wf_B', 'name': 'B'}, {'workflow_ref': 'wf_A', 'name': 'A'}])
    assert all(len(row) == 2 for row in second)
    assert table.resolve(number) == 'wf_A'
    table.update([{'workflow_ref': 'wf_B', 'name': 'B'}])
    with pytest.raises(ValueError):
        table.resolve(number)


def test_team_list_preserves_unknown_counts_and_expands_only_on_demand():
    transport = ProtocolTransport({'background_agents.list': {
        'items': [dto('AgentView', agent_id='ag_A', thread_id='ag_A', space_id='sp_1',
                      name='设计', purpose='做界面', model_profile_id='model_1')],
        'next_cursor': None, 'coverage': 'partial'}})
    agents = module('chat_parts.tui_background_agents').TuiBackgroundAgents(client(transport))
    agents.refresh()
    assert agents.count is None and agents.running_count is None
    assert '未知' in '\n'.join(agents.lines())
    assert [x['action'] for x in transport.calls] == ['background_agents.list']
    agents.load_tree('sp_1', 'ag_A')
    agents.load_log('log_1')
    assert [x['action'] for x in transport.calls][-2:] == ['agent_tree.get', 'logs.read']


def test_navigation_adds_background_team_between_watch_and_goals_without_run_identity():
    nav = TuiAgentNavigationState(TuiRuntime('thread-main'))
    assert callable(getattr(nav, 'update_business_agents', None))
    transport = ProtocolTransport({'background_agents.list': {
        'items': [dto('AgentView', agent_id='ag_A', thread_id='ag_A', space_id='sp_1', name='设计')],
        'coverage': 'complete', 'next_cursor': None}})
    agents = module('chat_parts.tui_background_agents').TuiBackgroundAgents(client(transport))
    agents.refresh()
    nav.update_business_agents(agents)
    nav.update_goal_rows([{'goal_id': 'g1', 'name': '任务'}])
    assert nav.move_selection(1) and nav.snapshot().selected_run_id == 'business:agents'
    assert nav.enter_selected()
    assert nav.snapshot().business_list_active is True
    assert nav.move_selection(1) and nav.enter_selected()
    snapshot = nav.snapshot()
    assert snapshot.active_thread_id == 'ag_A'
    assert snapshot.active_run_id == '' and snapshot.can_send_input
    assert not nav.enter_selected(), '完整会话空 Enter 不能把根选择当子代理进入'
    assert nav.back() and nav.snapshot().business_list_active
    assert nav.back() and nav.snapshot().depth == 0
    assert not any(x['action'].endswith(('cancel', 'retire', 'suspend')) for x in transport.calls)


def test_thread_messages_are_paged_without_summary_or_duplicate_ids():
    def messages(params):
        assert params['target'] == {'kind': 'background_main', 'id': 'ag_A'}
        return {'items': [dto('MessageView', message_id='m2' if params.get('cursor') else 'm1',
                              thread_id='ag_A', role='assistant', text='完整正文')],
                'coverage': 'complete', 'next_cursor': None if params.get('cursor') else 'next',
                'history_boundary': dto('HistoryBoundary', coverage='complete')}
    transport = ProtocolTransport({'messages.list': messages})
    agents = module('chat_parts.tui_background_agents').TuiBackgroundAgents(client(transport))
    result = agents.conversation('ag_A')
    assert [x['message_id'] for x in result['items']] == ['m1', 'm2']
    assert len(transport.calls) == 2 and result['coverage'] == 'complete'


def test_background_input_is_prepared_before_return_and_targets_original_thread():
    transport = ProtocolTransport(status=202)
    agents = module('chat_parts.tui_background_agents').TuiBackgroundAgents(client(transport))
    prepared = agents.prepare_input('ag_A', '继续画设计稿')
    agents.refresh()
    result = agents.client.send(prepared)
    assert result['state'] == 'accepted'
    assert transport.calls[-1]['params'] == {'agent_id': 'ag_A', 'text': '继续画设计稿', 'input_kind': 'message'}


@pytest.mark.parametrize(('status', 'receipt', 'state', 'effect', 'code'), [
    (202, pending_receipt, 'pending', 'not_started', None),
    (403, None, 'failed', 'not_started', 'BUSINESS_ACCESS_DENIED'),
    (409, None, 'failed', 'not_started', 'REVISION_CONFLICT'),
    (503, None, 'failed', 'unknown', 'OUTCOME_UNKNOWN'),
    (0, None, 'unknown', 'unknown', 'BUSINESS_TRANSPORT_UNKNOWN'),
])
def test_pending_denied_and_unknown_receipts_keep_facts_without_success(status, receipt, state, effect, code):
    transport = ProtocolTransport(status=status, receipt=receipt)
    api = client(transport)
    prepared = api.prepare('background_agents.messages.send', {'agent_id': 'ag_A', 'text': '同一请求', 'input_kind': 'message'})
    result = api.send(prepared)
    assert result['ok'] is False and result['http_status'] == status
    assert (result['state'], result['effect_outcome']) == (state, effect)
    assert (result['error'] or {}).get('code') == code
    assert result['idempotency_key'] == prepared.payload()['idempotency_key']
    assert transport.calls == [prepared.payload()], '不确定回执不能自动重发或换新请求'


def test_invented_ok_and_contradictory_receipts_stay_unknown():
    completed = produced('spaces.list', {})[1]
    accepted = produced('background_agents.messages.send',
                        {'agent_id': 'ag_A', 'text': '已受理', 'input_kind': 'message'}, status=202)[1]
    other_action = produced('background_agents.messages.send',
                            {'agent_id': 'ag_A', 'text': '其它动作', 'input_kind': 'message'})[1]
    denied = produced('spaces.list', {}, status=403)[1]
    cases = [(200, {'ok': True, 'state': 'completed', 'data': completed['data']}),
             (200, {**completed, 'ok': True}), (202, completed), (200, accepted),
             (200, other_action), (503, completed), (200, denied)]
    for status, body in cases:
        api = client(lambda _path, _payload, *, timeout, reply=(status, body): reply)
        result = api.call('spaces.list', {})
        assert result['ok'] is False, (status, body)
        assert (result['state'], result['effect_outcome']) == ('unknown', 'unknown')
        assert result['error'] == {'code': 'BUSINESS_RESPONSE_INVALID'} and result['body'] == body
        assert result['http_status'] == status


def test_footer_orders_mode_watch_team_before_existing_controls():
    from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext, _render_footer
    runtime = TuiRuntime('thread-main')
    assert 'background_agent_count' in TuiRenderContext.__dataclass_fields__
    context = TuiRenderContext(width=200, background_process_count=1,
                               background_agent_count=4, background_agent_running_count=None)
    text = ''.join(x[1] for x in _render_footer(runtime.store.snapshot(), context))
    assert text.index('后台驻留') < text.index('后台Agent 4(运行未知)') < text.index('F4')


def run_web(script, **fixtures):
    path = Path(__file__).resolve().parents[2] / 'examples/business_space_ui/app.js'
    assert path.is_file(), '参考 Web 尚未实现'
    node = shutil.which('node')
    assert node, '此验收需要 Node 来运行前端协议测试'
    constants = ''.join(f'const {name} = {json.dumps(value, ensure_ascii=False)};\n' for name, value in fixtures.items())
    result = subprocess.run([node, '-e', f'const ui = require({json.dumps(str(path))});\n' + constants + script],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def events_page(events=(), *, next_cursor=None, coverage='partial', reset_required=False):
    """真实生产者生成的 events.list 回执（canonical events 页，不是虚构 items 页）。"""
    page = {'events': list(events), 'next_cursor': next_cursor, 'coverage': coverage, 'reset_required': reset_required}
    return produced('events.list', {'filter': {}}, pages={'events.list': page})[1]


def test_web_ndjson_split_unicode_gap_and_authenticated_range():
    run_web(r'''
const assert = require('node:assert/strict');
(async () => {
 const calls = [];
 const fake = async (path, options) => {
   calls.push({path, options});
   return {ok: true, status: 200, blob: async () => new Blob(['video']),
           json: async () => path === '/client/business' ? {...EVENTS, request_id: JSON.parse(options.body).request_id} : {}};
 };
 const api = new ui.BusinessClient(fake, () => 'csrf-for-test');
 const lines = [];
 const parser = new ui.NDJSONDecoder(x => lines.push(x));
 const raw = new TextEncoder().encode('{"kind":"heartbeat","text":"中文"}\n{"kind":"gap"}\n');
 for (const byte of raw) parser.feed(new Uint8Array([byte]));
 parser.finish();
 assert.equal(lines[0].text, '中文');
 const observation = new ui.BusinessObservation(api, () => {});
 observation.cursor = 'old';
 await observation.accept({stream_item:'gap'});
 assert.equal(JSON.parse(calls[0].options.body).action, 'events.list');
 await api.readArtifact('art/1', 'version/1', 'bytes=0-4');
 assert.equal(calls[1].options.headers.Range, 'bytes=0-4');
 assert.equal(calls[1].options.credentials, 'same-origin');
 assert.ok(calls[1].path.includes('art%2F1/content?version_ref=version%2F1'));
 const file = new Blob(['real-file']);
 await api.uploadArtifact(file, 'sp_1', 'abcdefghijklmnop');
 const fields = calls[2].options.body;
 assert.equal(fields.get('space_id'), 'sp_1');
 assert.equal(fields.get('idempotency_key'), 'abcdefghijklmnop');
 assert.equal(fields.has('owner_id'), false); assert.equal(fields.has('path'), false);
 assert.equal(calls[2].options.headers['X-CSRF-Token'], 'csrf-for-test');
})().catch(e => {console.error(e);process.exitCode=1;});
''', EVENTS=events_page())


def test_web_has_six_real_panels_and_no_sample_success():
    path = Path(__file__).resolve().parents[2] / 'examples/business_space_ui/index.html'
    assert path.is_file()
    html = path.read_text()
    for name in ('controller', 'materials', 'packages', 'team', 'executions', 'delivery'):
        assert f'data-panel="{name}"' in html
    run_web(r'''
const assert=require('node:assert/strict');
assert.equal(ui.displayUnknown(null), '未知');
assert.equal(ui.displayUnknown(0), '0');
const api = new ui.BusinessClient(async () => ({ok:false,status:401}), () => '');
api.call('spaces.list', {}).then(() => {throw Error('401被当成功')}, e => assert.equal(e.status,401));
const invented = new ui.BusinessClient(async () => ({ok:true,status:200,json:async()=>({ok:true,data:{items:[]}})}));
invented.call('spaces.list', {}).then(() => {throw Error('虚构 ok 回执被当成功')}, e => assert.equal(e.status,200));
''')


def test_up_starts_at_business_entry_not_last_goal():
    nav = TuiAgentNavigationState(TuiRuntime('main-up'))
    agents = module('chat_parts.tui_background_agents').TuiBackgroundAgents(client(ProtocolTransport()))
    nav.update_business_agents(agents)
    nav.update_goal_rows([{'goal_id': 'last', 'name': '任务'}])
    assert nav.move_selection(-1)
    assert nav.snapshot().selected_run_id == 'business:agents'


def test_business_list_terminal_text_is_sanitized_and_wrapped():
    from agent_py_agent.cli.chat_parts.tui_block_renderer import (
        TuiRenderContext,
        _render_background_activity,
    )
    nav = TuiAgentNavigationState(TuiRuntime('main-render'))
    agents = module('chat_parts.tui_background_agents').TuiBackgroundAgents(client(ProtocolTransport()))
    agents.rows = ({'agent_id': 'ag_A', 'name': '\x1b[31m名字' + '长' * 120},)
    nav.update_business_agents(agents)
    nav.move_selection(1)
    nav.enter_selected()
    block = next(x for x in nav.active_runtime().store.snapshot().active_blocks if x.block_id == 'business:team-list')
    lines = _render_background_activity(block, TuiRenderContext(width=50))
    assert all('\x1b' not in ''.join(x[1] for x in line) for line in lines)
    assert len(lines) > 4


def test_deferred_input_does_not_follow_navigation_back():
    import asyncio

    from prompt_toolkit.buffer import Buffer
    transport = ProtocolTransport({'background_agents.list': {
        'items': [dto('AgentView', agent_id='ag_A', thread_id='ag_A', space_id='sp_1')],
        'coverage': 'complete', 'next_cursor': None}})
    background = module('chat_parts.tui_background_agents')
    agents = background.TuiBackgroundAgents(client(transport))
    agents.refresh()
    nav = TuiAgentNavigationState(TuiRuntime('main-deferred'))
    nav.update_business_agents(agents)
    nav.move_selection(1)
    nav.enter_selected()
    nav.move_selection(1)
    nav.enter_selected()
    pending = []
    event = SimpleNamespace(app=SimpleNamespace(create_background_task=pending.append, invalidate=lambda: None))
    params = SimpleNamespace(agent_navigation=nav, input_area=SimpleNamespace(buffer=Buffer()))
    assert background.submit_business_input(event, params, '精确原目标')
    assert nav.back() and nav.back()
    asyncio.run(pending[0])
    assert transport.calls[-1]['params']['agent_id'] == 'ag_A'
    assert nav.snapshot().active_thread_id == ''


def test_web_expired_cursor_backfill_is_exact_and_never_accepts_prose_states():
    run_web(r'''
const assert=require('node:assert/strict');
(async()=>{
 const calls=[];
 const api=new ui.BusinessClient(async(path,o)=>{calls.push(JSON.parse(o.body));
   return {ok:true,status:200,json:async()=>({...EVENTS,request_id:JSON.parse(o.body).request_id})};});
 const obs=new ui.BusinessObservation(api,()=>{}, {space_id:'sp_1'}); obs.cursor='expired-old';
 await obs.accept({stream_item:'cursor_expired'});
 assert.deepEqual(calls[0].params, {filter:{space_id:'sp_1'}});
 assert.equal(obs.coverage, 'partial');
 await assert.rejects(obs.accept({stream_item:'完成'}));
 const decoder=new ui.NDJSONDecoder(()=>{}); decoder.feed(new TextEncoder().encode('{"half":'));
 assert.throws(()=>decoder.finish());
})().catch(e=>{console.error(e);process.exitCode=1;});
''', EVENTS=events_page())


def test_denial_clears_old_roster_instead_of_showing_revoked_space():
    transport = ProtocolTransport({'background_agents.list': {'items': [dto('AgentView', agent_id='ag_private')],
        'coverage': 'complete', 'next_cursor': None}})
    agents = module('chat_parts.tui_background_agents').TuiBackgroundAgents(client(transport))
    agents.refresh()
    assert agents.rows
    transport.status = 403
    agents.refresh()
    assert agents.rows == () and agents.count is None


def test_open_business_conversation_refreshes_only_the_visible_thread(monkeypatch):
    nav = TuiAgentNavigationState(TuiRuntime('main-refresh'))
    agents = module('chat_parts.tui_background_agents').TuiBackgroundAgents(client(ProtocolTransport()))
    agents.rows = ({'agent_id': 'ag_A', 'thread_id': 'ag_A'},)
    calls = []
    monkeypatch.setattr(nav, '_load_business_conversation', lambda thread, runtime: calls.append(thread))
    nav.update_business_agents(agents)
    assert calls == []
    nav.move_selection(1)
    nav.enter_selected()
    assert calls == []
    nav.move_selection(1)
    nav.enter_selected()
    assert calls == ['ag_A']
    nav.update_business_agents(agents)
    assert calls == ['ag_A', 'ag_A']


def test_business_ctrl_c_does_not_interrupt_root_work(monkeypatch):
    import threading

    from agent_py_agent.cli.chat_parts import tui_keybindings as keys
    root = TuiRuntime('main-ctrl-c')
    nav = TuiAgentNavigationState(root)
    agents = module('chat_parts.tui_background_agents').TuiBackgroundAgents(client(ProtocolTransport()))
    nav.update_business_agents(agents)
    nav.move_selection(1)
    nav.enter_selected()
    interrupted = []
    monkeypatch.setattr(keys, '_copy_input_selection', lambda event, params: False)
    monkeypatch.setattr(keys, '_copy_transcript_selection', lambda event, params: False)
    monkeypatch.setattr(keys, '_reset_exit_arms', lambda params: None)
    monkeypatch.setattr(keys, '_dispatch_active_interrupt', lambda event, params: interrupted.append(True))
    params = SimpleNamespace(agent_navigation=nav, tui_runtime=root, state_lock=threading.Lock(),
                             is_running_ref=[True], pending_jobs_ref=[0])
    event = SimpleNamespace(app=SimpleNamespace(invalidate=lambda: None))
    keys._handle_ctrl_c_keybinding(event, params)
    assert interrupted == []


def test_web_unknown_mutation_reuses_exact_request_but_not_after_acknowledgement():
    run_web(r'''
const assert=require('node:assert/strict');
(async()=>{
 let fail=true;
 const api=new ui.BusinessClient(async(path,o)=>{if(fail) throw Error('response lost');
   return {ok:true,status:202,json:async()=>({...ACCEPTED,request_id:JSON.parse(o.body).request_id})};});
 const params={space_id:'sp_1',instruction:'做同一件事'};
 const first=api.prepare('tasks.submit',params,{mutating:true});
 await assert.rejects(api.send(first));
 const retry=api.prepare('tasks.submit',params,{mutating:true});
 assert.equal(retry.body,first.body);
 fail=false; await api.send(retry);
 assert.notEqual(api.prepare('tasks.submit',params,{mutating:true}).body,first.body);
})().catch(e=>{console.error(e);process.exitCode=1;});
''', ACCEPTED=produced('tasks.submit', {'space_id': 'sp_1', 'instruction': '做同一件事'}, status=202)[1])


def test_web_receipts_keep_pending_denied_and_unknown_out_of_success():
    params = {'agent_id': 'ag_A', 'text': '同一请求', 'input_kind': 'message'}
    receipts = {name: produced('background_agents.messages.send', params, status=status, receipt=receipt)
                for name, status, receipt in (('completed', 200, None), ('accepted', 202, None),
                                              ('pending', 202, pending_receipt), ('denied', 403, None),
                                              ('conflict', 409, None), ('unknown', 503, None))}
    run_web(r'''
const assert=require('node:assert/strict');
(async()=>{
 let current;
 const api=new ui.BusinessClient(async(path,o)=>{const [status,body]=current;
   return {ok:status>=200&&status<300,status,json:async()=>({...body,request_id:JSON.parse(o.body).request_id})};});
 for (const name of ['completed','accepted']) {
   current=RECEIPTS[name];
   const prepared=api.prepare('background_agents.messages.send',{agent_id:name},{mutating:true});
   const result=await api.send(prepared);
   assert.deepEqual(result,{...current[1],request_id:prepared.request_id});
   assert.equal(api.pending.has(prepared.request_id),false);
 }
 for (const [name,text] of [['pending','未确认'],['denied','拒绝'],['conflict','拒绝'],['unknown','未确认']]) {
   current=RECEIPTS[name];
   const prepared=api.prepare('background_agents.messages.send',{agent_id:name},{mutating:true});
   const error=await api.send(prepared).then(()=>null,e=>e);
   assert.ok(error,name+' 被当成功');
   assert.equal(error.status,current[0]); assert.ok(error.message.includes(text),name);
   assert.deepEqual(error.body,{...current[1],request_id:prepared.request_id});
   assert.equal(api.prepare('background_agents.messages.send',{agent_id:name},{mutating:true}).body,prepared.body);
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
''', RECEIPTS=receipts)


def test_web_catch_up_is_bounded_and_keeps_canonical_continuation():
    pages = [events_page([dto('EventView', stream_item='event', event_id=f'evt-{index}', cursor=f'cursor-{index}',
                              event_type='attempt.progress', payload=dto('TaskView'))],
                         next_cursor=f'cursor-{index}', coverage='complete') for index in range(4)]
    run_web(r'''
const assert=require('node:assert/strict');
(async()=>{
 const served=[]; let queue=[];
 const api=new ui.BusinessClient(async(path,o)=>{const request=JSON.parse(o.body); served.push(request.params);
   const body=queue.shift(); assert.ok(body,'补拉超出预算');
   return {ok:true,status:200,json:async()=>({...body,request_id:request.request_id})};});
 const updates=[]; const obs=new ui.BusinessObservation(api,item=>updates.push(item),{space_id:'sp_1'});
 obs.maxCatchUpPages=3; queue=[...PAGES];
 await obs.recover();
 assert.equal(served.length,3,'持续非空游标只靠页数预算收口');
 assert.deepEqual(served.map(params=>params.cursor),[undefined,'cursor-0','cursor-1']);
 assert.deepEqual(updates.filter(x=>x.stream_item==='event').map(x=>x.event_id),['evt-0','evt-1','evt-2']);
 assert.equal(obs.cursor,'cursor-2'); assert.equal(obs.coverage,'partial');
 // 非空续点的空过滤页不代表结束，替身供满本轮预算，避免把队列耗尽伪装成网络失败。
 queue=Array(obs.maxCatchUpPages).fill(FILTERED); await obs.recover();
 assert.equal(served.length,6); assert.equal(queue.length,0);
 assert.deepEqual(served.slice(3).map(params=>params.cursor),['cursor-2','filtered-scan','filtered-scan']);
 assert.equal(obs.cursor,'filtered-scan','过滤后为空的页也保存扫描续点');
 assert.equal(obs.coverage,'partial','达到页数预算不冒充全部补拉完成');
 queue=[PAGES[1],PAGES[2],PAGES[3],FILTERED]; await obs.recover();
 assert.equal(served.length,9); assert.equal(queue.length,1,'每轮仍受三页预算约束');
 assert.deepEqual(served.slice(6).map(params=>params.cursor),['filtered-scan','cursor-1','cursor-2']);
 assert.deepEqual(updates.filter(x=>x.stream_item==='event').map(x=>x.event_id),['evt-0','evt-1','evt-2','evt-3'],'按 event_id 去重');
 queue=[END]; await obs.recover();
 assert.equal(served.length,10); assert.equal(served[9].cursor,'cursor-3');
 assert.equal(obs.cursor,null); assert.equal(obs.coverage,'complete'); assert.equal(queue.length,0);
 obs.cursor='expired-cursor';
 queue=[RESET]; await obs.recover();
 assert.equal(served.length,11); assert.equal(served[10].cursor,'expired-cursor');
 assert.equal(obs.cursor,null); assert.equal(obs.coverage,'expired'); assert.equal(queue.length,0);
 assert.equal(obs.seen.size,0); assert.equal(api.pending.size,0);
 assert.equal(updates.at(-1).stream_item,'recovered'); assert.equal(updates.at(-1).coverage,'expired');
})().catch(e=>{console.error(e);process.exitCode=1;});
''', PAGES=pages, FILTERED=events_page(next_cursor='filtered-scan', coverage='complete'),
        END=events_page(coverage='complete'),
        RESET=events_page(next_cursor='reset-scan', coverage='expired', reset_required=True))
