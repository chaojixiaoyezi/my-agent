# LLM: wire正文与字节边界由真实ConversationStore/page reader产生；只替换HTTP出口，无模型、网络或真实用户数据。
# 模块用途: 回归跨窗口/new的thread游标重基、归属去重、安全拒绝和迟到页隔离。
from __future__ import annotations

import json
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.message_stream import read_background_response_page
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices


# LLM: 独立owner存储，thread通过真实succeed更换，未手造notice行或字节偏移返回值。
# 函数用途: 创建映射到固定聊天通道的真实会话。
def _store(tmp_path):
    store = ConversationStore(tmp_path / 'conversations')
    thread = store.threads.get_or_create({'canonical_user_id': 'local-agent', 'channel': 'chat',
        'channel_conversation_id': 'session', 'channel_user_id': 'local-agent', 'now': 1.0,
        'owner_id': 'owner-test', 'owner_home': str(tmp_path)})
    return store, thread.thread_id


# LLM: 前台final的归属元数据走生产append入口；不加background_delivery_reason，避免冒充后台续跑。
# 函数用途: 在canonical消息流写下一个真实可投影final。
def _append(store, thread_id, text, request_id='gwreq-owner'):
    return store.messages.append({'thread_id': thread_id, 'role': 'assistant', 'content': text, 'now': 20.0,
        'metadata': {'assistant_part_id': 'final', 'gateway_request_id': request_id}})


# LLM: 只组装HTTP信封；notices、cursor和ok全部取真实server reader，失败也保留已解析的绑定。
# 类用途: 记录每次poll的输入和真实页结果，供接收端端到端回归。
class _Wire:
    # LLM: 读数仅用于断言，不替代生产runtime或通知去重。
    # 函数用途: 保存当前server线程和有限poll日志。
    def __init__(self, store, thread_id):
        self.store, self.thread_id, self.calls = store, thread_id, []

    # LLM: 生产API同形传输出口，每次只做一个有界消息页读取；不查全历史或凭文本伪造失败。
    # 函数用途: 用真实byte reader返回本次线程页，并原样记录失败种类。
    def request_background_notices(self, _session_id, *, after, event_after, event_stream_id):
        notices, cursor, ok = read_background_response_page(self.store, self.thread_id, after=after,
                                                           include_foreground=True)
        failure = '' if ok else 'corrupt_or_unknown'
        self.calls.append({'thread_id': self.thread_id, 'after': after, 'cursor': cursor,
                           'failure_kind': failure, 'ok': ok})
        print(json.dumps(self.calls[-1], ensure_ascii=False))
        return {'ok': ok, 'thread_id': self.thread_id, 'binding_state': 'confirmed',
                'failure_kind': failure, 'cursor': cursor, 'notices': notices,
                'transcript_events': [], 'event_cursor': event_after, 'event_stream_id': event_stream_id,
                'agent_activity': {'active_task_count': 0, 'compact_count': 0,
                                   'context_usage': {}, 'model_metrics': {}}}


