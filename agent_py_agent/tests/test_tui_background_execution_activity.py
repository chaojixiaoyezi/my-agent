"""后台页面仅消费已证实的执行身份，沿原工具块展示进展。"""
from copy import deepcopy

from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
from agent_py_agent.cli.chat_parts.tui_background_agents import TuiBackgroundAgents
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime


class Client:
    def __init__(self):
        self.calls = []
        self.status = 'EXECUTING'
        self.coverage = 'complete'
        self.denied = False

    def call(self, action, params):
        self.calls.append((action, deepcopy(params)))
        if action == 'background_agents.get':
            return {'ok': True, 'data': {'current_execution': None}}
        if action == 'agent_tree.get':
            return {'ok': True, 'data': {'coverage': self.coverage, 'next_cursor': None,
                'nodes': [{'id': 'seat', 'thread_id': 'seat', 'space_id': 's'},
                    {'id': 'wake-real', 'run_id': 'wake-real', 'thread_id': 'seat', 'space_id': 's', 'status': 'running'},
                    {'id': 'child', 'run_id': 'child', 'thread_id': 'child-thread', 'space_id': 's',
                     'parent_run_id': 'wake-real', 'status': 'running'},
                    {'id': 'foreign', 'run_id': 'foreign', 'space_id': 'other', 'parent_run_id': 'wake-real'}],
                'edges': [{'from_id': 'seat', 'to_id': 'wake-real', 'relation': 'executes'},
                    {'from_id': 'wake-real', 'to_id': 'child', 'relation': 'child_of'},
                    {'from_id': 'wake-real', 'to_id': 'foreign', 'relation': 'child_of'}]}}
        if action == 'tool_calls.list':
            run = params['filter']['run_id']
            return {'ok': True, 'data': {'coverage': 'complete', 'next_cursor': None, 'items': [{
                'operation_id': 'op-' + run, 'run_id': run, 'attempt_id': 'attempt', 'task_id': 'task',
                'tool_name': 'create_subagents' if run == 'wake-real' else 'run_command',
                'status': self.status, 'effect_outcome': 'unknown',
                'error': {'code': 'EXACT_FAILURE', 'message': '原错误'} if self.status == 'FAILED' else None}]}}
        if action == 'background_agents.list':
            return {'ok': True, 'data': {'coverage': 'complete', 'next_cursor': None, 'items': []}}
        if action == 'spaces.packages.list':
            if self.denied:
                return {'ok': False, 'http_status': 403, 'error': {'code': 'SCOPE_DENIED', 'message': '当前空间无权'}}
            return {'ok': True, 'data': {'coverage': 'complete', 'next_cursor': None, 'items': [{
                'package_ref': 'native-package', 'version_ref': 'v1', 'enabled': True,
                'availability': {'state': 'available'}}] if params['space_id'] == 's' else []}}
        raise AssertionError(action)


def test_exact_tree_links_choose_real_parent_and_children_not_agent_id():
    client = Client()
    projection = TuiBackgroundAgents(client)
    activity = projection.execution_activity('seat', thread_id='seat', space_id='s')
    assert activity['coverage'] == 'partial'  # 被剔除的跨空间节点不能声称完整。
    assert [row['id'] for row in activity['children']] == ['child']
    assert [params['filter']['run_id'] for action, params in client.calls if action == 'tool_calls.list'] == ['wake-real', 'child']
    assert not any(action in {'subagents.list', 'executions.list'} for action, _ in client.calls)
    client.coverage = 'partial'
    assert projection.execution_activity('seat', thread_id='seat', space_id='s')['coverage'] == 'partial'


def test_same_native_tool_blocks_start_finish_fail_and_deduplicate_without_changing_chat():
    client = Client()
    projection = TuiBackgroundAgents(client)
    runtime = TuiRuntime('selected-seat')
    navigation = TuiAgentNavigationState(runtime)
    runtime._publish('assistant_completed', 'completed', 'original-chat', {'text': '原聊天全文'})
    def publish():
        navigation._publish_business_activity('seat', runtime,
            projection.execution_activity('seat', thread_id='seat', space_id='s'))
    publish()
    assert sum(block.role == 'tool' for block in runtime.store.snapshot().active_blocks) == 2
    publish()
    assert sum(block.role == 'tool' for block in runtime.store.snapshot().active_blocks) == 2
    client.status = 'FAILED'
    publish()
    snapshot = runtime.store.snapshot()
    assert not any(block.role == 'tool' for block in snapshot.active_blocks)
    assert sum(block.role == 'tool' for block in snapshot.stable_blocks) == 2
    assert any('原聊天全文' in str(block) for block in snapshot.stable_blocks)
    assert any('EXACT_FAILURE' in str(block) for block in snapshot.stable_blocks)


