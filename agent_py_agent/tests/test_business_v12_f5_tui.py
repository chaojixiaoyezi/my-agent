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

from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import validate_object
from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime


def module(name):
    path = 'agent_py_agent.cli.' + name
    assert importlib.util.find_spec(path) is not None, f'尚未实现 {path}'
    return importlib.import_module(path)


class ProtocolTransport:
    def __init__(self, pages=None, status=200):
        self.calls = []
        self.pages = pages or {}
        self.status = status

    def __call__(self, path, payload, *, timeout):
        assert path == '/client/business'
        assert timeout > 0
        validate_object(payload, business_action_spec(payload['action']).native_request_schema)
        self.calls.append(json.loads(json.dumps(payload)))
        data = self.pages.get(payload['action'], {'items': [], 'coverage': 'complete', 'next_cursor': None})
        if callable(data):
            data = data(payload['params'])
        return self.status, {'ok': self.status in (200, 202), 'data': data,
                             'state': 'accepted' if self.status == 202 else 'completed'}


def client(transport):
    return module('space_commands').BusinessClient(transport)


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
        'items': [{'agent_id': 'ag_A', 'thread_id': 'ag_A', 'space_id': 'sp_1',
                   'name': '设计', 'purpose': '做界面', 'model_profile_id': 'model_1'}],
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
        'items': [{'agent_id': 'ag_A', 'thread_id': 'ag_A', 'space_id': 'sp_1', 'name': '设计'}],
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
        return {'items': [{'message_id': 'm2' if params.get('cursor') else 'm1',
                           'thread_id': 'ag_A', 'role': 'assistant', 'text': '完整正文'}],
                'coverage': 'complete', 'next_cursor': None if params.get('cursor') else 'next'}
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


def test_footer_orders_mode_watch_team_before_existing_controls():
    from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext, _render_footer
    runtime = TuiRuntime('thread-main')
    assert 'background_agent_count' in TuiRenderContext.__dataclass_fields__
    context = TuiRenderContext(width=200, background_process_count=1,
                               background_agent_count=4, background_agent_running_count=None)
    text = ''.join(x[1] for x in _render_footer(runtime.store.snapshot(), context))
    assert text.index('后台驻留') < text.index('后台Agent 4(运行未知)') < text.index('F4')


def run_web(script):
    path = Path(__file__).resolve().parents[2] / 'examples/business_space_ui/app.js'
    assert path.is_file(), '参考 Web 尚未实现'
    node = shutil.which('node')
    assert node, '此验收需要 Node 来运行前端协议测试'
    result = subprocess.run([node, '-e', f'const ui = require({json.dumps(str(path))});\n' + script],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_web_ndjson_split_unicode_gap_and_authenticated_range():
    run_web(r'''
const assert = require('node:assert/strict');
(async () => {
 const calls = [];
 const fake = async (path, options) => {
   calls.push({path, options});
   return {ok: true, status: 200, json: async () => ({ok:true, data:{items:[], coverage:'partial', next_cursor:null}}),
           blob: async () => new Blob(['video'])};
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
''')


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
        'items': [{'agent_id': 'ag_A', 'thread_id': 'ag_A', 'space_id': 'sp_1'}],
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
   return {ok:true,status:200,json:async()=>({ok:true,data:{items:[],next_cursor:null,coverage:'partial'}})};});
 const obs=new ui.BusinessObservation(api,()=>{}, {space_id:'sp_1'}); obs.cursor='expired-old';
 await obs.accept({stream_item:'cursor_expired'});
 assert.deepEqual(calls[0].params, {filter:{space_id:'sp_1'}});
 assert.equal(obs.coverage, 'partial');
 await assert.rejects(obs.accept({stream_item:'完成'}));
 const decoder=new ui.NDJSONDecoder(()=>{}); decoder.feed(new TextEncoder().encode('{"half":'));
 assert.throws(()=>decoder.finish());
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


def test_denial_clears_old_roster_instead_of_showing_revoked_space():
    transport = ProtocolTransport({'background_agents.list': {'items': [{'agent_id': 'ag_private'}],
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
 const api=new ui.BusinessClient(async()=>{if(fail) throw Error('response lost');
   return {ok:true,status:202,json:async()=>({ok:true,state:'accepted'})};});
 const params={space_id:'sp_1',instruction:'做同一件事'};
 const first=api.prepare('tasks.submit',params,{mutating:true});
 await assert.rejects(api.send(first));
 const retry=api.prepare('tasks.submit',params,{mutating:true});
 assert.equal(retry.body,first.body);
 fail=false; await api.send(retry);
 assert.notEqual(api.prepare('tasks.submit',params,{mutating:true}).body,first.body);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')