def test_real_successor_invalid_16737_rebases_once_and_filters_owned_final(tmp_path):
    store, old = _store(tmp_path)
    _append(store, old, '')
    row_size = store.storage.message_path(old).stat().st_size
    _append(store, old, 'x' * (16737 - 2 * row_size))
    assert store.storage.message_path(old).stat().st_size == 16737
    successor = store.threads.succeed(old).thread_id
    own = _append(store, successor, 'B own ' + 'b' * 4500)
    other = _append(store, successor, 'A visible ' + 'a' * 18000, 'gwreq-other')
    first_boundary = store.messages.byte_offset_after(successor, own.message_id)
    end = store.messages.byte_offset_after(successor, other.message_id)
    assert first_boundary < 16737 < end
    assert read_background_response_page(store, successor, after=16737, include_foreground=True) == ([], 16737, False)
    wire, runtime, seen = _Wire(store, successor), TuiRuntime('session'), set()
    runtime.background_message_cursor, runtime.background_message_thread_id = 16737, old
    runtime.register_gateway_request('gwreq-owner')
    redraws = []
    app = SimpleNamespace(invalidate=lambda: redraws.append(1))
    assert _consume_background_notices(wire, 'session', runtime, [app], seen)
    assert runtime.background_message_cursor == 0 and runtime.background_message_thread_id == successor
    assert not runtime.store.snapshot().stable_blocks and not seen
    assert redraws
    for _ in range(3):
        assert _consume_background_notices(wire, 'session', runtime, [app], seen)
    assert [row['after'] for row in wire.calls] == [16737, 0, end, end]
    assert len(wire.calls) == 4 and sum(row['after'] == 0 for row in wire.calls) == 1
    texts = [block.text for block in runtime.store.snapshot().stable_blocks]
    assert texts.count(other.content) == 1 and own.content not in texts
    assert not any(text.startswith('x') for text in texts)
    assert runtime.background_message_cursor == end
    assert runtime.store.snapshot().status.thread_id == successor


def test_successor_equal_file_length_uses_identity_not_offset(tmp_path):
    store, old = _store(tmp_path)
    _append(store, old, 'equal length old')
    successor = store.threads.succeed(old).thread_id
    message = _append(store, successor, 'equal length new')
    old_size = store.storage.message_path(old).stat().st_size
    assert old_size == store.storage.message_path(successor).stat().st_size
    wire, runtime, seen = _Wire(store, successor), TuiRuntime('session'), set()
    runtime.background_message_cursor, runtime.background_message_thread_id = old_size, old
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    assert runtime.background_message_cursor == 0
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    assert [row['after'] for row in wire.calls] == [old_size, 0, old_size]
    assert [block.text for block in runtime.store.snapshot().stable_blocks] == [message.content]


@pytest.mark.parametrize('patch', [
    {'binding_state': 'invalid'}, {'error_code': 'PERMISSION_DENIED'},
    {'failure_kind': 'owner_mismatch'}, {'thread_id': 2}, {'thread_id': ' bad '},
    {'execution_target': {'thread_id': 'different-thread'}},
])
def test_untrusted_successor_never_rebases_or_publishes(tmp_path, patch):
    store, old = _store(tmp_path)
    successor = store.threads.succeed(old).thread_id
    _append(store, successor, 'must not display')
    wire, runtime = _Wire(store, successor), TuiRuntime('session')
    runtime.background_message_cursor, runtime.background_message_thread_id = 16737, old
    original = wire.request_background_notices
    wire.request_background_notices = lambda *args, **kwargs: {**original(*args, **kwargs), **patch}
    assert not _consume_background_notices(wire, 'session', runtime, [None], set())
    assert (runtime.background_message_cursor, runtime.background_message_thread_id) == (16737, old)
    assert not runtime.store.snapshot().stable_blocks


def test_late_page_from_old_poll_cannot_revert_a_successor_rebase(tmp_path):
    store, old = _store(tmp_path)
    _append(store, old, 'old must stay absent')
    successor = store.threads.succeed(old).thread_id
    current = _append(store, successor, 'new stays visible')
    runtime, seen = TuiRuntime('session'), set()
    runtime.background_message_thread_id = old
    started, release, results = Event(), Event(), []
    old_wire, wire = _Wire(store, old), _Wire(store, successor)
    original = old_wire.request_background_notices
    # LLM: 事件排序只延迟传输返回；线程数据和回调仍来自真实store/client链。
    # 函数用途: 保留一张旧poll，等新线程重基完成后才送达。
    def delayed(*args, **kwargs):
        payload = original(*args, **kwargs)
        started.set()
        assert release.wait(5)
        return payload
    old_wire.request_background_notices = delayed
    thread = Thread(target=lambda: results.append(_consume_background_notices(old_wire, 'session', runtime, [None], seen)))
    thread.start()
    try:
        assert started.wait(5)
        assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert runtime.background_message_thread_id == successor and runtime.background_message_cursor == 0
    assert not runtime.store.snapshot().stable_blocks
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    assert [block.text for block in runtime.store.snapshot().stable_blocks] == [current.content]


