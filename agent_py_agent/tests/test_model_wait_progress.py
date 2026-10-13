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


@pytest.mark.parametrize('raises', [False, True])
def test_actual_model_generation_thinking_without_visible_output_refreshes_and_stops(tmp_path, raises):
    writer = BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', delivery_channel='feishu')
    writer.set_verbose_level('stream', timezone='Asia/Shanghai')

    class Backend:
        supports_thinking_completion = True
        supports_provider_request_options = False

        def generate(self, prompt, **kwargs):
            observer = kwargs.get('on_thinking_delta')
            until = time.monotonic() + 1.15
            while time.monotonic() < until:
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


@pytest.mark.parametrize('rich,channel,interval', [(True, 'feishu', 1), (False, 'chat', 1), (False, 'feishu', 0)])
def test_disabled_or_local_model_wait_does_not_publish(tmp_path, rich, channel, interval):
    writer = BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', rich_transcript=rich, delivery_channel=channel)

    class Backend:
        supports_thinking_completion = False
        supports_provider_request_options = False

        def generate(self, prompt, **kwargs):
            return SimpleNamespace(text='final')

    _do_backend_generate(Backend(), 'synthetic', _state(writer, interval=interval))
    assert not writer.chunk_path.exists()


def test_abandon_stops_observer_while_backend_worker_has_not_returned(tmp_path):
    writer = BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', delivery_channel='feishu')
    state, release, entered = _state(writer), threading.Event(), threading.Event()

    class Backend:
        supports_thinking_completion = False
        supports_provider_request_options = False

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


@pytest.mark.parametrize('failed', [False, True])
def test_outer_wall_guard_closes_observer_on_every_exit(tmp_path, monkeypatch, failed):
    from agent_py_agent.agent.agent_core import tool_model_generation as generation
    from agent_py_agent.agent.agent_core.model.im_wait_activity import model_wait_activity

    writer = BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', delivery_channel='feishu')
    state = _state(writer)
    observer = model_wait_activity(state)

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


@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), float('-inf')])
def test_nonfinite_interval_never_becomes_one_second(tmp_path, invalid):
    from agent_py_agent.agent.agent_core.model.im_wait_activity import model_wait_activity

    state = _state(BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', delivery_channel='feishu'), interval=invalid)
    assert model_wait_activity(state).interval_seconds == 120


@pytest.mark.parametrize('call_id', ['', None, 0, '   '])
def test_pending_missing_model_identity_does_not_publish(tmp_path, call_id):
    from agent_py_agent.agent.agent_core.model.im_wait_activity import model_wait_activity

    state = _state(BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', delivery_channel='feishu'))
    state.call_id = call_id
    assert model_wait_activity(state) is None


def test_cancelled_before_start_or_closed_observer_cannot_restart(tmp_path):
    from agent_py_agent.agent.agent_core.model.im_wait_activity import model_wait_activity

    state = _state(BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', delivery_channel='feishu'))
    observer = model_wait_activity(state)
    state.params.cancellation_token.cancel()
    observer.start()
    assert observer._thread is None


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

    def blocked_sink(event):
        entered.set()
        assert release_sink.wait(3)

    writer = BufferedChunkStreamWriter(tmp_path / 'chunks.jsonl', delivery_channel='feishu',
        main_activity_sink=None if blocked_write else blocked_sink, transcript_sink=second_sink.append)
    state = _state(writer)
    original = writer.write_model_liveness

    def publish(event, **kwargs):
        if blocked_write:
            entered.set()
        original(event, **kwargs)

    writer.write_model_liveness = publish

    class Backend:
        supports_thinking_completion = False
        supports_provider_request_options = False
        stream_enabled = False

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


def test_plain_private_append_keeps_existing_format_and_lock_semantics(tmp_path):
    from agent_py_agent.agent.common.json_io import append_private_text

    path = tmp_path / 'ordinary.jsonl'
    append_private_text(path, 'one\n')
    append_private_text(path, 'two\n')
    assert path.read_bytes() == b'one\ntwo\n'
