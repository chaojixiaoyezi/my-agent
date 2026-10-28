"""真实writer与公开reader/render只发布sealed call，未提交raw和旧candidate事件均退休。"""
import json

import pytest

from agent_py_agent.agent.adapter.manager import _render_gateway_progress
from agent_py_agent.agent.agent_core.tool_model_generation import _model_chunk_callback
from agent_py_agent.agent.agent_core.tool_stream import ToolBoundaryChunkFilter
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.gateway_parts.http_handlers import _read_public_progress_events
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.cli.chat_parts.gateway_client import _emit_chunk_line
from agent_py_agent.tests.test_cli_run_conversation import _agent, _StaticBackend

_MARKER = 'SYNTHETIC_ONLY_SECRET_REV03'


# LLM: 使用生产chunk文件、/progress读口和IM渲染；只检查本测试的新文件，不读取用户日志。
# 函数用途: 检查每个可公开层面都没有合成raw标记，并返回真实记录供精确顺序断言。
def _public_rows(path):
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    events, _ = _read_public_progress_events(path, 0, channel='feishu') if path.exists() else ([], 0)
    assert _MARKER not in json.dumps(rows)
    assert _MARKER not in json.dumps(events)
    assert all(_MARKER not in _render_gateway_progress(event) for event in events)
    return rows, events


@pytest.mark.parametrize('rich', [False, True])
@pytest.mark.parametrize('verbose', ['off', 'on', 'full', 'stream'])
@pytest.mark.parametrize('boundary', ['close', 'tool', 'permission', 'compact', 'prepare'])
def test_uncommitted_raw_never_flushes_at_any_writer_boundary(tmp_path, rich, verbose, boundary):
    path = tmp_path / 'uncommitted.jsonl'
    writer = BufferedChunkStreamWriter(path, rich_transcript=rich, flush_chars=1, delivery_channel='feishu')
    writer.set_verbose_level(verbose)
    callback = ToolBoundaryChunkFilter(_model_chunk_callback(writer))
    for chunk in ['{"password":"', _MARKER, '\n']:
        callback(chunk)
        writer.flush()
        _public_rows(path)
    if boundary == 'tool':
        writer.write_progress({'tool': 'read_file', 'phase': 'started'}, '工具开始')
    elif boundary == 'permission':
        writer._prepare_permission_request()
    elif boundary == 'compact':
        writer.write_compact_boundary(1)
    elif boundary == 'prepare':
        writer.prepare_display_history()
    writer.close()
    rows, _ = _public_rows(path)
    assert not writer._committed_model_calls
    assert not [row for row in rows if row.get('kind') in {'model_delta', 'model_draft', 'assistant_commentary', 'assistant_final'}]


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('rich', [False, True])
@pytest.mark.parametrize('verbose', ['off', 'on', 'full', 'stream'])
@pytest.mark.parametrize('final_only', [False, True])
def test_real_fake_loop_commits_each_model_response_through_public_readers(tmp_path, stream, rich, verbose, final_only):
    agent = _agent(tmp_path)
    path = tmp_path / 'committed.jsonl'
    writer = BufferedChunkStreamWriter(path, rich_transcript=rich, flush_chars=1, delivery_channel='feishu')
    writer.set_verbose_level(verbose)
    if final_only:
        writer.set_final_only()
    source = agent.home_paths.owner_home_dir / 'evidence.txt'
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('原工具证据', encoding='utf-8')

    # LLM: 原core负责真实read_file和sealed提交，后端只按三次固定响应回放；下一次调用前检查上一call已公开。
    # 类用途: 覆盖stream/nonstream和全部展示档位，不由测试替真实Agent额外插入commit。
    class ReadTwice(_StaticBackend):
        # LLM: raw标记只经真正的on_chunk返回；每帧检查chunk、public reader、IM，绝不当成已确认正文。
        # 函数用途: 回放两次工具前说明与最终回复，并锁定原模型调用和工具计数。
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            rows, events = _public_rows(path)
            expected = [] if final_only and not rich else ['第1次确认。']
            if self.calls > 1:
                if rich or verbose == 'stream':
                    expected = [] if final_only and not rich else [f'第{i}次确认。' for i in range(1, self.calls)]
                actual = [row['text'] for row in rows if row.get('kind') == 'assistant_commentary']
                assert actual == expected
                if not rich:
                    assert [event['text'] for event in events if event.get('kind') == 'assistant_commentary'] == expected
            if stream:
                for chunk in ['{"password":"', _MARKER, '\n']:
                    on_chunk(chunk)
                    _public_rows(path)
            if self.calls < 3:
                return ModelResponse(f'第{self.calls}次确认。', self.name, tool_use_blocks=[
                    {'id': f'actual-read-{self.calls}', 'name': 'read_file', 'input': {'path': str(source)}}])
            return ModelResponse('最终确认完成。', self.name)

    agent.backend = ReadTwice()
    result = agent.run('核对当前证据', on_chunk=writer, allowed_tools=['read_file'], save=False)
    writer.close()
    rows, _ = _public_rows(path)
    assert agent.backend.calls == 3 and result.tool_rounds == 2 and result.response == '最终确认完成。'
    assert len(writer._committed_model_calls) == 3
    assert writer.assistant_commentary_messages() == ('第1次确认。', '第2次确认。')
    assert not any(row.get('kind') in {'model_delta', 'model_draft'} for row in rows)
    if rich:
        assert [row['text'] for row in rows if row.get('kind') == 'assistant_final'] == ['最终确认完成。']
        assert any(row.get('kind') == 'model_metrics_updated' for row in rows)


