# LLM: 覆盖IM模型等待期间的同消息进度心跳、敏感思考隔离、外层超时/停止守卫有界回收及异常配置回退，不放宽生产超时或取消合同。
# 模块用途: 验证模型等待活性观察者（model_wait_activity）在真实流写入器与生命周期守卫下的触发、停止与隔离边界。
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_model_generation import (
    _CallLiveness,
    _do_backend_generate,
)
from agent_py_agent.agent.common.cancellation import CancellationToken
from agent_py_agent.agent.gateway_parts.http_handlers import _read_public_progress_events
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter


# LLM: 合成状态沿真实调用入口，只隔离配置和身份，不提供业务正文或改变模型传输逻辑。
# 函数用途: 为模型等待测试准备真实 writer 与同一调用的活性守卫。
def _state(writer, *, interval=1):
    params = SimpleNamespace(context_scope='task_local', effective_on_chunk=writer,
                             cancellation_token=CancellationToken())
    return SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(im_stream_heartbeat_seconds=interval)),
        params=params, tools=None, tool_choice=None, messages=None, on_chunk=writer.write_model,
        call_id='model-synthetic-wait', tool_rounds=2, liveness=_CallLiveness(),
        started_at=time.monotonic(), stream_observer_events=set(), system_instruction='')


# LLM: 验证思考增量不泄漏到公开流，且1秒心跳在单次等待窗口内稳定产出唯一model_liveness事件并在返回或异常后立即停止。
# 函数用途: 确认仅输出思考而无可见正文的模型生成会刷新一次等待心跳并在结束时停止。
@pytest.mark.parametrize('raises', [False, True])
def test_actual_model_generation_thinking_without_visible_output_refreshes_and_stops(tmp_path, raises):
    writer = BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', delivery_channel='feishu')
    writer.set_verbose_level('stream', timezone='Asia/Shanghai')

    # LLM: 模拟支持思考流但不立即可见输出的模型后端；保底等待1.15s，若CI高负载下首个1.0s心跳尚未落盘则最多顺延至1.85s（低于第二次心跳阈值）。
    # 类用途: 在受控时间窗内持续发送敏感思考增量，结束后按参数返回结果或抛出异常。
    class Backend:
        supports_thinking_completion = True
        supports_provider_request_options = False

        # LLM: 持续推送思考增量直到满1.15s且首个model_liveness事件落盘（上限1.85s），消除CI线程调度抖动导致的0事件偶发假阴性。
        # 函数用途: 执行模拟模型生成并在退出前触发一次等待心跳。
        def generate(self, prompt, **kwargs):
            observer = kwargs.get('on_thinking_delta')
            started = time.monotonic()
            until = started + 1.15
            deadline = started + 1.85
            while time.monotonic() < until or (
                not (writer.chunk_path.exists() and writer.chunk_path.stat().st_size > 0)
                and time.monotonic() < deadline
            ):
                if callable(observer):
                    observer('sensitive-thinking-not-public')
                time.sleep(.05)
            if raises:
                raise RuntimeError('synthetic failure')
            return SimpleNamespace(text='final')

    state = _state(writer)
    if raises:
        with pytest.raises(RuntimeError, match='synthetic failure'):
            _do_backend_generate(Backend(), 'synthetic prompt', state)
    else:
        assert _do_backend_generate(Backend(), 'synthetic prompt', state).text == 'final'
    public, _ = _read_public_progress_events(writer.chunk_path, 0, channel='feishu')
    assert len(public) == 1 and public[0]['kind'] == 'model_liveness'
    assert public[0]['wait_state'] == 'model_response'
    assert public[0]['model_call_id'] == state.call_id
    assert public[0]['elapsed_seconds'] >= .9
    assert public[0]['updated_at'] and public[0]['preview_max_chars'] == 100
    assert 'sensitive-thinking-not-public' not in writer.chunk_path.read_text()
    before = writer.chunk_path.read_text()
    time.sleep(.08)
    assert writer.chunk_path.read_text() == before


