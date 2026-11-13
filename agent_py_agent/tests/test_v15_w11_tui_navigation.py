"""W11 TUI 消费端验收；新领域接口用明确隔离桩，不冒充真实接线通过。"""
from __future__ import annotations

import json
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlCommand,
    ConversationControlResult,
)
from agent_py_agent.cli.chat_parts import control_runtime as control
from agent_py_agent.cli.chat_parts import tui_agent_navigation as navigation
from agent_py_agent.cli.chat_parts.tui_background_agents import (
    TuiBackgroundAgents,
    submit_business_input,
)
from agent_py_agent.cli.chat_parts.tui_block_renderer import (
    TuiRenderContext,
    _render_footer,
    tui_render_context_key,
)
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.space_commands import PreparedBusinessCall


# LLM: fixture 只模拟已约定的领域消费口，不替换 BusinessClient 的 schema 或真实生产模块。
# 函数用途: 生成身份与工作层明确分离的后台主 Agent 行。
def agent_row(identifier, space=None, *, work='idle', identity='available', thread=None):
    return {'agent_id': identifier, 'thread_id': thread or identifier, 'space_id': space,
            'name': identifier, 'purpose': '测试当前工作', 'identity_state': identity, 'work_state': work}


# LLM: 每次调用只返回一个有界页，严格区分省略 space_id 和显式 null；不实现任何任务生命周期。
# 类用途: 为导航单测提供带准确调用证据的隔离领域端口。
class NavigationClient:
    # LLM: 输入页均是测试数据，断言在组件边界执行，不依赖未来领域模块。
    # 函数用途: 初始化两个空间、空间内 Agent 与无归属 Agent。
    def __init__(self):
        self.calls = []
        self.spaces = [{'space_id': 'sp_A', 'name': '空间A'}, {'space_id': 'sp_empty', 'name': '空空间'}]
        self.agents = [agent_row('ag_in', 'sp_A', work='running', thread='thread-in'),
                       agent_row('ag_free', work='idle', thread='thread-free'),
                       agent_row('ag_other', work='waiting', identity='suspended')]
        self.roster_coverage = 'complete'
        self.roster_cursor = None
        self.denied = False

    # LLM: 这里保留传输参数用于反例断言；未知动作失败而非返回假成功。
    # 函数用途: 返回一页只读空间、名册、消息、树或日志。
    def call(self, action, params):
        self.calls.append((action, json.loads(json.dumps(params))))
        if self.denied:
            return {'ok': False, 'http_status': 403}
        if action == 'spaces.list':
            data = {'items': self.spaces, 'coverage': 'complete', 'next_cursor': None}
        elif action == 'spaces.packages.list':
            data = {'items': [], 'coverage': 'complete', 'next_cursor': None}
        elif action == 'background_agents.list':
            rows = self.agents if 'space_id' not in params else [r for r in self.agents if r['space_id'] == params['space_id']]
            data = {'items': rows, 'coverage': self.roster_coverage, 'next_cursor': self.roster_cursor}
        elif action == 'background_agents.get':
            data = {'current_execution': None}
        elif action == 'messages.list':
            thread = params['target']['id']
            data = {'items': [{'message_id': 'm2' if params.get('cursor') else 'm1', 'thread_id': thread,
                               'role': 'assistant', 'text': '完整正文'}], 'coverage': 'complete',
                    'next_cursor': None if params.get('cursor') else 'message-next'}
        elif action in {'agent_tree.get', 'logs.read'}:
            data = {'items': [], 'coverage': 'complete'}
        else:
            raise AssertionError(action)
        return {'ok': True, 'http_status': 200, 'data': data}

    # LLM: 桩只冻结组件输入，不声明领域受理或真实传输已验证。
    # 函数用途: 返回与生产消费口一致的不可变准备对象。
    def prepare(self, action, params):
        return PreparedBusinessCall(json.dumps({'action': action, 'params': params}))

    # LLM: 记录冻结目标，不执行任务或改变后台身份。
    # 函数用途: 供输入异步目标稳定性测试观察请求。
    def send(self, prepared):
        payload = prepared.payload()
        self.calls.append((payload['action'], payload['params']))
        return {'state': 'accepted'}


