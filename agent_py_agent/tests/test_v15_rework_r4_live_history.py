"""F09：真实 canonical 消息经业务服务、原生 JSON 路由和客户端进入 TUI。

只预选来自规范线程的 UI 目标；不伪造消息页、不替换加载方法或服务。
线程工厂仍创建真实线程，测试用 join/Event 控制完成顺序，不靠 sleep。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_team_lifecycle import BACKGROUND_IDENTITY_KEY
from agent_py_agent.agent.conversation.business_observability import BusinessObservationService
from agent_py_agent.agent.conversation.control_commands import ConversationControlCommand
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.store_threads import create_thread_atomic
from agent_py_agent.agent.gateway_parts.business_api import (
    BusinessServices,
    bind_business_context,
    execute_business,
)
from agent_py_agent.cli.chat_parts import tui_agent_navigation as navigation
from agent_py_agent.cli.chat_parts.control_runtime import (
    ChatControlExecution,
    ChatControlState,
    execute_chat_control,
)
from agent_py_agent.cli.chat_parts.tui_background_agents import TuiBackgroundAgents
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary
from agent_py_agent.cli.space_commands import BusinessClient


class LoadThreads:
    """仅记录真实 UI 加载线程以便确定性等待；不替换其目标函数。"""

    def __init__(self):
        self.threads = []

    def create(self, *, target, name, daemon):
        thread = threading.Thread(target=target, name=name, daemon=daemon)
        self.threads.append(thread)
        return thread

    def join(self):
        for thread in self.threads:
            thread.join(timeout=5)
            assert not thread.is_alive(), 'UI 消息读取没有有界结束'


class CanonicalTransport:
    """只负责 JSON 往返和记录；DTO、授权、分页及封套全部由真实产品生成。"""

    def __init__(self, home, store):
        self.allowed = True
        self.requests = []
        self.responses = []
        self.fail_once = False
        self.after_response = None
        host = SimpleNamespace(home_paths=SimpleNamespace(owner_id='local/r4', owner_home_dir=home),
            config=SimpleNamespace(business_spaces_enabled=True), conversation_store=store)
        self.service = BusinessObservationService(home, owner_id='local/r4', store=store,
            cursor_key=b'r' * 32)
        self.services = BusinessServices(host, projection=self.service)
        self.context = bind_business_context(host, subject_id='r4-reader',
            authorize=lambda _action, _facts: self.allowed)

    def __call__(self, path, payload, *, timeout):
        assert path == '/client/business' and timeout > 0
        request = json.loads(json.dumps(payload))
        assert request['action'] == 'messages.list'
        assert request['params']['limit'] == 100
        self.requests.append(request)
        if self.fail_once:
            self.fail_once = False
            raise OSError('simulated connection interruption')
        status, response = execute_business(request, self.context, self.services)
        result = json.loads(json.dumps(response))
        self.responses.append((status, result))
        assert result['request_id'] == request['request_id']
        if self.after_response is not None:
            self.after_response()
        return status, result


@pytest.fixture
def case(tmp_path, monkeypatch):
    # 防止环境里的 editable 安装把测试悄悄指向另一个工作树。
    assert Path(navigation.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[2])
    store = ConversationStore(tmp_path / 'conversations')
    rows = []
    create_thread_atomic(store.threads, ConversationThread(thread_id='controller-r4',
        owner_id='local/r4', canonical_user_id='r4-reader', metadata={'business_controller': True}))
    for identifier in ('background-a', 'background-b'):
        thread = ConversationThread(thread_id=identifier, owner_id='local/r4', canonical_user_id='r4-reader',
            title=identifier, metadata={BACKGROUND_IDENTITY_KEY: {'agent_id': identifier,
                'managed_by_thread_id': 'controller-r4', 'space_id': None, 'project_ref': None,
                'identity_state': 'available', 'after_task': 'reuse', 'identity_revision': 1}})
        create_thread_atomic(store.threads, thread)
        rows.append({'agent_id': thread.thread_id, 'thread_id': thread.thread_id, 'space_id': None,
            'name': thread.title, 'identity_state': 'available', 'work_state': 'idle'})
    transport = CanonicalTransport(tmp_path, store)
    projection = TuiBackgroundAgents(BusinessClient(transport))
    projection.rows = tuple(rows)
    # 名册不是本次变更合同；预选规范身份，消息数据仍全部走真实 producer/router/client。
    projection._pages['unassigned'] = {'items': tuple(rows), 'coverage': 'complete', 'next_cursor': None}
    threads = LoadThreads()
    monkeypatch.setattr(navigation, 'threading', SimpleNamespace(Thread=threads.create, RLock=threading.RLock))
    nav = navigation.TuiAgentNavigationState(TuiRuntime('r4-root'))
    nav.update_business_agents(projection)
    yield SimpleNamespace(store=store, transport=transport, projection=projection, threads=threads, nav=nav)
    threads.join()
    assert not nav._business_loading


def append(case, content, *, thread='background-a', role='assistant'):
    return case.store.messages.append({'thread_id': thread, 'role': role, 'content': content,
        'channel': 'internal'})


def seed(case, count):
    return [append(case, f'历史-{index:03d}') for index in range(count)]


def enter(case, thread='background-a', *, wait=True):
    nav = case.nav
    nav._path[:] = ['business:agents']
    nav._selected_by_parent['business:agents'] = 'business:' + thread
    assert nav.enter_selected()
    if wait:
        case.threads.join()
    snapshot = nav.snapshot()
    assert snapshot.active_thread_id == snapshot.active_agent_id == thread
    assert snapshot.active_run_id == ''
    return nav.active_runtime()


def texts(runtime):
    snapshot = runtime.store.snapshot()
    out = []
    for block in (*snapshot.stable_blocks, *snapshot.active_blocks):
        if block.block_id.startswith('console:'):
            out.append(block.text)
        elif block.block_id.startswith('history:'):
            role = block.role
            role_map = {"user": "user", "assistant": "assistant", "tool": "tool", "system": "host"}
            role_name = role_map.get(role, role)
            out.append(f"{role_name}：{block.text}")
    return out


def refresh(case):
    before = len(case.transport.requests)
    case.nav.update_business_agents(case.projection)
    case.threads.join()
    assert len(case.transport.requests) == before + 1, '未读旧历史不能阻止实时查询'
    assert 'cursor' not in case.transport.requests[-1]['params'], '实时刷新不能重用旧历史游标'
    assert case.transport.requests[-1]['params']['target'] == {'kind': 'background_main', 'id': 'background-a'}


@pytest.mark.parametrize('count', [105, 205])
def test_live_refresh_with_unread_history_reads_current_page(case, count):
    original = seed(case, count)
    runtime = enter(case)
    nav, transport = case.nav, case.transport
    assert len(transport.requests) == 1
    status, first = transport.responses[-1]
    assert status == 200 and first['state'] == 'completed'
    assert first['data']['coverage'] == 'partial' and len(first['data']['items']) == 100
    assert {row['message_id'] for row in first['data']['items']} == {row.message_id for row in original[-100:]}
    cursor = nav._business_history['background-a']['next_cursor']
    assert cursor and len(nav._business_messages['background-a']) == 100
    reply = append(case, '实时回复-未读旧页')
    refresh(case)
    assert reply.message_id in nav._business_messages['background-a']
    assert texts(runtime).count('assistant：实时回复-未读旧页') == 1
    assert nav._business_history['background-a']['next_cursor'] == cursor
    assert '/history' in runtime.notice() and 'partial' in runtime.notice()
    refresh(case)
    assert texts(runtime).count('assistant：实时回复-未读旧页') == 1


@pytest.mark.parametrize('count', [105, 205])
def test_live_refresh_after_history_is_exhausted_never_reuses_old_cursor(case, count):
    original = seed(case, count)
    runtime = enter(case)
    nav, transport = case.nav, case.transport
    expected_pages = (count - 100 + 99) // 100
    for _ in range(expected_pages):
        cursor = nav._business_history['background-a']['next_cursor']
        assert cursor
        before = len(transport.requests)
        assert nav.load_more_business_messages()
        case.threads.join()
        assert len(transport.requests) == before + 1
        assert transport.requests[-1]['params']['cursor'] == cursor
    assert nav._business_history['background-a']['next_cursor'] is None
    assert nav._business_messages['background-a'] == {row.message_id for row in original}
    reply = append(case, '实时回复-历史已读完')
    # 同一真实客户端先证明 canonical 最新页确实已包含这条回复，不从 UI 失败推测数据丢失。
    latest = case.projection.client.call('messages.list', {
        'target': {'kind': 'background_main', 'id': 'background-a'}, 'limit': 100})
    assert latest['ok'] and reply.message_id in {row['message_id'] for row in latest['data']['items']}
    refresh(case)
    assert reply.message_id in nav._business_messages['background-a']
    assert texts(runtime).count('assistant：实时回复-历史已读完') == 1
    assert nav._business_history['background-a']['next_cursor'] is None
    assert '/history' not in runtime.notice(), '已读完的旧历史不能因末页刷新再次打开'


def test_partial_history_continuation_only_advances_on_explicit_history(case):
    original = seed(case, 205)
    runtime = enter(case)
    first_cursor = case.nav._business_history['background-a']['next_cursor']
    assert case.nav.load_more_business_messages()
    case.threads.join()
    cursor = case.nav._business_history['background-a']['next_cursor']
    assert cursor and cursor != first_cursor
    assert len(case.nav._business_messages['background-a']) == 200
    reply = append(case, '部分旧页后的实时回复')
    refresh(case)
    assert reply.message_id in case.nav._business_messages['background-a']
    assert case.nav._business_history['background-a']['next_cursor'] == cursor
    assert 'partial' in runtime.notice() and '/history' in runtime.notice()
    assert case.nav.load_more_business_messages()
    case.threads.join()
    assert case.transport.requests[-1]['params']['cursor'] == cursor
    assert case.nav._business_messages['background-a'] == {row.message_id for row in [*original, reply]}
    before = len(case.transport.requests)
    assert case.nav.load_more_business_messages()
    case.threads.join()
    assert len(case.transport.requests) == before, '耗尽后的 /history 不自动改读当前页'
    assert len(texts(runtime)) == len(set(texts(runtime))) == 206


def test_reentry_reads_latest_without_restarting_consumed_history(case):
    seed(case, 105)
    runtime = enter(case)
    assert case.nav.load_more_business_messages()
    case.threads.join()
    assert case.nav.back()
    reply = append(case, '重新进入后的实时回复')
    before = len(case.transport.requests)
    assert case.nav.enter_selected()
    case.threads.join()
    assert case.nav.active_runtime() is runtime
    assert len(case.transport.requests) == before + 1
    assert 'cursor' not in case.transport.requests[-1]['params']
    assert reply.message_id in case.nav._business_messages['background-a']
    assert case.nav._business_history['background-a']['next_cursor'] is None
    assert texts(runtime).count('assistant：重新进入后的实时回复') == 1


def pause_one_response(case):
    ready, release = threading.Event(), threading.Event()

    def pause():
        case.transport.after_response = None
        ready.set()
        assert release.wait(timeout=5), '测试没有释放原消息页'

    case.transport.after_response = pause
    case.nav.update_business_agents(case.projection)
    assert ready.wait(timeout=5)
    return release


def test_single_inflight_page_stays_bound_to_original_thread_and_runtime(case):
    seed(case, 105)
    runtime_a = enter(case)
    reply_a = append(case, '只属于 A 的实时回复')
    reply_b = append(case, '只属于 B 的实时回复', thread='background-b')
    release = pause_one_response(case)
    try:
        before = len(case.transport.requests)
        case.nav.update_business_agents(case.projection)
        assert case.nav.load_more_business_messages()
        assert len(case.transport.requests) == before, '同 thread 的实时/历史操作只能有一个在途页'
        runtime_b = enter(case, 'background-b', wait=False)
        case.threads.threads[-1].join(timeout=5)
        assert not case.threads.threads[-1].is_alive()
        assert texts(runtime_b) == ['assistant：只属于 B 的实时回复']
    finally:
        release.set()
        case.threads.join()
    assert case.nav.active_runtime() is runtime_b
    assert case.nav.snapshot().active_thread_id == 'background-b'
    assert reply_a.message_id in case.nav._business_messages['background-a']
    assert reply_a.message_id not in case.nav._business_messages['background-b']
    assert reply_b.message_id not in case.nav._business_messages['background-a']
    assert 'assistant：只属于 A 的实时回复' in texts(runtime_a)
    assert 'assistant：只属于 A 的实时回复' not in texts(runtime_b)


def test_reset_during_inflight_page_does_not_restore_old_display_or_ids(case):
    seed(case, 105)
    runtime = enter(case)
    before_ids = set(case.nav._business_messages['background-a'])
    reply = append(case, '重置前在途的旧回复')
    release = pause_one_response(case)
    try:
        runtime.publish_conversation_reset('background-a')
        assert texts(runtime) == []
    finally:
        release.set()
        case.threads.join()
    assert texts(runtime) == [], '已重置的 UI 不能被迟到旧页重新填入正文'
    assert reply.message_id not in case.nav._business_messages['background-a']
    assert case.nav._business_messages['background-a'] == before_ids


def descriptor_count():
    """读取本进程原生句柄数；目录枚举自身也在返回前关闭。"""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessHandleCount.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        count = wintypes.DWORD()
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count))
        return count.value
    path = '/proc/self/fd' if os.path.isdir('/proc/self/fd') else '/dev/fd'
    with os.scandir(path) as entries:
        return sum(1 for _entry in entries)


@pytest.mark.parametrize('operation', ['initial', 'current', 'history'])
def test_single_read_failure_retains_cursor_retries_and_has_zero_fd_delta(case, operation):
    seed(case, 105)
    if operation != 'initial':
        runtime = enter(case)
    cursor = case.nav._business_history.get('background-a', {}).get('next_cursor')
    before_fds = descriptor_count()
    case.transport.fail_once = True
    if operation == 'initial':
        runtime = enter(case)
    elif operation == 'current':
        refresh(case)
    else:
        assert case.nav.load_more_business_messages()
        case.threads.join()
    assert not case.nav._business_loading
    assert descriptor_count() - before_fds == 0
    assert case.nav._business_history.get('background-a', {}).get('next_cursor') == cursor
    assert 'unknown' in runtime.notice(), '单次运输失败不能冒充完整空页'
    reply = append(case, '单次读取失败后的新回复')
    refresh(case)
    assert descriptor_count() - before_fds == 0
    assert reply.message_id in case.nav._business_messages['background-a']
    assert texts(runtime).count('assistant：单次读取失败后的新回复') == 1
    assert case.nav._business_history['background-a']['next_cursor']
    if cursor:
        assert case.nav._business_history['background-a']['next_cursor'] == cursor


def test_revoked_access_uses_real_denial_and_clears_display_ids_and_history(case):
    seed(case, 105)
    runtime = enter(case)
    before_fds = descriptor_count()
    case.transport.allowed = False
    refresh(case)
    status, denial = case.transport.responses[-1]
    assert status == 403 and denial['state'] == 'failed'
    assert denial['error']['code'] == 'CAPABILITY_DENIED'
    assert case.projection.read_denied and case.projection.rows == ()
    assert texts(runtime) == []
    assert case.nav._business_messages == {} and case.nav._business_history == {}
    assert not case.nav.snapshot().can_send_input
    before = len(case.transport.requests)
    case.nav.update_business_agents(case.projection)
    case.threads.join()
    assert len(case.transport.requests) == before
    assert not case.nav._business_loading
    assert descriptor_count() - before_fds == 0


def test_replaced_projection_discards_inflight_result_then_refreshes_new_source(case):
    seed(case, 105)
    runtime = enter(case)
    reply = append(case, '替换观察源后的回复')
    release = pause_one_response(case)
    try:
        previous = case.projection
        replacement = TuiBackgroundAgents(previous.client)
        replacement.rows = previous.rows
        replacement._pages = dict(previous._pages)
        case.projection = replacement
        case.nav.update_business_agents(replacement)
    finally:
        release.set()
        case.threads.join()
    assert reply.message_id not in case.nav._business_messages['background-a']
    refresh(case)
    assert reply.message_id in case.nav._business_messages['background-a']
    assert texts(runtime).count('assistant：替换观察源后的回复') == 1


def test_headless_stream_steer_stop_new_next_turn_and_restart_keep_target(case):
    """真实控制口做 steer/new；模型回调与停止收口在真实 TUI runtime 中确定性驱动。

    不把 UI 中断意图/回合回执当成实际模型进程退出的证明。
    """
    seed(case, 105)
    background = enter(case)
    root = case.nav.root_runtime
    host = case.transport.services.host
    original = case.store.threads.get_or_create({'canonical_user_id': 'local-agent', 'channel': 'chat',
        'channel_conversation_id': root.session_id, 'channel_user_id': 'local-agent',
        'owner_id': host.home_paths.owner_id, 'owner_home': str(host.home_paths.owner_home_dir)})
    root.publish_conversation_reset(original.thread_id)
    root.enqueue_prompt('r4-request-old', '开始生成', queued=False)
    old_turn = root.begin_turn('r4-request-old')
    old_turn.write_model('流式内容')
    execution = ChatControlExecution(host, False, ChatControlState(True, 0, '运行中', 0.0,
        root.session_id, request_id='r4-request-old'))
    steer = execute_chat_control(execution, ConversationControlCommand('steer', value='保留当前任务身份'))
    assert steer.ok and steer.request_id == 'r4-request-old'
    root.enqueue_active_turn_input('r4-steer-message', '保留当前任务身份')
    assert [item.message_id for item in root.store.snapshot().pending_steers] == ['r4-steer-message']
    before = root.store.snapshot()
    reply = append(case, '根会话流式期间的后台回复')
    refresh(case)
    assert root.store.snapshot() == before, '后台观察不能切换前台任务、状态栏或输入队列'
    assert reply.message_id in case.nav._business_messages['background-a']
    assert root.request_interrupt()
    assert root.store.snapshot().status.phase == 'interrupting'

    # 原 worker 的停止收口尚未到达显示端；/new 后旧 adapter 须保留到按原身份收口。
    idle = ChatControlExecution(host, False, ChatControlState(False, 0, '就绪', 0.0, root.session_id))
    result = execute_chat_control(idle, ConversationControlCommand('new'))
    assert result.ok
    successor = case.store.threads.resolve(channel='chat', channel_conversation_id=root.session_id,
        channel_user_id='local-agent')
    assert successor.thread_id != original.thread_id
    root.publish_conversation_reset(successor.thread_id)
    assert 'r4-request-old' in root._retired_turns
    assert root.store.snapshot().pending_steers == ()
    root.enqueue_prompt('r4-request-next', '下一轮', queued=False)
    next_turn = root.begin_turn('r4-request-next')
    next_turn.write_model('下一轮流式内容')
    next_snapshot = root.store.snapshot()
    old_turn.write_model('不允许进入下一轮的迟到输出')
    root.complete_turn('r4-request-old', TuiTurnSummary(ok=False, interrupted=True))
    assert root.store.snapshot() == next_snapshot
    assert 'r4-request-old' not in root._retired_turns
    root.complete_turn('r4-request-next', TuiTurnSummary(response_text='下一轮回复'))
    assert not root._turns and not root._retired_turns and not root._foreground_gateway_bindings
    assert root.store.snapshot().queued_inputs == ()
    assert case.nav.snapshot().active_thread_id == 'background-a'
    assert texts(background).count('assistant：根会话流式期间的后台回复') == 1

    # 重建 UI 导航而不改 canonical 消息，再做一次真实单页失败/重试。
    case.nav = navigation.TuiAgentNavigationState(root)
    case.nav.update_business_agents(case.projection)
    restarted = enter(case)
    assert restarted is not background
    assert reply.message_id in case.nav._business_messages['background-a']
    case.transport.fail_once = True
    refresh(case)
    refresh(case)
    assert texts(restarted).count('assistant：根会话流式期间的后台回复') == 1
    assert case.nav.snapshot().active_thread_id == 'background-a'
    assert not case.nav._business_loading


@pytest.mark.parametrize('initial', [0, 5, 105])
def test_new_current_window_gap_keeps_all_unseen_history_reachable(case, initial):
    original = seed(case, initial)
    runtime = enter(case)
    burst = [append(case, f'跨页新增-{index:03d}') for index in range(105)]
    refresh(case)
    assert len(case.transport.responses[-1][1]['data']['items']) == 100
    assert case.nav._business_history['background-a']['next_cursor'], '最新页出现未见旧消息时必须保留可达续点'
    # 只有这里的显式用户操作续读；正常刷新仍只读一页，原来未读的更老历史也必须最终可达。
    for _ in range((initial + len(burst) + 99) // 100):
        if not case.nav._business_history['background-a']['next_cursor']:
            break
        before = len(case.transport.requests)
        assert case.nav.load_more_business_messages()
        case.threads.join()
        assert len(case.transport.requests) == before + 1
    assert case.nav._business_history['background-a']['next_cursor'] is None
    assert case.nav._business_messages['background-a'] == {row.message_id for row in [*original, *burst]}
    assert len(texts(runtime)) == len(set(texts(runtime))) == len(original) + len(burst)
