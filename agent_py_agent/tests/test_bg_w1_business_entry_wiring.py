# LLM: BG-1/BG-2/BG-4 入口接线回归只用隔离 home 与 echo 后端；不调用真实模型，不改原业务服务或授权账。
# 模块用途: 验证 HTTP 会话作用域、派活工具自然语言发现、后台派活回合业务身份三处真实接通。
from __future__ import annotations

import http.client
import json
from dataclasses import replace
from threading import Event, Thread
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.conversation.session_task_report import report_task_result
from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.chat_client_context import (
    _gateway_request_identity,
    owner_identity_from_config,
)
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call


@pytest.fixture
def agent(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    host = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(home), memory_path='memory.jsonl',
        orphan_supervision_interval_seconds=0), tmp_path / 'workspace')
    monkeypatch.setattr(host, 'run', lambda *a, **k: pytest.fail('业务接线不能调用模型'))
    return host


def _chat_thread(agent, conversation_id, *, channel='tui', user_id='local-agent'):
    return agent.conversation_store.threads.get_or_create({
        'canonical_user_id': user_id, 'channel': channel, 'channel_conversation_id': conversation_id,
        'channel_user_id': user_id, 'owner_id': agent.home_paths.owner_id,
        'owner_home': str(agent.home_paths.owner_home_dir)})


def _call(agent, action, params=None, **options):
    if 'idempotency_key' in options:
        options['idempotency_key'] = 'bg-w1-entry-' + options['idempotency_key']
    request = {'action': action, 'request_id': 'bg-w1', 'params': params or {}, **options}
    current = getattr(agent, '_current_run_params', None)
    run_id = getattr(current, 'run_id', '') or 'test-run'
    attempt_id = getattr(current, 'attempt_id', '') or 'test-attempt'
    outcome = execute_registry_test_call(agent.tools, 'business_' + action.replace('.', '_'), request,
        run_id=run_id, attempt_id=attempt_id, register_with=agent, call_id='bg-w1-' + uuid4().hex)
    return outcome, (json.loads(outcome.output) if outcome.output.startswith('{') else {})


def _http(server, identity, action, params, conversation_id=''):
    conn = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=10)
    headers = {'X-Gateway-Token': server._local_client_credential, 'X-User-Id': identity['user_id'],
               'X-Channel': identity['channel'], 'Content-Type': 'application/json'}
    if conversation_id:
        headers['X-Conversation-Id'] = conversation_id
    body = json.dumps({'action': action, 'request_id': 'bg-w1-' + uuid4().hex[:8], 'params': params})
    conn.request('POST', '/client/business', body.encode('utf-8'), headers)
    response = conn.getresponse()
    payload = json.loads(response.read())
    conn.close()
    return response.status, payload