# LLM: 同步执行读任务让测试可确定观察页切换，不连接真实终端或启动后台任务。
# 类用途: 用相同目标函数完成一次 UI 历史读取。
class InlineThread:
    # LLM: 仅接收产品使用的明确参数，不放宽其它 Thread 契约。
    # 函数用途: 保存本次有界读取函数。
    def __init__(self, *, target, name, daemon):
        self.target = target

    # LLM: 直接执行一次，不循环、不吞异常。
    # 函数用途: 完成一次确定性读任务。
    def start(self):
        self.target()


# LLM: 所有测试都复用真实导航/renderer，仅把未来领域端口隔离。
# 函数用途: 构造已读聚合快照、尚未展开任何空间的真实导航。
def make_navigation(monkeypatch):
    monkeypatch.setattr(navigation, 'threading', SimpleNamespace(Thread=InlineThread, RLock=threading.RLock))
    client = NavigationClient()
    projection = TuiBackgroundAgents(client)
    projection.refresh()
    nav = navigation.TuiAgentNavigationState(TuiRuntime('thread-main'))
    nav.update_business_agents(projection)
    return client, projection, nav


# LLM: 根必须同时含空间和无归属，不得把空间内 Agent 平铺在根伪造层级。
# 函数用途: 核对混合根、精确会话、空空间、逐级返回及选择保留。
def test_mixed_root_space_and_unassigned_navigation_preserves_exact_target(monkeypatch):
    client, projection, nav = make_navigation(monkeypatch)
    assert [a for a, _ in client.calls] == ['background_agents.list']
    assert nav.move_selection(1) and nav.enter_selected()
    assert nav._selection_ids_locked('business:agents') == [
        'business:space:sp_A', 'business:space:sp_empty', 'business:ag_free', 'business:ag_other']
    assert client.calls[-1][1]['space_id'] is None
    assert nav.move_selection(1) and nav.enter_selected()
    assert nav.snapshot().business_space_id == 'sp_A'
    assert nav._selection_ids_locked('business:space:sp_A') == ['business:ag_in']
    assert client.calls[-1][1]['space_id'] == 'sp_A'
    assert nav.move_selection(1) and nav.enter_selected()
    assert nav.snapshot().active_thread_id == 'thread-in'
    assert nav.snapshot().active_agent_id == 'ag_in'
    assert nav.snapshot().active_run_id == ''
    assert [params for action, params in client.calls if action == 'messages.list'][-1]['target'] == {'kind': 'background_main', 'id': 'ag_in'}
    assert nav.back() and nav.snapshot().selected_run_id == 'business:ag_in'
    assert nav.back() and nav.snapshot().selected_run_id == 'business:space:sp_A'
    assert nav.move_selection(1) and nav.enter_selected()
    assert nav.snapshot().business_space_id == 'sp_empty'
    assert not nav.move_selection(1)
    assert '没有可显示的条目' in '\n'.join(projection.lines(space_id='sp_empty'))
    assert nav.back() and nav.move_selection(1) and nav.enter_selected()
    assert nav.snapshot().active_thread_id == 'thread-free'
    assert nav.back() and nav.snapshot().selected_run_id == 'business:ag_free'
    assert nav.back() and nav.snapshot().selected_run_id == 'business:agents'
    assert not any(a.endswith(('cancel', 'suspend', 'retire')) for a, _ in client.calls)


# LLM: 没有空间时不可增加默认空间或一层无意义分组。
# 函数用途: 验证无空间根只出现真正无归属 Agent。
def test_no_spaces_is_flat_and_never_leaks_assigned_agent(monkeypatch):
    client, _, nav = make_navigation(monkeypatch)
    client.spaces = []
    assert nav.move_selection(1) and nav.enter_selected()
    assert nav._selection_ids_locked('business:agents') == ['business:ag_free', 'business:ag_other']
    assert nav.move_selection(1) and nav.enter_selected()
    assert nav.snapshot().depth == 2 and nav.snapshot().active_agent_id == 'ag_free'


