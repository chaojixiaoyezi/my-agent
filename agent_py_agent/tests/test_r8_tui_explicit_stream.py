"""真实Gateway传输的公开候选只进入TUI活动块，终稿沿原唯一边界。"""
import json
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.paths import gateway_chunk_path
from agent_py_agent.agent.gateway_parts.queue_service import archive_request
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.cli.chat_parts.gateway_client import (
    GatewayChunkPollRequest,
    NetworkGatewayObserver,
)
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary
from agent_py_agent.tests.test_s2w2_tui_network_mode import live_gateway  # noqa: F401


def _visible(runtime):
    snapshot = runtime.store.snapshot()
    return ''.join(block.text for block in (*snapshot.stable_blocks, *snapshot.active_blocks)
                   if block.role == 'assistant')


@pytest.mark.parametrize("sensitive", [False, True])
@pytest.mark.parametrize("reconnect", [False, True])
@pytest.mark.parametrize("level", ["stream", "off"])
def test_real_network_public_candidate_before_canonical_final(live_gateway, monkeypatch, reconnect, level, sensitive):  # noqa: F811
    paths = live_gateway['paths']
    runtime = TuiRuntime('wire-stream')
    turn = runtime.begin_turn('r')
    from agent_py_agent.agent.gateway_parts.stream_events import public_model_text

    secret = 'SyntheticWireCredentialOnly'
    raw = '公开候选正文。' * 100
    if sensitive:
        raw = raw[:90] + '{"password":"' + secret + '"}' + raw[90:]
    text = public_model_text(raw, max_chars=0, delivery_channel='', identifiers=())
    writer = BufferedChunkStreamWriter(gateway_chunk_path(paths, 'r'),
        text_delta_holdback_chars=8, rich_transcript=True)
    writer.set_verbose_level(level)
    write = writer.bind_model_call('call-1')
    write('即将撤回的旧候选。' * 40)
    writer.reset_model_text('retry', model_call_id='call-1')
    for offset in range(0, len(raw), 32):
        write(raw[offset:offset + 32])
    writer.commit_model_response({'schema': 'assistant_messages.v1',
        'source': {'call_id': 'call-1'}, 'boundary': 'terminal',
        'messages': [{'text': raw}], 'final_text': raw})
    writer.flush()
    record = paths.processing / 'r.json'
    record.write_text(json.dumps({'id': 'r', 'execution_attempt_id': 'a', 'user_id': 'alice', 'channel': 'chat'}))
    terminal = {'ok': True, 'response': text, 'id': 'r', 'request_id': 'r',
                'user_id': 'alice', 'channel': 'chat', 'execution_attempt_id': 'a'}
    archive_request(paths, record, paths.done, 'r', terminal_response=terminal)
    live_gateway['start']()
    agent = SimpleNamespace(config=SimpleNamespace(gateway_port=live_gateway['port'],
        gateway_request_timeout=10, gateway_auth_token=''))
    import agent_py_agent.agent.common.authenticated_http as transport
    actual_open = transport.open_authenticated_request
    opened = []
    class Disconnect:
        def __init__(self, raw):
            self.raw, self.frames = raw, 0
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.raw.close()
        def readline(self, size):
            if self.frames >= 5:
                self.raw.close()
                return b''
            line = self.raw.readline(size)
            if line in (b'\n', b'\r\n'):
                self.frames += 1
            return line
    def opener(request, *, timeout):
        opened.append(request)
        raw = actual_open(request, timeout=timeout)
        return Disconnect(raw) if reconnect and len(opened) == 1 else raw
    monkeypatch.setattr(transport, 'open_authenticated_request', opener)
    seen = []
    def consume(event):
        result = turn.on_gateway_event(event)
        if event.get('kind') == 'assistant_text_delta':
            seen.append(_visible(runtime))
        return result
    observer = NetworkGatewayObserver(agent, f"http://127.0.0.1:{live_gateway['port']}")
    result = observer(GatewayChunkPollRequest(gateway_chunk_path(paths, 'r'), paths.terminal / 'r.json',
        time.time() + 10, lambda _: None, [0], [0], on_event=consume))
    assert result['ok'] is True
    assert all(secret not in frame for frame in seen)
    if level == 'stream':
        assert seen and seen[0], '真实公开delta未进入TUI候选'
        if reconnect:
            assert len(opened) >= 2
            assert opened[1].get_header('Last-event-id')
    else:
        assert not seen
    assert _visible(runtime) == (text if level == 'stream' else '')
    runtime.complete_turn('r', TuiTurnSummary(response_text=text))
    assert _visible(runtime) == text
    assert not runtime.store.snapshot().active_blocks


def _delta(turn, call, seq, text, **extra):
    return turn.on_gateway_event({'kind': 'assistant_text_delta', 'level': 'stream',
        'model_call_id': call, 'seq': seq, 'text': text, **extra})


def test_replay_retry_call_switch_gap_and_final_correction():
    runtime = TuiRuntime('seq')
    turn = runtime.begin_turn('r')
    assert _delta(turn, 'one', 0, '候选')
    assert _delta(turn, 'one', 0, '候选')
    assert _visible(runtime) == '候选'
    assert turn.on_gateway_event({'kind': 'assistant_text_reset', 'model_call_id': 'one', 'reason': 'retry'})
    assert _visible(runtime) == ''
    assert _delta(turn, 'one', 0, '重试')
    assert _visible(runtime) == '重试'
    assert _delta(turn, 'two', 0, '新调用')
    assert _visible(runtime) == '新调用'
    _delta(turn, 'one', 1, '迟到')
    assert _visible(runtime) == '新调用'
    assert not turn.on_gateway_event({'kind': 'assistant_final', 'model_source': {'call_id': 'one'}, 'text': '旧调用迟到提交'})
    _delta(turn, 'two', 2, '欠序')
    assert _visible(runtime) == ''
    # 显示流不完整不影响完整已采纳正文。
    final = {'kind': 'assistant_final', 'model_source': {'call_id': 'two'}, 'text': '权威终稿'}
    assert turn.on_gateway_event(final)
    assert turn.on_gateway_event(final)
    runtime.complete_turn('r', TuiTurnSummary(response_text='权威终稿'))
    assert _visible(runtime) == '权威终稿'
    assert not turn.on_gateway_event(final)


