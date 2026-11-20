import random

from agent_py_agent.agent.gateway_parts import progress_text_delta
from agent_py_agent.agent.gateway_parts.progress_text_delta import TextDeltaBuffer
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.agent.tooling import host_output_redaction


def test_explicit_turn_handle_retains_cross_segment_writer_until_close(tmp_path):
    handle = host_output_redaction.TurnHoldingHandle('owner-m61', 'thread-m61', 'turn-m61')
    turn = handle.redaction
    value = 'synthetic value for writer lifetime 561'
    first = BufferedChunkStreamWriter(tmp_path / 'first.jsonl')
    second = BufferedChunkStreamWriter(tmp_path / 'second.jsonl')
    first.bind_credential_redaction(turn, holding_handle=handle)
    second.bind_credential_redaction(turn, holding_handle=handle)
    try:
        turn.register_values((value,))
        handle.release()
        first.close()
        assert turn.active
        assert value not in second._project_stream_text(value)
        second.close()
        assert not turn.active
        second._write_event({'kind': 'assistant_commentary', 'text': value})
        assert not second.chunk_path.exists()
    finally:
        first.close()
        second.close()
        handle.release()


def test_retention_projection_work_is_linear_for_one_mib(monkeypatch):
    total = 1 << 20
    work, scan_work = 0, 0
    original_cut = progress_text_delta._safe_cut

    def count_scan(raw, limit, identifiers, **options):
        nonlocal scan_work
        scan_work += len(raw)
        assert scan_work <= 6 * total
        return original_cut(raw, limit, identifiers, **options)

    monkeypatch.setattr(progress_text_delta, '_safe_cut', count_scan)

    def project(text):
        nonlocal work
        work += len(text)
        assert work <= 6 * total
        return text.strip()

    buffer = TextDeltaBuffer(project=project, max_projection_chars=1 << 32)
    chunk = 'ordinary output for the current turn. ' * 8
    count, tail = divmod(total, len(chunk))
    buffer.begin('call')
    deltas = []
    for piece in [chunk] * count + [chunk[:tail]]:
        deltas.extend(buffer.append(piece, model_call_id='call'))
    deltas.extend(buffer.finish('call'))
    assert not any(event['kind'] == 'assistant_text_reset' for event in deltas)
    assert ''.join(event['text'] for event in deltas if event['kind'] == 'assistant_text_delta') == (
        chunk * count + chunk[:tail]).strip()


def test_split_protocol_projection_matches_complete_output(tmp_path):
    writer = BufferedChunkStreamWriter(tmp_path / 'stream.jsonl', delivery_channel='feishu')
    prose = 'Plain context before and after each rendered block. ' * 12
    text = (prose + '\n[TOOL_CALL]\nhidden report payload\n[/TOOL_CALL]\n' + prose
            + '<function name="read_file">' + 'internal body ' * 37 + '</function>' + prose)
    expected = writer._project_stream_text(text)
    for seed in range(5):
        rng = random.Random(seed)
        buffer = TextDeltaBuffer(project=writer._project_stream_text)
        buffer.begin('call')
        rendered = ''
        cursor = 0
        while cursor < len(text):
            end = cursor + rng.randint(1, 131)
            for event in buffer.append(text[cursor:end], model_call_id='call'):
                assert event['kind'] == 'assistant_text_delta'
                rendered += event['text']
                assert expected.startswith(rendered)
            cursor = end
        for event in buffer.finish('call'):
            assert event['kind'] != 'assistant_text_reset'
            if event['kind'] == 'assistant_text_delta':
                rendered += event['text']
        assert rendered == expected


def test_log_match_index_keeps_shared_value_until_last_handle_releases():
    import logging

    from agent_py_agent.agent.common import log_redaction

    first = host_output_redaction.TurnHoldingHandle('log-owner', 'thread', 'first')
    second = host_output_redaction.TurnHoldingHandle('log-owner', 'thread', 'second')
    shared = 'log sample value referenced by both turns'
    unrelated = 'log sample value with different text'

    def render(value):
        record = logging.LogRecord('turn-test', logging.INFO, '', 0, '%s', (value,), None)
        log_redaction._apply_log_record_redaction(record)
        return log_redaction.RedactingFormatter().format(record)

    try:
        first.redaction.register_values((shared,))
        second.redaction.register_values((shared,))
        assert log_redaction._ACTIVE_TURN_LOG_COUNTS[shared] == 2
        assert render(shared) == '[REDACTED]'
        assert render(unrelated) == unrelated
        first.release()
        assert log_redaction._ACTIVE_TURN_LOG_COUNTS[shared] == 1
        assert render(shared) == '[REDACTED]'
        second.release()
        assert shared not in log_redaction._ACTIVE_TURN_LOG_COUNTS
        assert render(shared) == shared
    finally:
        first.release()
        second.release()


def test_late_filter_discards_data_after_handle_release():
    handle = host_output_redaction.TurnHoldingHandle('owner-late', 'thread', 'turn')
    value = 'synthetic value split between writes'
    handle.redaction.register_values((value,))
    flt = handle.redaction.new_stream_filter()
    assert not flt.feed_text(value[:8])
    handle.release()
    assert flt.feed_text(value[8:], final=True) == ''