# LLM: available 仅可接单，运行数只来自完整列表中的 work_state。
# 函数用途: 守住身份、工作、进程计数三者分层及未知/partial不填零。
@pytest.mark.parametrize(('coverage', 'cursor', 'work', 'counts'), [
    ('complete', None, 'idle', (3, 1)), ('partial', None, 'idle', (None, None)),
    ('complete', 'next', 'idle', (None, None)), ('complete', None, 'unknown', (3, None)),
])
def test_counts_use_complete_work_facts_not_available(coverage, cursor, work, counts):
    client = NavigationClient()
    client.roster_coverage, client.roster_cursor = coverage, cursor
    client.agents[1]['work_state'] = work
    projection = TuiBackgroundAgents(client)
    projection.refresh()
    assert (projection.count, projection.running_count) == counts
    assert len(client.calls) == 1 and 0 < client.calls[0][1]['limit'] <= 100
    projection.load_root()
    text = '\n'.join(projection.lines())
    assert '身份：可接单' in text
    assert ('工作：待命' if work == 'idle' else '工作：未知') in text
    assert '身份：已挂起' in text and '工作：等待' in text


# LLM: 显式翻页只推进当前类型一页，完整页带游标也不能被当作完整总量。
# 函数用途: 证明名册分页有界、准确保留续点且不自动扫全历史。
def test_roster_next_page_is_explicit_and_bounded(monkeypatch):
    client, projection, nav = make_navigation(monkeypatch)
    client.roster_cursor = 'roster-next'
    assert nav.move_selection(1) and nav.enter_selected()
    assert nav._selection_ids_locked('business:agents')[-1] == 'business:more:unassigned'
    before = len(client.calls)
    projection.load_more(None, 'unassigned')
    assert len(client.calls) == before + 1
    assert client.calls[-1][1]['cursor'] == 'roster-next'
    assert client.calls[-1][1]['space_id'] is None
    assert projection.list_coverage(None) == 'partial', '重复游标必须显性不完整，不能无限自动重试'


# LLM: 一次打开最多一页，后续由显式 /history 读取；message身份不改成Agent身份。
# 函数用途: 核对消息有界读取、续点以及跨线程内容拒绝。
def test_history_is_one_page_with_exact_agent_and_thread():
    client = NavigationClient()
    projection = TuiBackgroundAgents(client)
    first = projection.conversation('ag_in', thread_id='thread-in')
    assert [r['message_id'] for r in first['items']] == [], '服务端串thread记录不能进入会话'
    assert first['coverage'] == 'partial' and first['next_cursor'] == 'message-next'
    assert len(client.calls) == 1
    second = projection.conversation('ag_in', cursor='message-next')
    assert second['next_cursor'] is None and [r['message_id'] for r in second['items']] == ['m2']
    assert len(client.calls) == 2


# LLM: 输入/树/日志必须冻结准确 agent，不能用与之不同的 thread_id 去寻址业务 Agent。
# 函数用途: 验证在空间内及无归属会话中调用原树、日志消费口。
def test_tree_log_and_input_bind_agent_not_thread(monkeypatch):
    import asyncio

    from prompt_toolkit.buffer import Buffer

    client, _, nav = make_navigation(monkeypatch)
    nav.move_selection(1)
    nav.enter_selected()
    nav.move_selection(3)
    nav.move_selection(1)
    nav.move_selection(1)
    nav.enter_selected()
    assert nav.snapshot().active_agent_id == 'ag_free'
    pending = []
    event = SimpleNamespace(app=SimpleNamespace(create_background_task=pending.append, invalidate=lambda: None))
    params = SimpleNamespace(agent_navigation=nav, input_area=SimpleNamespace(buffer=Buffer()))
    assert submit_business_input(event, params, '/tree')
    assert nav.back() and nav.back()
    asyncio.run(pending.pop())
    assert client.calls[-1] == ('agent_tree.get', {'space_id': None, 'root_agent_id': 'ag_free'})
    assert nav.snapshot().active_thread_id == ''