# LLM: 沿真实GatewayReplyDeliveryWorker与草稿流验证同一消息草稿复用与100字符预览截断，确保思考内容不外发。
# 函数用途: 验证模型等待进度复用有界同消息草稿并在最终回复到达后完成投递。
def test_model_wait_reuses_bounded_same_message_draft_without_sending_thinking(tmp_path):
    from agent_py_agent.agent.adapter.delivery import (
        GatewayReplyDeliveryStore,
        GatewayReplyDeliveryWorker,
        PendingGatewayReply,
    )
    from agent_py_agent.agent.adapter.manager import _render_gateway_progress
    from agent_py_agent.agent.adapter.progress_draft import GatewayProgressBatch, send_gateway_draft
    from agent_py_agent.agent.delivery import (
        ChannelAdapterRegistry,
        ChannelCapabilities,
        DeliveryService,
    )
    from agent_py_agent.tests.test_im_progress_draft import Adapter

    writer = BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', delivery_channel='feishu')
    writer.set_verbose_level('off', timezone='Asia/Shanghai')
    provider, registry = Adapter(), ChannelAdapterRegistry()
    registry.register_adapter('feishu', provider, capabilities=ChannelCapabilities(edit_progress=True))
    service = DeliveryService(registry)
    store = GatewayReplyDeliveryStore(tmp_path / 'delivery')
    final, finals = [None], []

    # LLM: 从测试chunk文件按游标读取公开进度事件并渲染为网关进度批次。
    # 函数用途: 供投递worker轮询当前待发送进度草稿。
    def poll(row):
        events, cursor = _read_public_progress_events(writer.chunk_path, row.progress_cursor, channel='feishu')
        return GatewayProgressBatch(tuple(_render_gateway_progress(event) for event in events), cursor,
            'stream', True, 100, events[-1].get('updated_at', '') if events else '')

    worker = GatewayReplyDeliveryWorker(store, poll_response=lambda _: final[0],
        deliver_response=lambda _, text: finals.append(text) or True, poll_progress=poll,
        deliver_progress=lambda row, text: send_gateway_draft(service, row, text))
    worker.enqueue(PendingGatewayReply(request_id='gw-model-wait', channel='feishu', user_id='ou_synthetic', message_id='om_synthetic'))
    for elapsed in (120, 240):
        writer.write_model_liveness({'model_call_id': 'model-wait', 'phase': 'waiting',
            'wait_state': 'model_response', 'elapsed_seconds': elapsed})
        # 原发送节流按真实时间；合成第二次与第一次分隔超过一秒。
        if elapsed == 240:
            time.sleep(1.01)
        worker.run_once()
    assert len(provider.created) == 1 and len(provider.edits) == 1
    assert provider.edits[0][0] == 'om_preview'
    assert '模型请求尚未返回' in provider.edits[0][1] and '240' in provider.edits[0][1]
    assert '更新于 ' in provider.edits[0][1] and len(provider.edits[0][1]) <= 100
    final[0] = '完整最终结果' * 40
    worker.run_once()
    assert finals == [final[0]] and not store.pending()


# LLM: 覆盖富转录本地TUI、chat通道及interval=0禁用三类场景，确认不启动等待心跳也不写chunk文件。
# 函数用途: 验证禁用心跳或本地非IM通道不会发布模型等待事件。
@pytest.mark.parametrize('rich,channel,interval', [(True, 'feishu', 1), (False, 'chat', 1), (False, 'feishu', 0)])
def test_disabled_or_local_model_wait_does_not_publish(tmp_path, rich, channel, interval):
    writer = BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', rich_transcript=rich, delivery_channel=channel)

    # LLM: 立即返回结果的最小后端桩，不产生流事件。
    # 类用途: 验证无需等待观察的快速模型调用路径。
    class Backend:
        supports_thinking_completion = False
        supports_provider_request_options = False

        # LLM: 直接返回固定响应对象。
        # 函数用途: 模拟瞬时完成的模型生成。
        def generate(self, prompt, **kwargs):
            return SimpleNamespace(text='final')

    _do_backend_generate(Backend(), 'synthetic', _state(writer, interval=interval))
    assert not writer.chunk_path.exists()


# LLM: 验证外层调用abandon时立即停止并回收展示观察线程，无需等待阻塞中的模型后端worker返回。
# 函数用途: 确认调用被放弃后等待心跳线程立刻失权并退出且不再落盘。
def test_abandon_stops_observer_while_backend_worker_has_not_returned(tmp_path):
    writer = BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', delivery_channel='feishu')
    state, release, entered = _state(writer), threading.Event(), threading.Event()

    # LLM: 进入后置位事件并阻塞等待测试主线程放行，模拟未返回的慢模型调用。
    # 类用途: 保持模型worker存活以检验abandon对展示线程的独立停止能力。
    class Backend:
        supports_thinking_completion = False
        supports_provider_request_options = False

        # LLM: 通知已进入generate并等待release信号。
        # 函数用途: 阻塞模型worker直到断言完成。
        def generate(self, prompt, **kwargs):
            entered.set()
            assert release.wait(3)
            return SimpleNamespace(text='late')

    worker = threading.Thread(target=lambda: _do_backend_generate(Backend(), 'synthetic', state))
    worker.start()
    try:
        assert entered.wait(1)
        observer = state.liveness._display_waiter
        assert observer is not None and observer._thread.is_alive()
        state.liveness.abandon()
        assert worker.is_alive() and not observer.is_current()
        assert observer.close() and not observer._thread.is_alive()
        time.sleep(1.05)
        assert not writer.chunk_path.exists()
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()