def test_steer_stop_new_next_turn_rejects_late_and_foreign_frames():
    runtime = TuiRuntime('lifecycle')
    turn = runtime.begin_turn('r')
    _delta(turn, 'call', 0, '未采纳')
    turn.begin_active_turn_input(('steer-id',))
    assert _visible(runtime) == ''
    _delta(turn, 'call', 1, '迟到')
    assert _visible(runtime) == ''
    _delta(turn, 'next-call', 0, '待取消')
    runtime.complete_turn('r', TuiTurnSummary(response_text='', interrupted=True))
    assert _visible(runtime) == ''
    assert not _delta(turn, 'next-call', 1, '停止后迟到')
    runtime.publish_conversation_reset('new-thread')
    following = runtime.begin_turn('r2')
    assert not _delta(following, 'foreign', 0, '外线程', thread_id='old-thread')
    assert not _delta(following, 'foreign', 0, '外请求', request_id='r')
    _delta(following, 'fresh', 0, '当前候选', thread_id='new-thread')
    assert _visible(runtime) == '当前候选'
    runtime.complete_turn('r2', TuiTurnSummary(response_text='下一轮终稿'))
    assert _visible(runtime) == '下一轮终稿'
    assert not _delta(turn, 'late', 0, '新建后迟到')


def test_done_does_not_commit_and_committed_commentary_replaces_candidate():
    runtime = TuiRuntime('done')
    turn = runtime.begin_turn('r')
    _delta(turn, 'call', 0, '待校正')
    turn.on_gateway_event({'kind': 'assistant_text_done', 'model_call_id': 'call', 'seq': 0})
    assert not runtime.store.snapshot().stable_blocks
    _delta(turn, 'call', 1, 'done后迟到')
    assert _visible(runtime) == '待校正'
    commentary = {'kind': 'assistant_commentary', 'model_source': {'call_id': 'call'},
                  'text': '完整已采纳说明', 'replace_candidate': True}
    turn.on_gateway_event(commentary)
    turn.on_gateway_event(commentary)
    assert _visible(runtime) == '完整已采纳说明'
    runtime.complete_turn('r', TuiTurnSummary(response_text='终答'))
    assert _visible(runtime) == '完整已采纳说明终答'


def test_ten_tuis_bound_candidate_storage_and_cumulative_work():
    # 每个TUI拥有自己的有界候选；预算耗尽撤回，不能无限复制增长正文。
    for index in range(10):
        runtime = TuiRuntime(f'bounded-{index}')
        turn = runtime.begin_turn('r')
        text = '文' * 16384
        for seq in range(100):
            _delta(turn, 'call', seq, text)
        assert len(turn._assistant_text) <= 1_048_576
        assert not turn._model_candidate_text
        assert turn._public_text_calls['call'] == -1
        assert _visible(runtime) == ''
        runtime.complete_turn('r', TuiTurnSummary(response_text='完成'))
        assert _visible(runtime) == '完成'


def test_public_stream_never_accepts_raw_or_missing_stream_level():
    runtime = TuiRuntime('raw')
    turn = runtime.begin_turn('r')
    turn.on_gateway_event({'kind': 'model_delta', 'text': '未净化秘密'})
    assert not turn.on_gateway_event({'kind': 'assistant_text_delta', 'model_call_id': 'x',
        'seq': 0, 'text': '非stream'})
    assert _visible(runtime) == ''


@pytest.mark.parametrize('reason', ['cancel', 'superseded', 'tool_call'])
def test_reset_retracts_candidate_and_only_tool_boundary_can_commit(reason):
    runtime = TuiRuntime('reset')
    turn = runtime.begin_turn('r')
    _delta(turn, 'call', 0, '撤回')
    turn.on_gateway_event({'kind': 'assistant_text_reset', 'model_call_id': 'call', 'reason': reason})
    assert _visible(runtime) == ''
    event = {'kind': 'assistant_commentary', 'model_source': {'call_id': 'call'}, 'text': '已采纳工具说明'}
    accepted = turn.on_gateway_event(event)
    assert accepted is (reason == 'tool_call')
    assert _visible(runtime) == ('已采纳工具说明' if reason == 'tool_call' else '')


def test_final_adoption_reuses_candidate_block_and_summary_freezes_once():
    runtime = TuiRuntime('same-block')
    turn = runtime.begin_turn('r')
    _delta(turn, 'call', 0, '候选')
    block_id = turn._assistant_block_id
    event = {'kind': 'assistant_final', 'model_source': {'call_id': 'call'}, 'text': '校正终稿'}
    turn.on_gateway_event(event)
    turn.on_gateway_event(event)
    assert turn._assistant_block_id == block_id
    assert _visible(runtime) == '校正终稿'
    assert not runtime.store.snapshot().stable_blocks
    runtime.complete_turn('r', TuiTurnSummary(response_text='校正终稿'))
    blocks = [block for block in runtime.store.snapshot().stable_blocks if block.role == 'assistant']
    assert len(blocks) == 1 and blocks[0].block_id == block_id