# LLM: 权限撤销必须清空全部空间缓存与已显示消息，不能借另一个空间旧帧复活。
# 函数用途: 守住混合导航权限失败后的旧内容清理。
def test_denial_clears_scoped_rosters_and_message_runtime(monkeypatch):
    client, projection, nav = make_navigation(monkeypatch)
    nav.move_selection(1)
    nav.enter_selected()
    nav.move_selection(1)
    nav.enter_selected()
    nav.move_selection(1)
    nav.enter_selected()
    client.denied = True
    projection.refresh()
    nav.update_business_agents(projection)
    assert projection.navigation_rows(None) == ()
    assert not nav.snapshot().can_send_input
    assert not nav._business_messages


# LLM: route 仍参与原缓存键，但不再显示在底栏；已知正总量与进程数只作尾标。
# 函数用途: 核对路由设置不污染控制提示，未知或零团队数量不占底栏。
def test_footer_route_counts_and_cache_key():
    runtime = TuiRuntime('thread-footer')
    first = TuiRenderContext(width=240, route_enabled=False, business_agents_enabled=True,
                            background_process_count=7, background_agent_count=3, background_agent_running_count=1)
    second = TuiRenderContext(width=240, route_enabled=True, business_agents_enabled=True,
                             background_process_count=7, background_agent_count=3, background_agent_running_count=1)
    snapshot = runtime.store.snapshot()
    text = ''.join(part[1] for part in _render_footer(snapshot, first))
    assert '/route' not in text and '简洁' not in text
    assert text.index('F4') < text.index('后台Agent：运行 1 / 总 3') < text.index('后台驻留 7')
    assert text.endswith(' · 后台Agent：运行 1 / 总 3 · ⚡ 后台驻留 7 项')
    assert _render_footer(snapshot, first) == _render_footer(snapshot, second)
    assert tui_render_context_key(snapshot, first) != tui_render_context_key(snapshot, second)
    for count in (None, 0):
        empty = ''.join(p[1] for p in _render_footer(snapshot, TuiRenderContext(
            width=240, business_agents_enabled=True, background_agent_count=count)))
        assert '/route' not in empty and '后台Agent' not in empty
        assert empty.startswith('  ? 快捷键 · F4 权限')


# LLM: 业务列表、后台会话和进程观察仍保留原导航与身份提示，只移除路由状态显示。
# 函数用途: 防止子页面底栏残留 /route 前缀或空团队数量。
@pytest.mark.parametrize('route', [None, False, True])
@pytest.mark.parametrize(('options', 'hint'), [
    ({'business_list_active': True}, 'Ctrl+G 返回 · Enter 下钻/下一页 · ↑↓ 选择'),
    ({'business_thread_active': True, 'business_identity_state': 'available',
      'business_work_state': 'running'}, '输入发往本后台会话 · /tree 子树 · /log 日志 · /history 下一页 · 身份：可接单 · 工作：运行中'),
    ({'background_watch_active': True}, 'Ctrl+G / Esc 返回 · 后台进程只读观察 · ↑↓ 切换输出'),
])
def test_navigation_footers_keep_controls_without_route(route, options, hint):
    snapshot = TuiRuntime('thread-footer-navigation').store.snapshot()
    text = ''.join(p[1] for p in _render_footer(snapshot, TuiRenderContext(
        width=240, route_enabled=route, business_agents_enabled=True, **options)))

    assert text.startswith('  Ctrl+G') and hint in text
    assert '/route' not in text and '后台Agent' not in text


# LLM: 新route_mode尚未集成，桩只验证CLI适配器入参与控制边界，不证明真实设置持久化。
# 函数用途: 安装父代理约定的公开接口并留下精确调用证据。
def route_stub(monkeypatch):
    module = ModuleType('agent_py_agent.agent.conversation.route_mode')
    calls = []

    # LLM: 返回结构化控制结果，不产生任何文件或偏好写入。
    # 函数用途: 记录本地控制使用的准确线程与可信actor。
    def execute(agent, command, thread, *, actor_id='', channel=''):
        calls.append((agent, command, thread, actor_id, channel))
        return ConversationControlResult('route', True, '模式已读取', details={'enabled': True, 'source': 'thread', 'revision': 2})

    # LLM: 只读接口的值来自明确桩，不由显示文案推断。
    # 函数用途: 返回可用于底栏的设置快照。
    def setting(agent, thread, *, actor_id='', channel=''):
        calls.append((agent, thread, actor_id, channel))
        return {'enabled': True, 'source': 'thread', 'revision': 2}

    module.execute_route_control, module.route_setting = execute, setting
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return calls


