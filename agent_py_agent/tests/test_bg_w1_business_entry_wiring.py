# LLM: BG-1/BG-2/BG-4 入口接线回归只用隔离 home 与 echo 后端；不调用真实模型，不改原业务服务或授权账。
# 模块用途: 验证 HTTP 会话作用域、派活工具自然语言发现、后台派活回合业务身份三处真实接通。
from __future__ import annotations

import http.client
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
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
        execution_mode='local_unmanaged'), tmp_path / 'workspace')
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
    outcome = execute_registry_test_call(agent.tools, 'business_' + action.replace('.', '_'), request,
        call_id='bg-w1-' + uuid4().hex)
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
@pytest.mark.parametrize('query,expected', [
    ('派活', 'business_tasks_assign'), ('给后台代理派任务', 'business_tasks_assign'),
    ('分配任务', 'business_tasks_assign'), ('总控 提交任务', 'business_tasks_submit'),
    ('追问后台任务进度', 'business_tasks_follow_up'),
])
def test_task_dispatch_words_find_business_task_tools(agent, query, expected):
    found = execute_registry_test_call(agent.tools, 'tool_search', {'query': query, 'limit': 5})
    assert found.ok, found.output
    assert found.metadata['handler_details']['tool_search']['loaded_tool_names'][0] == expected, found.output
    spec = agent.tools.tools[expected].model_spec
    assert spec.hints.category == 'tasks' and spec.hints.default_deferred
    assert 0 < len(spec.hints.deferred_summary) <= 60


def test_self_map_business_section_lists_task_entry(agent):
    from agent_py_agent.agent.tooling.models import ToolInvocationContext

    tool = agent.tools.tools['self_map']
    context = ToolInvocationContext(agent.tools.runtime_snapshot())
    overview = tool.execute_scoped({'section': 'business'}, context)
    assert overview.ok and '{"item":"tasks"}' in overview.output, overview.output
    entry = json.loads(tool.execute_scoped({'section': 'business', 'item': 'tasks'}, context).output)['item']
    assert entry['state'] == 'available'
    assert {'business_tasks_assign', 'business_tasks_submit', 'business_tasks_follow_up'} <= set(entry['tools'])


def _background_turn(agent, thread_id, context, request_id='bg-turn-1'):
    agent._current_run_params = RunParams(request_id=request_id, task_attributes={'conversation_thread_id': thread_id},
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