# LLM: 验证_generate_with_wall_timeout在正常返回与异常退出两条路径上都会关闭展示观察者并清空引用。
# 函数用途: 确认外层墙钟守卫退出时必然回收等待观察线程。
@pytest.mark.parametrize('failed', [False, True])
def test_outer_wall_guard_closes_observer_on_every_exit(tmp_path, monkeypatch, failed):
    from agent_py_agent.agent.agent_core import tool_model_generation as generation
    from agent_py_agent.agent.agent_core.model.im_wait_activity import model_wait_activity

    writer = BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', delivery_channel='feishu')
    state = _state(writer)
    observer = model_wait_activity(state)

    # LLM: 绑定观察者后按failed参数返回正常响应或抛出中断异常。
    # 函数用途: 替代内层墙钟守卫以检验finally回收逻辑。
    def guard(*args):
        state.liveness.bind_display_waiter(observer)
        if failed:
            raise InterruptedError('synthetic stop')
        return SimpleNamespace(text='finished')

    monkeypatch.setattr(generation, '_generate_through_wall_guard', guard)
    monkeypatch.setattr(generation, '_restore_failed_call_turn_input', lambda _: None)
    request = SimpleNamespace(agent=state.agent, params=state.params)
    if failed:
        with pytest.raises(InterruptedError):
            generation._generate_with_wall_timeout(request, state)
    else:
        generation._generate_with_wall_timeout(request, state)
    assert not observer._thread.is_alive() and state.liveness._display_waiter is None
    assert not writer.chunk_path.exists()