# LLM: direct需要canonical thread；从现有store解析，不创建新线程或停止工作。
# 函数用途: 为本地route接线提供准确线程和正在运行的回合。
def local_execution():
    thread = SimpleNamespace(thread_id='thread-real', canonical_user_id='user-trusted')
    agent = SimpleNamespace(conversation_store=SimpleNamespace(threads=SimpleNamespace(load=lambda identifier: thread)))
    state = control.ChatControlState(True, 0, '正在工作', 0.0, 'thread-real', request_id='request-running')
    return control.ChatControlExecution(agent, False, state), thread


# LLM: route不可落入本地stop默认分支，切换也不能取消当前回合。
# 函数用途: 验证direct route调用共享接口和原thread，不调用停止。
def test_direct_route_uses_public_shared_control(monkeypatch):
    calls = route_stub(monkeypatch)
    execution, thread = local_execution()
    monkeypatch.setattr(control, '_execute_local_stop', lambda execution, command: pytest.fail('route错误停止当前工作'))
    command = ConversationControlCommand('route', value='true')
    assert control.execute_chat_control(execution, command).ok
    assert calls == [(execution.agent, command, thread, 'user-trusted', 'chat')]
    assert control.chat_route_setting(execution.agent, 'thread-real') == {'enabled': True, 'source': 'thread', 'revision': 2}


# LLM: 所有delegate参数只回迁移提示，连非法参数也不能进入旧偏好/stop路径。
# 函数用途: 断言旧命令无写入且不触发本地执行控制。
@pytest.mark.parametrize('value', ['', 'enable', 'disable', 'status', 'true', 'nonsense extra'])
def test_delegate_retirement_never_mutates_or_stops(monkeypatch, value):
    execution, _ = local_execution()
    monkeypatch.setattr(control, '_execute_local_control', lambda execution, command: pytest.fail('delegate进入旧控制'))
    result = control.execute_chat_control(execution, ConversationControlCommand('delegate', value=value, valid=False, usage='old'))
    assert '/route' in result.message and '迁移' in result.message


# LLM: Gateway仍沿/control发结构化命令的原值，不发送prompt或新造route接口。
# 函数用途: 核对route序列化及原Gateway控制入口。
@pytest.mark.parametrize('value', ['true', 'false', 'status', ''])
def test_gateway_route_serialization_keeps_argument(monkeypatch, value):
    command = ConversationControlCommand('route', value=value)
    assert control._command_text(command) == ('/route ' + value).rstrip()
    execution, _ = local_execution()
    seen = []

    # LLM: 只替换已有credentialed运输边界，不把它当HTTP往返验收。
    # 函数用途: 确认Gateway模式沿用原控制方法且命令不变。
    def gateway(execution, command, *, message_id, target_control_message_id):
        seen.append(command)
        return ConversationControlResult('route', True, 'gateway')

    monkeypatch.setattr(control, '_credentialed_gateway_control', gateway)
    gateway_execution = control.ChatControlExecution(execution.agent, True, execution.state)
    assert control.execute_chat_control(gateway_execution, command).ok and seen == [command]


# LLM: 真实旧parser可能把参数标invalid，迁移提示必须在usage及持久outbox之前返回。
# 函数用途: 验证plain/TUI原始入口的所有delegate参数只提示、不发送。
@pytest.mark.parametrize('suffix', ['', ' enable', ' disable', ' status', ' nonsense extra'])
def test_delegate_raw_entry_never_writes_even_when_parser_rejects(suffix):
    from agent_py_agent.cli.chat_parts.slash_commands import _handle_control_command
    from agent_py_agent.cli.chat_parts.tui_actions import _tui_control_or_im_only_refusal

    printed = []
    ctx = SimpleNamespace(print_line=printed.append, control_executor=lambda command: pytest.fail('旧delegate不应运输'))
    assert _handle_control_command('/delegate' + suffix, ctx, False)
    assert len(printed) == 1 and '迁移' in printed[0] and '/route' in printed[0]
    runtime = TuiRuntime('raw-delegate')
    command, handled = _tui_control_or_im_only_refusal(SimpleNamespace(tui_runtime=runtime), '/delegate' + suffix)
    assert handled and command.kind == 'delegate'