# LLM: TUI 会话里模型创建的后台代理，必须能被同一会话的 /team 与团队面板读到；缺省作用域保持原 default 行为。
# 函数用途: 覆盖 X-Conversation-Id 选择本人会话、缺省/未知会话仍 403 且不建线程。
def test_http_business_read_uses_selected_conversation_scope(agent):
    server = GatewayHTTPServer(0, gateway_paths_from_root(agent.home_paths.root / 'gateway'),
        params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        identity = _gateway_request_identity(owner_identity_from_config(agent.config))
        chat = _chat_thread(agent, 'sess-bg-w1', channel=identity['channel'], user_id=identity['user_id'])
        agent._current_run_params = RunParams(request_id='turn-1', task_attributes={'conversation_thread_id': chat.thread_id})
        outcome, created = _call(agent, 'background_agents.create', {'name': '监控助手', 'space_id': None},
            idempotency_key='create-agent')
        assert outcome.ok and created['error'] is None, outcome.output
        threads_dir = agent.conversation_store.storage.threads_dir
        threads_before = sorted(path.name for path in threads_dir.glob('*.json'))
        status, scoped = _http(server, identity, 'background_agents.list', {'limit': 50}, 'sess-bg-w1')
        assert status == 200 and scoped['error'] is None, scoped
        assert [row['name'] for row in scoped['data']['items']] == ['监控助手']
        status, default = _http(server, identity, 'background_agents.list', {'limit': 50})
        assert status == 403 and default['error']['code'] == 'ACTOR_REQUIRED'
        status, unknown = _http(server, identity, 'background_agents.list', {'limit': 50}, 'sess-never-seen')
        assert status == 403 and unknown['error']['code'] == 'ACTOR_REQUIRED'
        assert sorted(path.name for path in threads_dir.glob('*.json')) == threads_before
    finally:
        server.stop()


# LLM: 用户自然语言“派活/分配任务/交给总控”必须直接命中 tasks.* 原工具，不被同义的会话派活或后台代理创建抢走。
# 函数用途: 覆盖 tool_search 首位命中、tasks 分类、执行类用途说明与 self_map 业务分块。
@pytest.mark.parametrize('query,expected_action', [
    ('派活', 'tasks.assign'), ('给后台代理派任务', 'tasks.assign'),
    ('分配任务', 'tasks.assign'), ('总控 提交任务', 'tasks.submit'),
    ('追问后台任务进度', 'tasks.follow_up'),
])
def test_task_dispatch_words_find_business_task_tools(agent, query, expected_action):
    found = execute_registry_test_call(agent.tools, 'tool_search', {'query': query, 'limit': 5}, register_with=agent)
    assert found.ok, found.output
    assert 'business_execution' in found.metadata['handler_details']['tool_search']['loaded_tool_names'], found.output
    spec = agent.tools.tools['business_execution'].model_spec
    assert spec.hints.category == 'background_agents' and spec.hints.default_deferred
    assert 0 < len(spec.hints.deferred_summary) <= 60
    assert expected_action in spec.input_schema['properties']['action']['enum']


def test_self_map_business_section_lists_task_entry(agent):
    from agent_py_agent.agent.tooling.models import ToolInvocationContext

    tool = agent.tools.tools['self_map']
    context = ToolInvocationContext(agent.tools.runtime_snapshot())
    overview = tool.execute_scoped({'section': 'business'}, context)
    assert overview.ok and '{"item":"background_agents"}' in overview.output, overview.output
    entry = json.loads(tool.execute_scoped({'section': 'business', 'item': 'background_agents'}, context).output)['item']
    assert entry['state'] == 'available'
    assert 'business_execution' in set(entry['tools'])


def _background_turn(agent, thread_id, context, request_id='bg-turn-1'):
    run_id, attempt_id = '', ''
    if context is not None:
        run_id = 'bg-run-' + uuid4().hex
        proof = agent.subagents.runtime_db.record_run_creation(
            owner_id=agent.home_paths.owner_id,
            run_id=run_id,
            thread_id=thread_id,
            task_run_metadata={'business_context': context.to_dict(), 'session_task_ref': context.task_id},
        )
        attempt_id = proof['attempt_id']
    agent._current_run_params = RunParams(run_id=run_id, attempt_id=attempt_id,
        request_id=request_id, task_attributes={'conversation_thread_id': thread_id},
        business_context=context)


# LLM: 后台代理/总控执行业务 SessionTask 时，业务身份沿原派活发送方恢复；任务、线程、Owner 任一不一致仍拒绝。
# 函数用途: 覆盖后台派活回合读团队、无业务上下文仍拒绝、错线程上下文拒绝三种真实状态。
def test_background_business_turn_recovers_sender_identity(agent):
    chat = _chat_thread(agent, 'sess-bg-w1-actor')
    agent._current_run_params = RunParams(request_id='turn-1', task_attributes={'conversation_thread_id': chat.thread_id})
    _, space = _call(agent, 'spaces.create', {'name': '派活空间'}, idempotency_key='space')
    sid = space['data']['space_id']
    _, created = _call(agent, 'background_agents.create', {'name': '工人', 'space_id': sid}, idempotency_key='worker')
    worker = created['data']['agent']['agent_id']
    _, assigned = _call(agent, 'tasks.assign', {'agent_id': worker, 'space_id': sid, 'instruction': '整理日报'},
        idempotency_key='assign')
    task = agent.conversation_store.session_tasks.require(assigned['data']['task_id'])
    context = BusinessExecutionContext.from_dict(task.metadata['business_context'])

    _background_turn(agent, worker, context)
    outcome, listed = _call(agent, 'background_agents.list', {'limit': 20})
    assert outcome.ok and listed['error'] is None, outcome.output
    assert worker in [row['agent_id'] for row in listed['data']['items']]

    _background_turn(agent, worker, None, request_id='bg-turn-2')
    outcome, denied = _call(agent, 'background_agents.list', {'limit': 20})
    assert not outcome.ok and denied['error']['code'] == 'CAPABILITY_DENIED', outcome.output

    agent._current_run_params = RunParams(request_id='turn-3', task_attributes={'conversation_thread_id': chat.thread_id})
    _, created = _call(agent, 'background_agents.create', {'name': '旁观者', 'space_id': sid}, idempotency_key='bystander')
    bystander = created['data']['agent']['agent_id']
    _background_turn(agent, bystander, replace(context), request_id='bg-turn-3')
    outcome, mismatched = _call(agent, 'background_agents.list', {'limit': 20})
    assert not outcome.ok and mismatched['error']['code'] == 'CAPABILITY_DENIED', outcome.output


@pytest.fixture
def delegation(agent):
    chat = _chat_thread(agent, 'delegation-entry')
    _background_turn(agent, chat.thread_id, None)
    _, space = _call(agent, 'spaces.create', {'name': '委派空间'}, idempotency_key='lineage-space')
    sid = space['data']['space_id']
    workers = []
    for key in ('first', 'second'):
        outcome, created = _call(agent, 'background_agents.create', {'name': key, 'space_id': sid},
            idempotency_key='lineage-' + key)
        assert outcome.ok, outcome.output
        workers.append(created['data']['agent']['agent_id'])
    outcome, assigned = _call(agent, 'tasks.assign', {'agent_id': workers[0], 'space_id': sid,
        'instruction': '上级任务'}, idempotency_key='lineage-root')
    assert outcome.ok, outcome.output
    root = agent.conversation_store.session_tasks.require(assigned['data']['task_id'])
    assert root.origin_task_id == ''
    context = BusinessExecutionContext.from_dict(root.metadata['business_context'])
    _background_turn(agent, workers[0], context)
    return SimpleNamespace(chat=chat, sid=sid, first=workers[0], second=workers[1], root=root,
        params={'agent_id': workers[1], 'space_id': sid, 'instruction': '下级任务'})


def _later_parent(agent, case):
    _background_turn(agent, case.chat.thread_id, None, request_id='later-chat')
    outcome, result = _call(agent, 'tasks.assign', {'agent_id': case.first, 'space_id': case.sid,
        'instruction': '后来的上级任务'}, idempotency_key='lineage-later-root')
    assert outcome.ok, outcome.output
    parent = agent.conversation_store.session_tasks.require(result['data']['task_id'])
    _background_turn(agent, case.first, BusinessExecutionContext.from_dict(parent.metadata['business_context']),
        request_id='later-parent-turn')
    return parent


@pytest.mark.parametrize('status', ['queued', 'done', 'failed', 'cancelled'])
@pytest.mark.parametrize('legacy', [False, True], ids=['linked', 'legacy-empty-origin'])
def test_omitted_origin_replay_preserves_canonical_identity_and_terminal_state(
    agent, delegation, monkeypatch, status, legacy,
):
    case, store = delegation, agent.conversation_store
    outcome, assigned = _call(agent, 'tasks.assign', case.params, idempotency_key='lineage-child')
    assert outcome.ok, outcome.output
    task = store.session_tasks.require(assigned['data']['task_id'])
    if legacy:
        # 模拟旧版本已受理的空来源记录；重放不能借后来上下文补写历史身份。
        update_json_file_atomic(store.session_tasks._path(task.task_id),
            lambda data: {**data, 'origin_task_id': ''}, require_existing=True)
    else:
        assert task.origin_task_id == case.root.task_id
    if status != 'queued':
        store.session_tasks.bind_turn(task.task_id, turn_id='executing-child')
        store.session_tasks.advance(task.task_id, SessionTaskUpdate(status, summary='原任务结果', result_refs=('result:1',)))
        if status == 'cancelled':
            store.session_tasks.record_cancellation_control(task.task_id, outstanding_refs=['cleanup:original'])
    original = store.session_tasks.require(task.task_id)
    later = _later_parent(agent, case)
    wakes_before = [row.wake_signal_id for row in store.wakes.pending(limit=0)]
    outcome, replay = _call(agent, 'tasks.assign', case.params, idempotency_key='lineage-child')
    assert outcome.ok and replay['data']['task_id'] == original.task_id, outcome.output
    assert store.session_tasks.require(task.task_id).to_dict() == original.to_dict()
    assert [row.wake_signal_id for row in store.wakes.pending(limit=0)] == wakes_before
    create, errors = store.session_tasks.create, []

    def observe_create(*args, **kwargs):
        try:
            return create(*args, **kwargs)
        except ValueError as error:
            errors.append(error)
            raise

    monkeypatch.setattr(store.session_tasks, 'create', observe_create)
    outcome, conflict = _call(agent, 'tasks.assign', {**case.params, 'origin_task_id': later.task_id},
        idempotency_key='lineage-child')
    assert not outcome.ok and outcome.error_code == 'TOOL_OPERATION_OUTCOME_UNKNOWN', outcome.output
    assert conflict['reported_error_code'] == 'TOOL_EXECUTION_FAILED'
    assert len(errors) == 1 and str(errors[0]).startswith('IDEMPOTENCY_CONFLICT:')
    assert errors[0].__cause__ is None
    assert store.session_tasks.require(task.task_id).to_dict() == original.to_dict()
    assert [row.wake_signal_id for row in store.wakes.pending(limit=0)] == wakes_before
    if status in {'done', 'failed'}:
        assert report_task_result(agent, original)
        entries = store.guidance.pending('thread', case.first, limit=0)
        reports = [entry for entry in entries if entry.metadata.get('session_task_id') == task.task_id]
        assert len(reports) == 1 and reports[0].metadata['origin_thread_id'] == case.second
    if status == 'cancelled':
        assert store.session_tasks.require(task.task_id).metadata['cancellation_control']['outstanding_refs'] == [
            'cleanup:original']


def test_inherited_origin_uses_configured_dispatch_depth_limit(agent, delegation, tmp_path):
    config = tmp_path / 'capability_config.yaml'
    config.write_text('session_task_max_chain_depth: 1\n', encoding='utf-8')
    agent.capability_config_path = config
    agent._capability_config_runtime_snapshot = None
    before = [row.wake_signal_id for row in agent.conversation_store.wakes.pending(limit=0)]
    outcome, result = _call(agent, 'tasks.assign', delegation.params, idempotency_key='depth-rejected')
    assert not outcome.ok and result['error']['code'] == 'SESSION_TASK_CHAIN_LIMIT', outcome.output
    assert [row.wake_signal_id for row in agent.conversation_store.wakes.pending(limit=0)] == before


def test_replay_racing_cancellation_keeps_cleanup_and_single_scope_claim(agent, delegation, monkeypatch):
    case, store = delegation, agent.conversation_store
    outcome, assigned = _call(agent, 'tasks.assign', case.params, idempotency_key='racing-child')
    assert outcome.ok, outcome.output
    task = store.session_tasks.require(assigned['data']['task_id'])
    entered, proceed = Event(), Event()
    find = store.session_tasks.find_by_dedupe_key
    results, errors = [], []
    claim = store.claims.acquire({'thread_id': case.second, 'reason': 'lineage-test', 'task_id': task.task_id})
    assert claim is not None

    def pause_after_lookup(key):
        previous = find(key)
        if previous is not None and previous.task_id == task.task_id:
            entered.set()
            assert proceed.wait(10)
        return previous

    def replay():
        try:
            _background_turn(agent, case.first, BusinessExecutionContext.from_dict(case.root.metadata['business_context']))
            results.append(_call(agent, 'tasks.assign', case.params, idempotency_key='racing-child'))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(store.session_tasks, 'find_by_dedupe_key', pause_after_lookup)
    thread = Thread(target=replay)
    thread.start()
    try:
        assert entered.wait(10)
        _background_turn(agent, case.chat.thread_id, None, request_id='concurrent-chat-turn')
        assert store.claims.acquire({'thread_id': case.second, 'reason': 'competing-task'}) is None
        store.session_tasks.advance(task.task_id, SessionTaskUpdate('cancelled'))
        cancelled = store.session_tasks.record_cancellation_control(task.task_id, outstanding_refs=['cleanup:original'])
        wakes_before = [row.wake_signal_id for row in store.wakes.pending(limit=0)]
    finally:
        proceed.set()
        thread.join(10)
        store.claims.finish({'thread_id': case.second, 'claim_id': claim['claim_id'], 'status': 'cancelled'})
    assert not thread.is_alive() and not errors
    assert results[0][0].ok, results[0][0].output
    assert store.session_tasks.require(task.task_id).to_dict() == cancelled.to_dict()
    assert [row.wake_signal_id for row in store.wakes.pending(limit=0)] == wakes_before
    assert store.claims.load(case.second)['status'] == 'cancelled'
    assert agent._current_run_params.request_id == 'concurrent-chat-turn'


def test_steer_cancel_and_next_task_keep_lineage_and_context_scope(agent, delegation):
    case, store = delegation, agent.conversation_store
    outcome, assigned = _call(agent, 'tasks.assign', case.params, idempotency_key='sequence-child')
    assert outcome.ok, outcome.output
    task = store.session_tasks.require(assigned['data']['task_id'])
    outcome, sent = _call(agent, 'background_agents.messages.send', {'agent_id': case.second,
        'text': '追加结构化资料', 'input_kind': 'guidance'}, idempotency_key='sequence-steer')
    assert outcome.ok and sent['data']['receipt_state'] == 'accepted', outcome.output
    outcome, stopped = _call(agent, 'tasks.cancel', {'task_id': task.task_id, 'include_descendants': False},
        idempotency_key='sequence-stop', expected_revision=1)
    assert outcome.ok and stopped['error'] is None, outcome.output
    assert store.session_tasks.require(task.task_id).status == 'cancelled'
    assert store.guidance.receipt(task.body_dedupe_key).status == 'rejected'
    outcome, assigned = _call(agent, 'tasks.assign', {**case.params, 'instruction': '下一轮任务'},
        idempotency_key='sequence-next')
    assert outcome.ok, outcome.output
    next_task = store.session_tasks.require(assigned['data']['task_id'])
    assert next_task.task_id != task.task_id and next_task.origin_task_id == case.root.task_id
    _background_turn(agent, case.second, BusinessExecutionContext.from_dict(next_task.metadata['business_context']),
        request_id='sequence-worker-next')
    outcome, space = _call(agent, 'spaces.get', {'space_id': case.sid})
    assert outcome.ok and space['data']['space']['space_id'] == case.sid, outcome.output
    _background_turn(agent, case.second, None, request_id='sequence-new-empty-turn')
    outcome, denied = _call(agent, 'spaces.get', {'space_id': case.sid})
    assert not outcome.ok and denied['error']['code'] == 'CAPABILITY_DENIED'


def test_message_and_task_model_help_distinguish_receipt_and_execution(agent):
    from agent_py_agent.agent.tooling.input_schema import validate_tool_input

    spec = agent.tools.tools['business_execution'].model_spec
    assert 'tasks.assign' in spec.description and 'SessionTask' in spec.description
    assert 'background_agents.messages.send' in spec.description and '不新建SessionTask' in spec.description
    assert '冻结' in spec.description and '只证明已受理' in spec.description
    assert len(spec.hints.examples) >= 2
    seen_actions = set()
    for raw in spec.hints.examples:
        example = json.loads(raw)
        assert validate_tool_input(example, spec.input_schema).ok
        seen_actions.add(example['action'])
        assert 'request_id' in example
    assert {'tasks.assign', 'background_agents.messages.send'} <= seen_actions