def test_space_mount_read_is_only_on_selected_space_and_never_catalog():
    client = Client()
    projection = TuiBackgroundAgents(client)
    assert client.calls == []
    projection.load_space('s')
    assert 'native-package · 版本 v1 · 启用' in '\n'.join(projection.lines(space_id='s'))
    projection.load_space('empty')
    assert '未挂载能力包' in '\n'.join(projection.lines(space_id='empty'))
    assert 'native-package' not in '\n'.join(projection.lines(space_id='empty'))
    client.denied = True
    projection.load_space('denied')
    assert '无权读取' in '\n'.join(projection.lines(space_id='denied'))
    assert 'SCOPE_DENIED' in '\n'.join(projection.lines(space_id='denied'))
    assert not any(action.endswith('catalog') or action == 'tool_calls.list' for action, _ in client.calls)


def test_standard_keyboard_enters_only_selected_space_package_page():
    from agent_py_agent.tests.test_v15_w11_tui_navigation import NavigationClient

    client = NavigationClient()
    projection = TuiBackgroundAgents(client)
    projection.refresh()
    nav = TuiAgentNavigationState(TuiRuntime('front'))
    nav.update_business_agents(projection)
    assert not any(action == 'spaces.packages.list' for action, _ in client.calls)
    assert nav.move_selection(1) and nav.enter_selected()  # 后台团队
    assert nav.move_selection(1) and nav.enter_selected()  # 空间A
    assert [params['space_id'] for action, params in client.calls if action == 'spaces.packages.list'] == ['sp_A']
    assert not any(action == 'tool_calls.list' for action, _ in client.calls)


def test_activity_and_mount_caches_are_bounded_and_failures_remain_partial():
    client = Client()
    projection = TuiBackgroundAgents(client)
    for index in range(40):
        projection.load_space('space-' + str(index))
    assert len(projection._package_cache) <= 32
    assert len(projection._pages) <= 34
    runtime = TuiRuntime('bounded')
    navigation = TuiAgentNavigationState(runtime)
    for page in range(4):
        tools = [{'run_id': 'run', 'attempt_id': 'attempt', 'operation_id': str(page * 100 + index),
                  'tool_name': 'read_file', 'status': 'SUCCEEDED'} for index in range(100)]
        navigation._publish_business_activity('seat', runtime, {'tools': tools, 'coverage': 'partial'})
    assert len(navigation._business_tools['seat']) == 256
    original = client.call
    client.call = lambda action, params: ({'ok': False, 'error': {'code': 'READ_DENIED', 'message': '明确拒绝'}}
                                         if action == 'tool_calls.list' else original(action, params))
    activity = projection.execution_activity('seat', thread_id='seat', space_id='s')
    assert activity['coverage'] == 'partial' and activity['tools'] == []
    assert activity['errors'][0]['code'] == 'READ_DENIED'


def test_current_execution_survives_unavailable_historical_tree_and_uses_exact_attempt():
    client = Client()
    original = client.call
    def call(action, params):
        if action == 'background_agents.get':
            return {'ok': True, 'data': {'current_execution': {'run_id': 'wake-real',
                'attempt_id': 'attempt', 'thread_id': 'seat', 'space_id': 's'}}}
        if action == 'subagents.list':
            assert params['parent_run_id'] == 'wake-real'
            return {'ok': True, 'data': {'items': [], 'coverage': 'partial', 'next_cursor': 'more'}}
        assert action != 'agent_tree.get', 'current execution must not depend on historical owner pages'
        return original(action, params)
    client.call = call
    activity = TuiBackgroundAgents(client).execution_activity('seat', thread_id='seat', space_id='s')
    assert activity['current_execution']['run_id'] == 'wake-real'
    assert activity['tools'][0]['run_id'] == 'wake-real'
    assert activity['coverage'] == 'partial'
    assert client.calls[-1][1]['filter'] == {'run_id': 'wake-real', 'attempt_id': 'attempt'}
