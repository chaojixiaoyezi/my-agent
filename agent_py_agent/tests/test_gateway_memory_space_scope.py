"""前台空间仅缩小记忆召回，不创建业务任务或改变工具执行身份。"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.capability.memory_search_tool import _scope_predicate
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.business_api import local_business_context
from agent_py_agent.agent.gateway_parts.request_space import (
    GatewayMemoryScopeError,
    gateway_memory_recall_scope,
)
from agent_py_agent.agent.memory_store.recall import MemoryRecallScope, runtime_long_term_scope
from agent_py_agent.agent.settings.config import AgentConfig


@pytest.fixture
def spaces(tmp_path, monkeypatch):
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home'),
        business_spaces_enabled=True, enable_memory_search_tool=True), tmp_path / 'workspace')
    from agent_py_agent.agent.gateway_parts.business_host_ports import wire_business_host_ports

    wire_business_host_ports(agent)
    authority, services = local_business_context(agent)
    actor = services.actor(authority)
    rows = []
    for project in ('alpha', 'beta', None):
        if project:
            (agent.home_paths.owner_home_dir / 'projects' / project).mkdir(parents=True)
        params = {'name': project or 'empty'}
        if project:
            params['default_project_ref'] = project
        row = services.spaces.create(params, actor=actor, idempotency_key='create-' + (project or 'empty'))
        rows.append({'owner_id': agent.home_paths.owner_id, 'space_id': row['space_id']})
    return agent, services, rows


def record(scope, key):
    return SimpleNamespace(attributes={'scope_type': scope, 'scope_key': key}, kind='fact')


@pytest.mark.parametrize('index', [0, 1, 2])
def test_space_resolves_only_own_project_and_keeps_personal(spaces, index):
    agent, _, rows = spaces
    scope = gateway_memory_recall_scope(agent, rows[index], channel_actor=None, request_id='local')
    params = RunParams(memory_recall_scope=scope)
    assert params.business_context is None  # 不能把普通请求变成SessionTask/工作流来源。
    agent._current_run_params = params
    predicate = _scope_predicate(agent, agent.memory, '')
    for project, position in [('alpha', 0), ('beta', 1)]:
        assert predicate(record('project', 'project:' + project)) is (index == position)
    assert predicate(record('personal', 'personal'))
    assert predicate(record('global', 'global'))
    # 模型可写attrs不扩大宿主只读范围；无需访问全项目索引。
    memory = SimpleNamespace(all=lambda: pytest.fail('selected space must not enumerate all project scopes'))
    assert runtime_long_term_scope(memory, task_attributes={'project_id': 'injected'},
        memory_recall_scope=scope) is scope


def test_unbound_and_history_keep_existing_scope(spaces):
    agent, _, _ = spaces
    assert gateway_memory_recall_scope(agent, None, channel_actor=None, request_id='local') is None
    memory = SimpleNamespace(
        all=lambda: [record('project', 'project:alpha'), record('project', 'project:beta')],
        scope_pairs=lambda _kind: [('project', 'project:alpha'), ('project', 'project:beta')],
    )
    scope = runtime_long_term_scope(memory)
    assert scope.allows('project', 'project:alpha') and scope.allows('project', 'project:beta')


def test_remember_management_reads_only_visible_project_through_list_and_changes(spaces):
    from agent_py_agent.agent.capability.memory_tool import RememberTool

    agent, _, rows = spaces
    records = {key: agent.memory.add('user', value, kind='fact',
        attributes={'scope_type': scope, 'scope_key': key}) for scope, key, value in (
            ('project', 'project:alpha', 'alpha 731'), ('project', 'project:beta', 'beta 842'),
            ('personal', 'personal', 'personal 316'), ('global', 'global', 'global 405'))}
    tool = RememberTool(agent)
    for index, project in ((0, 'alpha'), (1, 'beta'), (2, None)):
        scope = gateway_memory_recall_scope(agent, rows[index], channel_actor=None, request_id='local')
        agent._current_run_params = RunParams(memory_recall_scope=scope)
        visible = json.loads(tool.execute({'action': 'list'}).output)['entries']
        expected = {'personal', 'global'} | ({'project:' + project} if project else set())
        assert {item['scope']['scope_key'] for item in visible} == expected

    other_id = records['project:beta'].entry_id
    agent._current_run_params = RunParams(memory_recall_scope=gateway_memory_recall_scope(
        agent, rows[0], channel_actor=None, request_id='local'))
    for action in ('replace', 'remove'):
        operation = {'action': action, 'origin': 'user_explicit', 'content': 'change'}
        hidden = tool.execute({**operation, 'entry_id': other_id})
        missing = tool.execute({**operation, 'entry_id': 'missing-id'})
        assert hidden.output == missing.output
        assert hidden.error_code == missing.error_code == 'MEMORY_ENTRY_NOT_FOUND'
        assert hidden.effect_outcome == missing.effect_outcome == 'not_started'
        batch = tool.execute({'action': 'batch', 'operations': [{**operation, 'entry_id': other_id}]})
        assert (batch.error_code, batch.effect_outcome) == ('MEMORY_ENTRY_NOT_FOUND', 'not_started')
    agent._current_run_params = RunParams()
    entries = json.loads(tool.execute({'action': 'list'}).output)['entries']
    assert {item['content'] for item in entries} == {row.content for row in records.values()}


@pytest.mark.parametrize('context_scope', ['task_local', 'control_plane'])
def test_isolated_turn_cannot_list_formal_memory_or_probe_target(spaces, context_scope):
    from agent_py_agent.agent.capability.memory_tool import RememberTool

    agent, _, _ = spaces
    agent._current_run_params = RunParams(context_scope=context_scope)
    agent.memory.all = lambda: pytest.fail('an isolated turn cannot read the formal store')
    tool = RememberTool(agent)
    for call in ({'action': 'list'}, {'action': 'remove', 'entry_id': 'known'},
                 {'action': 'batch', 'operations': [{'action': 'RePlAcE', 'entry_id': 'known'}]}):
        response = tool.execute(call)
        assert (response.error_code, response.effect_outcome) == ('TOOL_UNAVAILABLE', 'not_started')


def test_unreadable_formal_store_cannot_trigger_tombstone_recovery(spaces, monkeypatch):
    from agent_py_agent.agent.capability import memory_tool, memory_tool_recovery

    agent, _, _ = spaces
    def denied_read():
        raise PermissionError('private-store-canary')
    monkeypatch.setattr(agent.memory, 'all', denied_read)
    monkeypatch.setattr(memory_tool_recovery, 'prepare_committed_remove',
        lambda *_: pytest.fail('unreadable authority is not a proven deletion'))
    for action in ('list', 'remove', 'replace'):
        result = memory_tool.RememberTool(agent).execute({'action': action, 'entry_id': 'id', 'origin': 'user_explicit'})
        assert (result.error_code, result.effect_outcome) == ('MEMORY_AUTHORITY_UNREADABLE', 'not_started')
        assert 'PermissionError' in result.output and 'private-store-canary' not in result.output


def test_original_job_context_remains_authoritative(spaces):
    agent, _, rows = spaces
    foreground = gateway_memory_recall_scope(agent, rows[0], channel_actor=None, request_id='local')
    job = BusinessExecutionContext(agent.home_paths.owner_id, rows[1]['space_id'], 'beta',
        'original-job', 0, None, (), (), 'business-context.v1')
    scope = runtime_long_term_scope(agent.memory, business_context=job, memory_recall_scope=foreground)
    assert scope.allows('project', 'project:beta')
    assert scope.allows('project', 'project:original-job')
    assert not scope.allows('project', 'project:alpha')


@pytest.mark.parametrize('damage,code', [('missing', 'SPACE_NOT_FOUND'), ('archived', 'SPACE_ARCHIVED'),
    ('project_deleted', 'REFERENCE_NOT_FOUND'), ('owner', 'SPACE_SCOPE_MISMATCH')])
def test_unavailable_selection_never_falls_back_to_all_projects(spaces, damage, code):
    agent, services, rows = spaces
    snapshot = dict(rows[0])
    if damage == 'missing':
        snapshot['space_id'] = 'missing'
    elif damage == 'owner':
        snapshot['owner_id'] = 'other-owner'
    elif damage == 'project_deleted':
        (agent.home_paths.owner_home_dir / 'projects' / 'alpha').rmdir()
    else:
        # 合成状态损坏只用于纯行为单测，不计入真实晋升/验收。
        original = services.spaces._read
        def archived():
            state = original()
            state['spaces'][snapshot['space_id']]['state'] = 'archived'
            return state
        services.spaces._read = archived
    with pytest.raises(GatewayMemoryScopeError) as caught:
        gateway_memory_recall_scope(agent, snapshot, channel_actor=None, request_id='local')
    assert caught.value.error_code == code
    assert caught.value.side_effect_state == 'not_started'
    assert '模型和工具尚未执行' in str(caught.value)
    assert '重新发送' in str(caught.value)


def test_only_typed_host_scope_is_accepted():
    with pytest.raises(TypeError, match='MemoryRecallScope'):
        RunParams(memory_recall_scope={'project': 'forged'})


def test_private_sender_rebound_after_acceptance_cannot_recall_original_project(spaces, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime import loop_support
    from agent_py_agent.agent.capability.memory_search_tool import MemorySearchTool
    from agent_py_agent.agent.conversation.host_actor import (
        HostActorError,
        actor_from_scope,
        checked_foreground_memory_scope,
    )

    agent, _, rows = spaces
    threads = agent.conversation_store.threads
    thread = threads.get_or_create({'channel': 'feishu', 'channel_conversation_id': 'private-chat',
        'channel_user_id': 'sender-a', 'canonical_user_id': 'sender-a', 'owner_id': agent.home_paths.owner_id,
        'owner_home': str(agent.home_paths.owner_home_dir)})
    inbound = SimpleNamespace(channel='feishu', channel_conversation_id='private-chat', channel_user_id='sender-a')
    sender = actor_from_scope(agent, inbound, thread.thread_id, 'original-request')
    snapshot = {**rows[0], 'actor_id': 'sender-a', 'thread_id': thread.thread_id, 'revision': 1, 'source': 'thread'}
    original = gateway_memory_recall_scope(agent, snapshot, channel_actor=sender, request_id='original-request')
    params = RunParams(request_id='original-request', host_session_space=snapshot,
        host_channel_actor=sender, memory_recall_scope=original)
    agent._current_run_params = params
    assert checked_foreground_memory_scope(agent, params).allows('project', 'project:alpha')
    with pytest.raises(GatewayMemoryScopeError, match='CHANNEL_ACTOR_SCOPE_MISMATCH'):
        gateway_memory_recall_scope(agent, snapshot, channel_actor=sender, request_id='another-request')
    threads.update_atomic(thread.thread_id, lambda row: replace(row, channel_bindings=tuple(
        replace(binding, channel_user_id='sender-b') for binding in row.channel_bindings)))

    with pytest.raises(GatewayMemoryScopeError, match='CHANNEL_ACTOR_SCOPE_MISMATCH'):
        gateway_memory_recall_scope(agent, snapshot, channel_actor=sender, request_id='original-request')
    with pytest.raises(HostActorError, match='归属不一致'):
        checked_foreground_memory_scope(agent, params)
    monkeypatch.setattr(loop_support, '_tool_snapshots_for_run', lambda *_: (None, None))
    monkeypatch.setattr(loop_support, '_routed_memory_context_for_request',
        lambda *_a, **_kw: pytest.fail('binding denied before reading routed rules'))
    with pytest.raises(HostActorError, match='归属不一致'):
        loop_support._prepare_runtime_context(agent,
            loop_support.RuntimeContextRequest('检索', [], False, task_id='original-request'))
    agent.memory.search_scoped_candidates_report = lambda *_a, **_kw: pytest.fail('binding denied before search')
    rejected = MemorySearchTool(agent).execute({'query': '合成测试记忆'})
    assert rejected.error_code == 'CHANNEL_ACTOR_SCOPE_MISMATCH'
    assert rejected.effect_outcome == 'not_started'
    from agent_py_agent.agent.capability.memory_tool import RememberTool
    agent.memory.all = lambda: pytest.fail('binding denied before listing or resolving target')
    for call in ({'action': 'list'}, {'action': 'remove', 'entry_id': 'foreign'}):
        rejected = RememberTool(agent).execute(call)
        assert (rejected.error_code, rejected.effect_outcome) == ('CHANNEL_ACTOR_SCOPE_MISMATCH', 'not_started')


def test_scope_failure_keeps_exact_code_effect_and_next_action():
    from agent_py_agent.agent.gateway_parts.request_execution import _apply_gateway_request_failure

    error = GatewayMemoryScopeError('SPACE_NOT_FOUND')
    response = {}
    _apply_gateway_request_failure(SimpleNamespace(), {'request': {}}, response, error)
    assert response['error_code'] == 'SPACE_NOT_FOUND'
    assert response['user_error'] == str(error)
    assert response['side_effect_state'] == 'not_started'


def test_automatic_recall_consumes_same_frozen_scope():
    from agent_py_agent.agent.agent_core.runtime import loop_support
    from agent_py_agent.tests.test_memory_first_loop import _runtime_context_capture_agent
    agent = _runtime_context_capture_agent({})
    scope = MemoryRecallScope.from_runtime(task_attributes={'project_id': 'alpha'})
    agent._current_run_params = RunParams(memory_recall_scope=scope)
    checked = []
    def search(_query, _limit, predicate):
        checked.append(True)
        assert predicate(record('project', 'project:alpha'))
        assert not predicate(record('project', 'project:beta'))
        assert predicate(record('personal', 'personal'))
        return []
    agent.memory.search_scoped = search
    loop_support._prepare_runtime_context(agent,
        loop_support.RuntimeContextRequest('普通问题', [], False, task_id='request'))
    assert checked == [True]