# LLM: malformed计数不能抛出或变零，重复agent不能多占总数。
# 函数用途: 核对防御性统计与身份去重边界。
@pytest.mark.parametrize('damage', ['work', 'duplicate', 'row'])
def test_malformed_roster_does_not_fabricate_counts(damage):
    client = NavigationClient()
    if damage == 'work':
        client.agents[0]['work_state'] = []
    elif damage == 'duplicate':
        client.agents.append(dict(client.agents[0]))
    else:
        client.agents.append({})
    projection = TuiBackgroundAgents(client)
    projection.refresh()
    assert projection.running_count is None
    assert projection.count == (3 if damage == 'work' else None)


# LLM: route状态读取不能因无绑定创建会话，也不能取旧delegate数据补默认值。
# 函数用途: 断言只解析已有线程，无线程时显示未知。
def test_route_setting_no_thread_is_unknown_and_does_not_create(monkeypatch):
    calls = route_stub(monkeypatch)
    agent = SimpleNamespace(conversation_store=SimpleNamespace(threads=SimpleNamespace(
        resolve_report=lambda *, channel, channel_conversation_id, channel_user_id: (None, None),
        get_or_create=lambda facts: pytest.fail('只读状态创建了线程'))))
    assert control.chat_route_setting(agent, 'old-session')['enabled'] is None
    assert calls == []


# LLM: 不只测renderer字段存在，还通过真实context factory连上同一导航快照。
# 函数用途: 核对route及身份/工作两层从导航传到最终底栏。
def test_render_context_factory_wires_route_and_lifecycle(monkeypatch):
    from agent_py_agent.cli.chat_parts.tui_interaction import TuiInteractionState
    from agent_py_agent.cli.chat_parts.tui_transcript import TuiTranscriptModeState
    from agent_py_agent.cli.chat_parts.tui_ui_setup import _make_render_context_factory

    _, _, nav = make_navigation(monkeypatch)
    nav.update_route_setting({'enabled': True})
    params = SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(agent_name='test'), root=''),
                             tui_runtime=nav.root_runtime, agent_navigation=nav)
    factory = _make_render_context_factory(params, TuiInteractionState(), TuiTranscriptModeState())
    assert factory(240).route_enabled is True
    nav.move_selection(1)
    nav.enter_selected()
    nav.move_selection(1)
    nav.enter_selected()
    nav.move_selection(1)
    nav.enter_selected()
    context = factory(240)
    assert context.business_identity_state == 'available' and context.business_work_state == 'running'
    assert '身份：可接单' in ''.join(p[1] for p in _render_footer(nav.active_runtime().store.snapshot(), context))


# LLM: after_task策略由领域执行，UI只消费最新两层事实；退役中仍有运行时不能显示已退役。
# 函数用途: 核对reuse/suspend/retire结果和可见会话的工作状态刷新。
@pytest.mark.parametrize(('identity', 'work'), [('available', 'idle'), ('suspended', 'idle'),
                                              ('retiring', 'running'), ('retired', 'idle')])
def test_visible_lifecycle_uses_newest_authoritative_roster(monkeypatch, identity, work):
    client, projection, nav = make_navigation(monkeypatch)
    nav.move_selection(1)
    nav.enter_selected()
    nav.move_selection(1)
    nav.enter_selected()
    nav.move_selection(1)
    nav.enter_selected()
    client.agents[0]['identity_state'], client.agents[0]['work_state'] = identity, work
    projection.refresh()
    nav.update_business_agents(projection)
    assert (nav.snapshot().active_identity_state, nav.snapshot().active_work_state) == (identity, work)
    assert nav.back()
    assert projection.navigation_rows('sp_A')[0]['identity_state'] == identity
    assert projection.navigation_rows('sp_A')[0]['work_state'] == work