# LLM: 覆盖NaN与正负无穷配置，确认非有限浮点数一律回退到120秒默认间隔而不会坍缩成1秒高频刷屏。
# 函数用途: 验证非有限心跳间隔配置的防呆回退。
@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), float('-inf')])
def test_nonfinite_interval_never_becomes_one_second(tmp_path, invalid):
    from agent_py_agent.agent.agent_core.model.im_wait_activity import model_wait_activity

    state = _state(BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', delivery_channel='feishu'), interval=invalid)
    assert model_wait_activity(state).interval_seconds == 120


# LLM: 覆盖空串、None、非字符串及纯空白call_id，确认缺少模型调用身份时不创建等待观察者。
# 函数用途: 验证缺失合法model_call_id时拒绝启动等待心跳。
@pytest.mark.parametrize('call_id', ['', None, 0, '   '])
def test_pending_missing_model_identity_does_not_publish(tmp_path, call_id):
    from agent_py_agent.agent.agent_core.model.im_wait_activity import model_wait_activity

    state = _state(BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', delivery_channel='feishu'))
    state.call_id = call_id
    assert model_wait_activity(state) is None


# LLM: 验证启动前已取消的token不会拉起线程，以及已关闭的观察者不可二次重启。
# 函数用途: 确认取消态或关闭态下ActivityHeartbeat不会启动后台线程。
def test_cancelled_before_start_or_closed_observer_cannot_restart(tmp_path):
    from agent_py_agent.agent.agent_core.model.im_wait_activity import model_wait_activity

    state = _state(BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', delivery_channel='feishu'))
    observer = model_wait_activity(state)
    state.params.cancellation_token.cancel()
    observer.start()
    assert observer._thread is None


# LLM: 当effective_on_chunk传入绑定方法writer.write_model时，仍能通过__self__解析原BufferedChunkStreamWriter并写入liveness事件。
# 函数用途: 验证绑定方法回调下仍能正确定位所属stream_writer并拒绝关闭后重启。
def test_bound_callback_uses_original_sink_owner(tmp_path):
    from agent_py_agent.agent.agent_core.model.im_wait_activity import model_wait_activity

    writer = BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', delivery_channel='feishu')
    state = _state(writer)
    state.params.effective_on_chunk = writer.write_model
    observer = model_wait_activity(state)
    assert observer is not None
    observer.guarded_sink({**observer.event_fields, 'elapsed_seconds': 1.0}, still_current=observer.is_current)
    assert _read_public_progress_events(writer.chunk_path, 0)[0][0]['model_call_id'] == state.call_id
    observer.close()
    observer.start()
    assert observer._thread is None


# LLM: 覆盖sink回调阻塞与chunk文件锁占用两类场景，验证外层守卫取消与超时不会被展示心跳阻塞，且拿到文件锁后二次复核still_current。
# 函数用途: 确认展示回调或文件锁阻塞不会阻碍provider取消与guard有界退出。
@pytest.mark.parametrize('blocked_write', [False, True])
@pytest.mark.parametrize('user_stop', [False, True])
def test_guard_cancel_is_not_blocked_by_liveness_sink_or_real_chunk_lock(tmp_path, monkeypatch, blocked_write, user_stop):
    from queue import Queue

    from agent_py_agent.agent.agent_core import tool_model_generation as generation
    from agent_py_agent.agent.backends.errors import ProviderTimeoutError
    from agent_py_agent.agent.common.json_io import locked_json_path
    from agent_py_agent.agent.concurrency.interrupt import (
        register_interrupt_callback,
        set_interrupt,
    )

    entered, release_sink, release_backend, provider_cancelled, guard_done = (threading.Event() for _ in range(5))
    second_sink = []

    # LLM: 置位进入信号并阻塞等待release_sink，模拟外部展示回调卡住。
    # 函数用途: 构造受控阻塞的main_activity_sink回调。
    def blocked_sink(event):
        entered.set()
        assert release_sink.wait(3)

    writer = BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', delivery_channel='feishu',
        main_activity_sink=None if blocked_write else blocked_sink, transcript_sink=second_sink.append)
    state = _state(writer)
    original = writer.write_model_liveness

    # LLM: 在文件锁阻塞模式下先置位entered再调用原write_model_liveness等待chunk锁。
    # 函数用途: 通知测试主线程心跳线程已到达写盘入口。
    def publish(event, **kwargs):
        if blocked_write:
            entered.set()
        original(event, **kwargs)

    writer.write_model_liveness = publish

    # LLM: 注册中断回调并在release_backend上阻塞，验证外层守卫取消能及时触发provider中断回调。
    # 类用途: 模拟支持中断回调的阻塞模型后端。
    class Backend:
        supports_thinking_completion = False
        supports_provider_request_options = False
        stream_enabled = False

        # LLM: 登记中断回调并等待释放信号，退出时复位全局中断标志。
        # 函数用途: 在生成期间观测provider_cancelled触发时机。
        def generate(self, prompt, **kwargs):
            try:
                with register_interrupt_callback(provider_cancelled.set):
                    assert release_backend.wait(3)
                return SimpleNamespace(text='late')
            finally:
                set_interrupt(False)

    state.agent.backend = Backend()
    worker = threading.Thread(target=lambda: _do_backend_generate(state.agent.backend, 'synthetic', state))
    failures = []

    # LLM: 调用_wait_for_generation_result并在30ms超时后捕获中断或超时异常，完成后置位guard_done。
    # 函数用途: 在独立线程运行外层等待守卫并记录退出异常类型。
    def guard():
        try:
            generation._wait_for_generation_result(SimpleNamespace(agent=state.agent), state, Queue(), worker, .03)
        except (InterruptedError, ProviderTimeoutError) as exc:
            failures.append(type(exc))
        finally:
            guard_done.set()

    monkeypatch.setattr(generation, '_MODEL_INTERRUPT_DRAIN_SECONDS', .02)
    monkeypatch.setattr(generation, 'is_interrupted', lambda: user_stop)
    lock = locked_json_path(writer.chunk_path) if blocked_write else None
    if lock is not None:
        lock.__enter__()
    worker.start()
    guard_worker = threading.Thread(target=guard)
    try:
        assert entered.wait(2)
        guard_worker.start()
        assert provider_cancelled.wait(.3), '展示回调挡住了真实 provider 取消'
        assert guard_done.wait(.4), 'guard 等待展示线程导致无界阻塞'
        writer.close()
    finally:
        release_sink.set()
        if lock is not None:
            lock.__exit__(None, None, None)
        release_backend.set()
        worker.join(2)
        if guard_worker.ident is not None:
            guard_worker.join(2)
    assert failures == [InterruptedError if user_stop else ProviderTimeoutError]
    assert not second_sink
    if blocked_write:
        assert not writer.chunk_path.exists()  # 真锁之后、真实 append 前复核已关闭。
    else:
        assert len(writer.chunk_path.read_text().splitlines()) == 1  # 已经写出的观察不可撤销。


# LLM: 验证普通append_private_text在不传when_allowed谓词时保持原有追加写盘与锁语义。
# 函数用途: 确认基础私有文本追加接口向后兼容。
def test_plain_private_append_keeps_existing_format_and_lock_semantics(tmp_path):
    from agent_py_agent.agent.common.json_io import append_private_text

    path = tmp_path / 'ordinary.jsonl'
    append_private_text(path, 'one\n')
    append_private_text(path, 'two\n')
    assert path.read_bytes() == b'one\ntwo\n'
