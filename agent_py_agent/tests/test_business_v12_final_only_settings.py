# 模块用途: ROUTE04-OBS-001（MSG-023 B）回归：非 rich writer 的 process_settings 延到本 writer 首个实际发布事件之前，
#   纯 final-only 短受理轮与零事件 writer 不落任何 chunk 文件；设置仍先于首个普通可见事件，首发之后的档位变化即时写出，
#   rich 模式与公开进度投影不变。修复前零事件与 final-only 轮各留一条只有 process_settings 的 chunk（见 evidence/
#   route04-obs-001-triage.txt）。只用 tmp 路径与纯 writer，不是线上或真实 IM 证据。
from __future__ import annotations

import json
import threading

import pytest

from agent_py_agent.agent.gateway_parts.http_handlers import _read_public_progress_events
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.tests.test_committed_model_messages import _commit_writer_fixture

_PROGRESS = {'tool': 'read_file', 'status': '完成'}


def _levels(writer):
    rows = [json.loads(line) for line in writer.chunk_path.read_text(encoding='utf-8').splitlines()]
    return [(row['kind'], row.get('verbose_level')) for row in rows], rows


@pytest.mark.parametrize('level', ['off', 'on', 'full', 'stream'])
def test_zero_event_writer_writes_no_chunk(tmp_path, level):
    writer = BufferedChunkStreamWriter(tmp_path / 'zero.jsonl')
    writer.set_verbose_level(level)
    writer.flush()
    writer.close()
    assert not writer.chunk_path.exists()


def test_final_only_ack_round_after_verbose_writes_nothing(tmp_path):
    writer = BufferedChunkStreamWriter(tmp_path / 'ack.jsonl')
    writer.set_verbose_level('stream', preview_max_chars=200)
    writer.set_final_only()
    writer.write_model('已受理')
    _commit_writer_fixture(writer, '已受理', call_id='final-only-settings:0')
    writer.write('模型等待过程')
    writer.flush()
    writer.write_progress(dict(_PROGRESS), 'legacy')
    writer.close()
    assert not writer.chunk_path.exists()


def test_final_only_round_orders_settings_before_a_published_event(tmp_path):
    writer = BufferedChunkStreamWriter(tmp_path / 'resumed.jsonl')
    writer.set_verbose_level('on')
    writer.set_final_only()
    _commit_writer_fixture(writer, '已受理', call_id='final-only-settings:1')
    writer.write_turn_resumed('gateway_safe_restart')
    writer.close()
    assert _levels(writer)[0] == [('process_settings', 'on'), ('turn_resumed', None)]


def test_settings_precede_first_visible_event_and_public_projection_is_unchanged(tmp_path):
    writer = BufferedChunkStreamWriter(tmp_path / 'chat.jsonl')
    writer.set_verbose_level('on')
    assert not writer.chunk_path.exists()
    writer.write_progress(dict(_PROGRESS), 'legacy')
    writer.close()
    assert _levels(writer)[0] == [('process_settings', 'on'), ('tool_progress', 'on')]
    events, cursor = _read_public_progress_events(writer.chunk_path, 0)
    assert cursor == 2 and events[0] == {'kind': 'process_settings', 'verbose_level': 'on'}
    assert events[1]['tool'] == 'read_file'


def test_stream_settings_keep_preview_limit_before_committed_commentary(tmp_path):
    writer = BufferedChunkStreamWriter(tmp_path / 'stream.jsonl')
    writer.set_verbose_level('stream', preview_max_chars=8000)
    _commit_writer_fixture(writer, '普通后续聊天', call_id='final-only-settings:2')
    writer.close()
    levels, rows = _levels(writer)
    assert levels == [('process_settings', 'stream'), ('assistant_commentary', None)]
    assert rows[0]['preview_max_chars'] == rows[1]['preview_max_chars'] == 8000
    assert rows[1]['text'] == '普通后续聊天'


def test_later_verbose_changes_are_written_immediately(tmp_path):
    writer = BufferedChunkStreamWriter(tmp_path / 'later.jsonl')
    writer.set_verbose_level('on')
    writer.write_progress(dict(_PROGRESS), 'legacy')
    writer.set_verbose_level('full')
    assert _levels(writer)[0] == [('process_settings', 'on'), ('tool_progress', 'on'), ('process_settings', 'full')]
    writer.set_verbose_level('off')
    writer.close()
    assert _levels(writer)[0][-1] == ('process_settings', 'off')


def test_setting_superseded_before_any_event_is_written_once_with_the_level_in_force(tmp_path):
    writer = BufferedChunkStreamWriter(tmp_path / 'superseded.jsonl')
    writer.set_verbose_level('stream')
    writer.set_verbose_level('on')
    writer.write_progress(dict(_PROGRESS), 'legacy')
    writer.close()
    levels, rows = _levels(writer)
    assert levels == [('process_settings', 'on'), ('tool_progress', 'on')]
    assert 'preview_max_chars' not in rows[0]


def test_rich_transcript_never_writes_settings_and_keeps_final_only_events(tmp_path):
    rich = BufferedChunkStreamWriter(tmp_path / 'rich.jsonl', rich_transcript=True)
    rich.set_verbose_level('stream')
    rich.close()
    assert not rich.chunk_path.exists()
    rich = BufferedChunkStreamWriter(tmp_path / 'rich-final.jsonl', rich_transcript=True)
    rich.set_verbose_level('stream')
    rich.set_final_only()
    _commit_writer_fixture(rich, '富客户端内部过程\n', call_id='final-only-settings:3')
    rich.close()
    kinds = [kind for kind, _ in _levels(rich)[0]]
    assert 'assistant_commentary' in kinds and 'process_settings' not in kinds


def test_concurrent_first_events_get_exactly_one_settings_row_first(tmp_path):
    for attempt in range(12):
        writer = BufferedChunkStreamWriter(tmp_path / f'race-{attempt}.jsonl')
        writer.set_verbose_level('on')
        barrier = threading.Barrier(4)

        def publish(index, writer=writer, barrier=barrier):
            barrier.wait()
            writer.write_progress({'tool': f'tool-{index}', 'status': '完成'}, 'legacy')

        threads = [threading.Thread(target=publish, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        writer.close()
        kinds = [kind for kind, _ in _levels(writer)[0]]
        assert kinds[0] == 'process_settings' and kinds.count('process_settings') == 1
        assert kinds.count('tool_progress') == 4