# LLM: 从真实导航选择到原输入消费者再返回，异步日志/消息不追随当前页面重新寻址。
# 函数用途: 冒烟验证精确guidance、日志和显式历史续页链。
def test_navigation_input_log_history_headless_sequence(monkeypatch):
    import asyncio

    from prompt_toolkit.buffer import Buffer

    client, projection, nav = make_navigation(monkeypatch)
    nav.move_selection(1)
    nav.enter_selected()
    nav.move_selection(1)
    nav.move_selection(2)
    nav.enter_selected()
    assert nav.snapshot().active_thread_id == 'thread-free'
    runtime = nav.active_runtime()
    pending = []
    event = SimpleNamespace(app=SimpleNamespace(create_background_task=pending.append, invalidate=lambda: None))
    params = SimpleNamespace(agent_navigation=nav, input_area=SimpleNamespace(buffer=Buffer()))
    assert submit_business_input(event, params, '精确guidance')
    assert submit_business_input(event, params, '/log audit-ref')
    before = sum(action == 'messages.list' for action, _ in client.calls)
    assert submit_business_input(event, params, '/history')
    assert sum(action == 'messages.list' for action, _ in client.calls) == before + 1
    assert client.calls[-1][1]['cursor'] == 'message-next'
    assert nav.back() and nav.back()
    for operation in pending:
        asyncio.run(operation)
    assert client.calls[-2:] == [('background_agents.messages.send',
        {'agent_id': 'ag_free', 'text': '精确guidance', 'input_kind': 'message'}), ('logs.read', {'source_ref': 'audit-ref'})]
    assert runtime.notice() == '回执：unknown；不等于执行完成'
    assert projection.count == 3 and nav.snapshot().depth == 0


# LLM: 写设置后发生I/O错误不能反推未写入；适配器保留unknown而不是伪报成功/未修改。
# 函数用途: 验证direct路由故障既不停止任务，也不声称设置已经保存。
def test_direct_route_failure_is_explicit_unknown(monkeypatch):
    route_stub(monkeypatch)
    execution, _ = local_execution()
    module = sys.modules['agent_py_agent.agent.conversation.route_mode']

    # LLM: 模拟明确的I/O失败，不能让测试误入默认停止分支。
    # 函数用途: 在共享控制调用边界返回不可确认故障。
    def failed(agent, command, thread, *, actor_id='', channel=''):
        raise OSError('simulated I/O error')

    monkeypatch.setattr(module, 'execute_route_control', failed)
    result = control.execute_chat_control(execution, ConversationControlCommand('route', value='true'))
    assert not result.ok and result.delivery_status == 'unknown' and '未确认' in result.message


# LLM: 总量来自Owner权威配额覆盖，不把有界名册页当全量；未知或坏配额不能退回局部页数冒充已知。
# 函数用途: 证明完整配额可跨页显示总量，而运行数仍须完整工作事实。
@pytest.mark.parametrize(('quota', 'cursor', 'expected'), [
    ({'coverage': 'complete', 'used': 9}, 'next', 9),
    ({'coverage': 'complete', 'used': 0}, None, 0),
    ({'coverage': 'partial', 'used': 9}, None, None),
    ({'coverage': 'complete', 'used': True}, None, None),
    ({'coverage': 'complete', 'used': -1}, None, None),
    (None, None, None),
])
def test_owner_quota_total_is_independent_of_roster_page(monkeypatch, quota, cursor, expected):
    client = NavigationClient()
    client.roster_cursor = cursor
    original = client.call

    # LLM: 仅给原领域读口附上真实配额DTO字段，名册和原调用证据保持不变。
    # 函数用途: 模拟同一响应内两个覆盖面不同的有界事实。
    def call(action, params):
        result = original(action, params)
        result['data']['quota'] = quota
        return result

    monkeypatch.setattr(client, 'call', call)
    projection = TuiBackgroundAgents(client)
    projection.refresh()
    assert projection.count == expected
    assert len(client.calls) == 1
    if cursor or expected is None:
        assert projection.running_count is None