def test_reset_during_fetch_discards_page_and_rebase(tmp_path):
    store, old = _store(tmp_path)
    successor = store.threads.succeed(old).thread_id
    _append(store, successor, 'late response')
    wire, runtime = _Wire(store, successor), TuiRuntime('session')
    runtime.background_message_cursor, runtime.background_message_thread_id = 16737, old
    original = wire.request_background_notices
    # LLM: 同步回调发生reset，不用sleep制造竞态。
    # 函数用途: 验证捕获代次之后返回的旧页没有任何副作用。
    def fetch(*args, **kwargs):
        payload = original(*args, **kwargs)
        runtime.publish_conversation_reset('newer-thread')
        return payload
    wire.request_background_notices = fetch
    assert _consume_background_notices(wire, 'session', runtime, [None], set())
    assert (runtime.background_message_cursor, runtime.background_message_thread_id) == (0, 'newer-thread')
    assert not runtime.store.snapshot().stable_blocks


def test_local_successor_rebinds_before_reading_and_never_replays_old_rows(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import message_stream
    store, old = _store(tmp_path)
    _append(store, old, 'local old', request_id='')
    runtime, seen = TuiRuntime('session'), set()
    runtime.background_message_cursor = store.storage.message_path(old).stat().st_size
    runtime.background_message_thread_id = old
    successor = store.threads.succeed(old).thread_id
    message = store.messages.append({'thread_id': successor, 'role': 'assistant', 'content': 'local new',
        'metadata': {'assistant_part_id': 'final', 'background_delivery_reason': 'root_subagents_terminal'}})
    reads = []
    real_reader = message_stream.read_background_response_page
    # LLM: wrapper只计数并调用同一真实页读取，不返回定制行或吞掉校验错误。
    # 函数用途: 证明本地路径不再把旧offset交给新文件。
    def read(store, thread_id, **kwargs):
        reads.append((thread_id, kwargs['after']))
        return real_reader(store, thread_id, **kwargs)
    monkeypatch.setattr(message_stream, 'read_background_response_page', read)
    agent = SimpleNamespace(conversation_store=store)
    assert _consume_background_notices(agent, 'session', runtime, [None], seen)
    assert runtime.background_message_thread_id == successor
    assert _consume_background_notices(agent, 'session', runtime, [None], seen)
    assert reads[0] == (successor, 0)
    assert [block.text for block in runtime.store.snapshot().stable_blocks] == [message.content]


# LLM: canonical final行带请求真实thread；旧cursor不能替新前台请求决定线程归属。
# 函数用途: 检查先begin后确认successor且先消费owned final时回答仍显示一次。
def test_review_owned_successor_final_binds_exact_foreground_request(tmp_path):
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary
    store, old = _store(tmp_path)
    _append(store, old, 'old row')
    successor = store.threads.succeed(old).thread_id
    own = _append(store, successor, 'owned successor reply', 'gwreq-owner')
    runtime, wire, seen = TuiRuntime('session'), _Wire(store, successor), set()
    runtime.background_message_thread_id = old
    runtime.register_gateway_request('gwreq-owner')
    old_turn = runtime.begin_turn('other-old')
    runtime.begin_turn('new-local-id', task_progress_generation_id='gwreq-owner')
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    assert (successor, own.message_id) in seen
    runtime.complete_turn('new-local-id', TuiTurnSummary(response_text=own.content))
    texts = [block.text for block in runtime.store.snapshot().stable_blocks]
    assert texts.count(own.content) == 1
    assert old_turn._thread_id == old
    assert not any(block.block_id.startswith('thinking:new-local-id:') for block in runtime.store.snapshot().active_blocks)


# LLM: 延迟发生在真实reader边界；保留navigation与外层finally生产实现。
# 函数用途: 迟到T1页不能在T2重基后把后台观察范围切回旧线程。
def test_review_late_local_poll_keeps_successor_navigation_scope(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import background_process_view, message_stream
    from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
    store, old = _store(tmp_path)
    runtime, seen = TuiRuntime('session'), set()
    runtime.background_message_thread_id = old
    nav = TuiAgentNavigationState(runtime)
    agent = SimpleNamespace(conversation_store=store,
        home_paths=SimpleNamespace(owner_home_dir=tmp_path, owner_id='owner-test'))
    monkeypatch.setattr(background_process_view, 'list_background_processes',
        lambda _home, *, thread_id, expected_owner_id: {'projection_ok': True, 'items': [], 'active_count': 0, 'errors': []})
    started, release, errors = Event(), Event(), []
    reader = message_stream.read_background_response_page
    # LLM: 只延迟旧线程原页返回，不伪造IO结果。
    # 函数用途: 等待第二poll建立新观察范围后放行旧页。
    def delayed(store, thread_id, **kwargs):
        value = reader(store, thread_id, **kwargs)
        if thread_id == old:
            started.set()
            assert release.wait(5)
        return value
    monkeypatch.setattr(message_stream, 'read_background_response_page', delayed)
    # LLM: 异常交回主线程，结束前join不遗留后台工作。
    # 函数用途: 执行真正的旧本地poll，包括外层finally。
    def poll_old():
        try:
            _consume_background_notices(agent, 'session', runtime, [None], seen, agent_navigation=nav)
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=poll_old)
    thread.start()
    try:
        assert started.wait(5)
        successor = store.threads.succeed(old).thread_id
        assert _consume_background_notices(agent, 'session', runtime, [None], seen, agent_navigation=nav)
        assert _consume_background_notices(agent, 'session', runtime, [None], seen, agent_navigation=nav)
        assert nav._background_watch_scope[3] == successor
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert runtime.background_message_thread_id == successor
    assert nav._background_watch_scope[3] == successor


# LLM: Event仅排序完整finalize与真实notice消费；不替换reducer、final正文、cursor或去重。
# 函数用途: 覆盖pop后及finalize后两条交接缝隙，成功正文必须恰好显示一次且无残留thinking。
@pytest.mark.parametrize('arrival', ['before_finalize', 'after_finalize'])
def test_review_owned_final_during_completion_handoff(tmp_path, monkeypatch, arrival):
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary
    store, old = _store(tmp_path)
    successor = store.threads.succeed(old).thread_id
    own = _append(store, successor, 'handoff successor reply', 'gwreq-owner')
    runtime, wire, seen = TuiRuntime('session'), _Wire(store, successor), set()
    runtime.background_message_thread_id = old
    runtime.register_gateway_request('gwreq-owner')
    old_turn = runtime.begin_turn('other-old')
    adapter = runtime.begin_turn('new-local-id', task_progress_generation_id='gwreq-owner')
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    entered, release, errors = Event(), Event(), []
    finalize = adapter.finalize

    # LLM: complete_turn已进入真实完成路径；保留原finalize全部副作用，只暂停交接顺序。
    # 函数用途: 把owned final放在pop后或原finalize后、release前送达。
    def delayed(summary):
        if arrival == 'after_finalize':
            finalize(summary)
        entered.set()
        assert release.wait(5)
        if arrival == 'before_finalize':
            finalize(summary)

    monkeypatch.setattr(adapter, 'finalize', delayed)

    # LLM: 不吞工作线程异常，所有退出均join，运行结果回到主测试线程断言。
    # 函数用途: 运行真实complete_turn，允许通知线程完整进入显示交接窗口。
    def complete():
        try:
            runtime.complete_turn('new-local-id', TuiTurnSummary(response_text=own.content))
        except BaseException as exc:
            errors.append(exc)

    worker = Thread(target=complete)
    worker.start()
    try:
        assert entered.wait(5)
        assert _consume_background_notices(wire, 'session', runtime, [None], seen)
        assert (successor, own.message_id) in seen
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    for _ in range(2):
        assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    view = runtime.store.snapshot()
    assert [block.text for block in view.stable_blocks].count(own.content) == 1
    assert not any(block.block_id.startswith('thinking:new-local-id:') for block in view.active_blocks)
    assert old_turn._thread_id == old
    assert 'new-local-id' not in runtime._turns and 'new-local-id' not in runtime._retired_turns
    assert 'new-local-id' not in runtime._foreground_gateway_bindings
    assert 'gwreq-owner' not in runtime._owned_gateway_requests


# LLM: 第一张canonical final页严格晚于真实complete_turn；同正文不同请求仍须分别显示。
# 函数用途: 前台已经交付后才首次轮询，不能因释放adapter而再显示一次相同请求。
@pytest.mark.parametrize('thread_known', [True, False])
def test_review_first_canonical_final_after_release_is_not_duplicated(tmp_path, thread_known):
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary
    store, thread_id = _store(tmp_path)
    own = _append(store, thread_id, 'already delivered reply')
    runtime, wire, seen = TuiRuntime('session'), _Wire(store, thread_id), set()
    if thread_known:
        runtime.background_message_thread_id = thread_id
    runtime.register_gateway_request('gwreq-owner')
    runtime.begin_turn('local-owner', task_progress_generation_id='gwreq-owner')
    runtime.complete_turn('local-owner', TuiTurnSummary(response_text=own.content))
    assert [block.text for block in runtime.store.snapshot().stable_blocks].count(own.content) == 1
    assert not seen
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    assert (thread_id, own.message_id) in seen
    assert [block.text for block in runtime.store.snapshot().stable_blocks].count(own.content) == 1
    other = _append(store, thread_id, own.content, 'gwreq-unrelated')
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    assert (thread_id, other.message_id) in seen
    assert [block.text for block in runtime.store.snapshot().stable_blocks].count(own.content) == 2
    assert not runtime._turns and not runtime._completing_turns
    assert not runtime._foreground_gateway_bindings and not runtime._owned_gateway_requests


# LLM: 过程行由生产append/read产生；legacy变体只去掉可选thread字段，源观察仍在T1捕获。
# 函数用途: T1暂存过程在真实T2重基后必须丢弃，不能重开旧正文或动画。
@pytest.mark.parametrize('legacy_thread', [False, True])
def test_review_deferred_process_cannot_replay_on_successor(tmp_path, legacy_thread):
    from agent_py_agent.agent.conversation import background_transcript as transcript
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary
    store, old = _store(tmp_path)
    runtime = TuiRuntime('session')
    runtime.background_message_thread_id = old
    runtime.register_gateway_request('gwreq-owner')
    runtime.begin_turn('old-local', task_progress_generation_id='gwreq-owner')
    source = transcript.TranscriptSource(thread_id=old, task_id='task-old',
        request_id='bg-main:old-request', gateway_request_id='gwreq-owner')
    agent = SimpleNamespace()
    transcript.append_background_transcript_event(agent, source=source, kind='thinking_started',
        phase='started', block_id='bg-main:old-request:thinking:1', payload={'text': 'old progress'})
    transcript.append_background_transcript_event(agent, source=source, kind='assistant_completed',
        phase='completed', block_id='bg-main:old-request:assistant:1', payload={'text': 'retired response'})
    rows = transcript.read_background_transcript_events(agent, thread_id=old, after=0)['events']
    if legacy_thread:
        rows = [{key: value for key, value in row.items() if key != 'thread_id'} for row in rows]
    assert runtime.publish_background_transcript_events(rows) == 2
    assert len(runtime._deferred_background_events) == 2
    successor = store.threads.succeed(old).thread_id
    assert _consume_background_notices(_Wire(store, successor), 'session', runtime, [None], set())
    runtime.complete_turn('old-local', TuiTurnSummary(response_text='retired response'))
    view = runtime.store.snapshot()
    assert runtime.background_message_thread_id == successor
    assert not any(block.block_id.startswith('bg-main:old-request:')
                   for block in (*view.stable_blocks, *view.active_blocks))
    assert not runtime._deferred_background_events


# LLM: 保留原finalize与真实reader，仅在两者之间确定排序；不能用额外旧adapter掩盖running残留。
# 函数用途: canonical补显后关闭精确前台观察，同时不能结束另一个还在运行的回合。
@pytest.mark.parametrize('another_running', [False, True])
def test_review_fallback_final_closes_only_its_foreground_observation(tmp_path, monkeypatch, another_running):
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary
    store, old = _store(tmp_path)
    successor = store.threads.succeed(old).thread_id
    own = _append(store, successor, 'late confirmed final')
    runtime, wire, seen = TuiRuntime('session'), _Wire(store, successor), set()
    runtime.background_message_thread_id = old
    runtime.register_gateway_request('gwreq-owner')
    adapter = runtime.begin_turn('local-owner', task_progress_generation_id='gwreq-owner')
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    other = runtime.begin_turn('concurrent-new') if another_running else None
    finalize = adapter.finalize

    # LLM: 不改finalizer结果，不伪造notice；只把canonical首次消费放在finalize后、release前。
    # 函数用途: 重现正文可补显但前台running状态未收口的交错。
    def final_then_notice(summary):
        finalize(summary)
        assert _consume_background_notices(wire, 'session', runtime, [None], seen)

    monkeypatch.setattr(adapter, 'finalize', final_then_notice)
    runtime.complete_turn('local-owner', TuiTurnSummary(response_text=own.content))
    view = runtime.store.snapshot()
    assert [block.text for block in view.stable_blocks].count(own.content) == 1
    assert not any(block.block_id.startswith('thinking:local-owner:') for block in view.active_blocks)
    assert view.status.phase == ('running' if another_running else 'idle')
    assert view.has_active_work is another_running
    if other is not None:
        assert not other._finished and runtime._turns['concurrent-new'] is other


# LLM: 同一canonical请求的生产过程流首次晚于前台完成；不能靠消息页seen掩盖过程流入口。
# 函数用途: 已交付请求的迟到过程不重复正文、不重开thinking，别的请求仍正常展示。
def test_review_late_process_after_release_does_not_reopen_delivered_request(tmp_path):
    from agent_py_agent.agent.conversation import background_transcript as transcript
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary
    store, thread_id = _store(tmp_path)
    own = _append(store, thread_id, 'process late reply')
    runtime = TuiRuntime('session')
    runtime.background_message_thread_id = thread_id
    runtime.register_gateway_request('gwreq-owner')
    runtime.begin_turn('local-owner', task_progress_generation_id='gwreq-owner')
    runtime.complete_turn('local-owner', TuiTurnSummary(response_text=own.content))
    agent = SimpleNamespace()
    source = transcript.TranscriptSource(thread_id=thread_id, task_id='task-owner',
        request_id='bg-main:owner', gateway_request_id='gwreq-owner')
    for kind, phase, suffix, text in [('thinking_started', 'started', 'thinking', 'old thinking'),
                                     ('assistant_completed', 'completed', 'assistant', own.content)]:
        transcript.append_background_transcript_event(agent, source=source, kind=kind, phase=phase,
            block_id=f'bg-main:owner:{suffix}', payload={'text': text})
    rows = transcript.read_background_transcript_events(agent, thread_id=thread_id, after=0)['events']
    assert runtime.publish_background_transcript_events(rows) == 2
    view = runtime.store.snapshot()
    assert [block.text for block in view.stable_blocks].count(own.content) == 1
    assert not any(block.block_id.startswith('bg-main:owner:') for block in (*view.stable_blocks, *view.active_blocks))
    unrelated = transcript.TranscriptSource(thread_id=thread_id, task_id='task-other',
        request_id='bg-main:other', gateway_request_id='gwreq-other')
    transcript.append_background_transcript_event(agent, source=unrelated, kind='assistant_completed',
        phase='completed', block_id='bg-main:other:assistant', payload={'text': own.content})
    rows = transcript.read_background_transcript_events(agent, thread_id=thread_id, after=2)['events']
    assert runtime.publish_background_transcript_events(rows) == 3
    assert [block.text for block in runtime.store.snapshot().stable_blocks].count(own.content) == 2


# LLM: 复用生产有界身份窗口，仅缩小测试容量；记录中不得含adapter或正文，reset后旧身份不能吞新页。
# 函数用途: 连续交付时淘汰旧身份，结束后清引用，并验证去重围栏也包含reset代次。
def test_review_delivery_identity_window_is_bounded_and_generation_scoped():
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary
    runtime = TuiRuntime('session')
    runtime._delivered_foreground_requests.maximum = 2
    for index in range(3):
        gateway_id, local_id = f'gwreq-{index}', f'local-{index}'
        runtime.register_gateway_request(gateway_id)
        runtime.begin_turn(local_id, task_progress_generation_id=gateway_id)
        runtime.complete_turn(local_id, TuiTurnSummary(response_text=f'answer {index}'))
        assert len(runtime._delivered_foreground_requests) <= 2
    assert {key[0] for key in runtime._delivered_foreground_requests} == {'gwreq-1', 'gwreq-2'}
    assert all(isinstance(key, tuple) and len(key) == 3 and isinstance(key[2], int)
               for key in runtime._delivered_foreground_requests)
    assert not runtime._turns and not runtime._retired_turns and not runtime._completing_turns
    assert not runtime._foreground_gateway_bindings
    runtime.publish_conversation_reset('thread-next')
    runtime.publish_background_response('new generation reply', thread_id='thread-next',
        message_id='next-message', gateway_request_id='gwreq-2')
    assert [block.text for block in runtime.store.snapshot().stable_blocks] == ['new generation reply']


# LLM: Direct入口不注册Gateway ownership；用真实本地poll重基，验证显示phase清理不依赖远端归属表。
# 函数用途: 旧本地回合跨successor结束后不残留running，另一在途回合仍可独立运行并正常结束。
@pytest.mark.parametrize('overlap', [False, True])
def test_review_local_successor_completion_releases_only_its_display_phase(tmp_path, overlap):
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary
    store, old = _store(tmp_path)
    runtime = TuiRuntime('session')
    runtime.background_message_thread_id = old
    adapter = runtime.begin_turn('local-old')
    generation = runtime.conversation_reset_generation
    assert not runtime._owned_gateway_requests
    successor = store.threads.succeed(old).thread_id
    agent = SimpleNamespace(conversation_store=store)
    assert _consume_background_notices(agent, 'session', runtime, [None], set())
    assert runtime.background_message_thread_id == successor
    assert runtime.conversation_reset_generation == generation
    current = runtime.begin_turn('local-new') if overlap else None
    runtime.complete_turn('local-old', TuiTurnSummary(response_text='old local final'))
    view = runtime.store.snapshot()
    assert view.status.phase == ('running' if overlap else 'idle')
    assert 'old local final' not in [block.text for block in view.stable_blocks]
    assert not any(block.block_id in adapter._display_block_ids for block in view.active_blocks)
    if current is None:
        current = runtime.begin_turn('local-new')
    assert not current._finished
    runtime.complete_turn('local-new', TuiTurnSummary(response_text='successor local reply'))
    view = runtime.store.snapshot()
    assert view.status.phase == 'idle'
    assert [block.text for block in view.stable_blocks].count('successor local reply') == 1
    assert not runtime._turns and not runtime._retired_turns and not runtime._completing_turns
    assert not runtime._foreground_gateway_bindings and not runtime._owned_gateway_requests