@pytest.mark.parametrize('kind', ['model_delta', 'model_draft'])
def test_old_raw_chunk_is_consumed_without_reaching_any_typed_callback(tmp_path, kind):
    row = {'kind': kind, 'text': _MARKER}
    path = tmp_path / 'old-private.jsonl'
    path.write_text(json.dumps(row) + '\n')
    typed, plain = [], []
    assert _emit_chunk_line(path.read_text(), plain.append, lambda value: typed.append(value) or True) == (1, 0)
    assert typed == plain == []
    events, cursor = _read_public_progress_events(path, 0)
    assert events == [] and cursor == 1
    assert _render_gateway_progress(row) == ''
    assert json.loads(path.read_text()) == row


@pytest.mark.parametrize('outcome', ['cancelled', 'failed'])
@pytest.mark.parametrize('rich', [False, True])
@pytest.mark.parametrize('verbose', ['off', 'stream'])
def test_real_fake_failure_close_does_not_commit_or_publish_raw(tmp_path, outcome, rich, verbose):
    from agent_py_agent.agent.concurrency.interrupt import interrupt_by_name, register_interruptible

    agent = _agent(tmp_path)
    path = tmp_path / 'failed.jsonl'
    writer = BufferedChunkStreamWriter(path, rich_transcript=rich, flush_chars=1, delivery_channel='feishu')
    writer.set_verbose_level(verbose)

    # LLM: 不抛provider异常触重试，使用原取消token或结构化错误；只让原core决定失败收口。
    # 类用途: 验证原finally close在失败/取消时也不能把未提交正文释放。
    class Failure(_StaticBackend):
        # LLM: raw仅交原回调，最终response状态与cancel均为原结构化接口；不伪造sealed来源。
        # 函数用途: 单次返回失败或取消，确保零工具零committed正文。
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            for chunk in ['https://user:', _MARKER]:
                on_chunk(chunk)
                _public_rows(path)
            if outcome == 'cancelled':
                assert interrupt_by_name('rev03-cancelled-model')
                return ModelResponse('不应公开。', self.name)
            return ModelResponse('不应公开。', self.name, runtime_status='error', runtime_reason='SYNTHETIC_ERROR')

    agent.backend = Failure()
    from agent_py_agent.agent.concurrency.interrupt import set_interrupt
    try:
        with register_interruptible('rev03-cancelled-model'):
            if outcome == 'cancelled':
                with pytest.raises(InterruptedError):
                    agent.run('核对失败收口', on_chunk=writer, save=False)
            else:
                result = agent.run('核对失败收口', on_chunk=writer, save=False)
                assert result.tool_rounds == 0 and result.runtime_status != 'ok'
    finally:
        set_interrupt(False)
    writer.close()
    rows, _ = _public_rows(path)
    assert agent.backend.calls == 1 and not writer._committed_model_calls
    assert not [row for row in rows if row.get('kind') in {'model_delta', 'model_draft', 'assistant_commentary', 'assistant_final'}]


def test_foreground_old_raw_event_cannot_enter_shared_history(tmp_path):
    from agent_py_agent.agent.conversation.background_transcript import (
        read_background_transcript_events,
    )
    from agent_py_agent.tests.test_gateway_foreground_transcript import _foreground

    agent, writer, _store, thread, _request = _foreground(tmp_path)
    writer.transcript_sink({'kind': 'model_delta', 'text': _MARKER})
    events = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)['events']
    assert _MARKER not in json.dumps(events)
    assert not [row for row in events if row.get('payload', {}).get('text')]


@pytest.mark.parametrize('kind', ['future_event', [], {}])
def test_retirement_keeps_unknown_future_kind_callback_semantics(kind):
    events = []
    row = {'kind': kind, 'text': '未来合法字段。'}
    assert _emit_chunk_line(json.dumps(row), lambda _: False, lambda item: events.append(item) or True) == (1, 0)
    assert events == [row]


def test_old_private_raw_rows_do_not_hide_committed_tool_or_liveness_events(tmp_path):
    path = tmp_path / 'mixed-history.jsonl'
    rows = [
        {'kind': 'assistant_commentary', 'text': '已确认正文。'},
        {'kind': 'model_draft', 'text': _MARKER},
        {'kind': 'model_delta', 'text': _MARKER},
        {'kind': 'model_liveness', 'progress': {'model_call_id': 'call-old', 'wait_state': 'model_response', 'elapsed_seconds': 90}},
        {'kind': 'tool_progress', 'heartbeat': True, 'progress': {'tool': 'read_file', 'wait_state': 'handler_result', 'elapsed_seconds': 120}},
    ]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    events, cursor = _read_public_progress_events(path, 0)
    assert cursor == len(rows)
    assert [row.get('kind') for row in events] == ['assistant_commentary', 'model_liveness', 'tool_liveness']
    rendered = [_render_gateway_progress(row) for row in events]
    assert rendered[0] == '已确认正文。' and '90.00' in rendered[1] and '120.00' in rendered[2]
    assert _MARKER not in ''.join(rendered)
    assert [json.loads(line) for line in path.read_text().splitlines()] == rows
