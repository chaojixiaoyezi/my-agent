# LLM: 只使用真实TuiRuntime/typed事件/renderer构造确定性离线帧；不启动CLI、模型、网络或服务，不解析正文判任务状态。
# 模块用途: 证明活动回合在工具与模型展示边界间仍应有可见活动提示，并保留终态、排队与授权/压缩替代显示。
from dataclasses import replace

import pytest

from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext, render_tui_snapshot
from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary
from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore


def frame(snapshot):
    return render_tui_snapshot(snapshot, TuiRenderContext(width=100, now=1000.0, spinner_index=1))


def spinner_present(rendered):
    return any('tui-spinner' in style for line in rendered.transcript_lines for style, _ in line)


def tool_pair(turn):
    turn.on_gateway_event({'kind': 'tool_progress', 'progress': {
        'phase': 'started', 'round': 1, 'call_index': 0, 'tool': 'fixture_tool'}})
    turn.on_gateway_event({'kind': 'tool_progress', 'progress': {
        'phase': 'finished', 'round': 1, 'call_index': 0, 'tool': 'fixture_tool', 'ok': True}})


def test_running_after_gateway_tool_completion_keeps_activity_visible():
    runtime = TuiRuntime('after-tool')
    turn = runtime.begin_turn('request')
    tool_pair(turn)
    snapshot = runtime.store.snapshot()
    assert snapshot.status.phase == 'running'
    assert snapshot.has_active_work
    assert snapshot.active_blocks == ()
    assert runtime.needs_periodic_refresh()
    assert spinner_present(frame(snapshot))


def test_local_private_raw_accounting_keeps_activity_visible():
    runtime = TuiRuntime('private-accounting')
    turn = runtime.begin_turn('request')
    turn.write_model('synthetic uncommitted bytes')
    snapshot = runtime.store.snapshot()
    assert snapshot.status.phase == 'running'
    assert snapshot.has_active_work
    assert snapshot.status.output_tokens > 0
    assert snapshot.active_blocks == ()
    assert spinner_present(frame(snapshot))


def test_gateway_thinking_completed_before_sealed_response_keeps_activity_visible():
    runtime = TuiRuntime('thinking-terminal')
    turn = runtime.begin_turn('request')
    assert turn.on_gateway_event({'kind': 'assistant_thinking', 'text': 'synthetic reasoning'})
    snapshot = runtime.store.snapshot()
    assert snapshot.status.phase == 'running'
    assert snapshot.has_active_work
    assert snapshot.active_blocks == ()
    assert spinner_present(frame(snapshot))


def test_initial_turn_has_activity_indicator():
    runtime = TuiRuntime('start')
    runtime.begin_turn('request')
    assert runtime.store.snapshot().has_active_work
    assert spinner_present(frame(runtime.store.snapshot()))


def test_actual_turn_terminal_stops_activity():
    runtime = TuiRuntime('terminal')
    turn = runtime.begin_turn('request')
    tool_pair(turn)
    runtime.complete_turn('request', TuiTurnSummary(response_text='synthetic final'))
    snapshot = runtime.store.snapshot()
    assert snapshot.status.phase == 'idle'
    assert not snapshot.has_active_work
    assert not spinner_present(frame(snapshot))


def test_queue_only_does_not_claim_running_work():
    runtime = TuiRuntime('queued')
    runtime.enqueue_prompt('request', 'synthetic prompt', queued=True)
    snapshot = runtime.store.snapshot()
    assert snapshot.queued_inputs
    assert snapshot.status.phase == 'idle'
    assert not snapshot.has_active_work
    assert not spinner_present(frame(snapshot))


def test_waiting_permission_has_typed_overlay_instead_of_generic_spinner():
    store = TuiStateStore()
    seq = TuiEventSequencer('permission', clock=lambda: 100.0)
    for event in [seq.emit('turn_started', 'started', 'turn'),
                  seq.emit('thinking_started', 'started', 'thinking'),
                  seq.emit('tool_started', 'started', 'tool', {'tool': 'fixture_tool'}),
                  seq.emit('permission_requested', 'waiting_permission', 'tool', {
                      'permission_id': 'fixture-permission', 'title': 'synthetic permission',
                      'options': [{'id': 'allow', 'label': 'Yes', 'decision': 'approved', 'feedback_type': 'accept'},
                                  {'id': 'deny', 'label': 'No', 'decision': 'denied', 'feedback_type': 'reject'}]})]:
        store.publish(event)
    snapshot = store.snapshot()
    assert snapshot.permission is not None
    rendered = frame(snapshot)
    assert rendered.overlay_lines
    assert not spinner_present(rendered)


def test_compaction_replaces_generic_thinking_with_explicit_progress():
    store = TuiStateStore()
    seq = TuiEventSequencer('compact', clock=lambda: 100.0)
    store.publish(seq.emit('turn_started', 'started', 'turn'))
    store.publish(seq.emit('thinking_started', 'started', 'thinking'))
    store.publish(seq.emit('conversation_compaction_started', 'started', 'compact', {
        'generation': 2, 'source_kind': 'conversation_transcript', 'commit_authority': 'conversation_thread',
        'percent': 5, 'stage': 'preparing', 'before_tokens': 1000}))
    snapshot = store.snapshot()
    assert snapshot.has_active_work
    assert any(block.role == 'compact' for block in snapshot.active_blocks)
    assert any('正在压缩会话上下文' in ''.join(text for _, text in line) for line in frame(snapshot).transcript_lines)


def test_nonempty_active_thinking_keeps_animating_without_new_events():
    from agent_py_agent.cli.chat_parts.tui_view import TuiFrameProvider
    runtime = TuiRuntime('nonempty-thinking')
    turn = runtime.begin_turn('request')
    turn.write_thinking_delta('synthetic reasoning')
    index = [0]
    provider = TuiFrameProvider(runtime.store, lambda width: TuiRenderContext(
        width=width, now=1000.0 + index[0] / 4, spinner_index=index[0]))
    before = provider.frame(100)
    index[0] = 1
    after = provider.frame(100)
    assert runtime.store.snapshot().has_active_work
    before_glyphs = [(style, text) for line in before.transcript_lines for style, text in line if 'tui-spinner-highlight' in style]
    after_glyphs = [(style, text) for line in after.transcript_lines for style, text in line if 'tui-spinner-highlight' in style]
    assert before_glyphs
    assert after_glyphs != before_glyphs


@pytest.mark.parametrize('phase', ['idle', 'failed', 'interrupted', 'blocked', 'waiting_input', 'queued'])
def test_nonrunning_typed_phase_never_synthesizes_activity(phase):
    runtime = TuiRuntime('nonrunning')
    snapshot = runtime.store.snapshot()
    snapshot = replace(snapshot, status=replace(snapshot.status, phase=phase))
    assert not spinner_present(frame(snapshot))


@pytest.mark.parametrize('width', [1, 2, 4, 8, 20, 80])
def test_foreground_gap_wraps_inside_terminal_width_without_ansi(width):
    from agent_py_agent.cli.chat_parts.tui_markdown import display_width_fragments
    runtime = TuiRuntime('narrow')
    turn = runtime.begin_turn('request')
    turn.write_model('synthetic uncommitted bytes')
    before = runtime.store.snapshot()
    rendered = render_tui_snapshot(before, TuiRenderContext(width=width, spinner_index=1))
    assert spinner_present(rendered)
    for line in rendered.transcript_lines:
        assert display_width_fragments(line) <= width
        assert not any('\x1b' in text for _, text in line)
    assert runtime.store.snapshot() == before


@pytest.mark.parametrize('terminal', ['completed', 'failed', 'interrupted'])
def test_running_gap_real_terminal_removes_indicator(terminal):
    runtime = TuiRuntime('terminal-transition')
    turn = runtime.begin_turn('request')
    tool_pair(turn)
    assert spinner_present(frame(runtime.store.snapshot()))
    runtime.complete_turn('request', TuiTurnSummary(
        ok=terminal == 'completed', interrupted=terminal == 'interrupted'))
    assert not spinner_present(frame(runtime.store.snapshot()))


def test_tool_and_next_thinking_share_one_activity_line():
    runtime = TuiRuntime('tool-transition')
    turn = runtime.begin_turn('request')
    turn.write_progress({'phase': 'started', 'round': 1, 'call_index': 0, 'tool': 'fixture_tool'})
    assert sum(any('tui-spinner' in style for style, _ in line) for line in frame(runtime.store.snapshot()).transcript_lines) == 1
    turn.write_thinking_delta('synthetic reasoning')
    assert sum(any('tui-spinner' in style for style, _ in line) for line in frame(runtime.store.snapshot()).transcript_lines) == 1


def test_stable_assistant_does_not_end_running_turn_or_hide_activity():
    store = TuiStateStore()
    seq = TuiEventSequencer('stable-is-not-terminal', clock=lambda: 100.0)
    store.publish(seq.emit('turn_started', 'started', 'turn'))
    store.publish(seq.emit('assistant_completed', 'completed', 'assistant', {'text': 'synthetic committed text'}))
    assert store.snapshot().has_active_work
    assert spinner_present(frame(store.snapshot()))
    store.publish(seq.emit('turn_completed', 'completed', 'turn'))
    assert not spinner_present(frame(store.snapshot()))


def test_visible_assistant_keeps_original_no_generic_spinner_rule():
    runtime = TuiRuntime('visible-assistant')
    turn = runtime.begin_turn('request')
    tool_pair(turn)
    assert turn.on_gateway_event({'kind': 'assistant_final', 'text': 'synthetic committed text', 'model_source': {}})
    assert not spinner_present(frame(runtime.store.snapshot()))


def test_background_activity_suppresses_duplicate_foreground_row():
    runtime = TuiRuntime('background')
    turn = runtime.begin_turn('request')
    tool_pair(turn)
    runtime.update_background_activity(1, {})
    assert any(block.role == 'background' for block in runtime.store.snapshot().active_blocks)
    assert not any('处理中' in ''.join(text for _, text in line) for line in frame(runtime.store.snapshot()).transcript_lines)


def test_gap_provider_animates_but_terminal_provider_reuses_frame():
    from agent_py_agent.cli.chat_parts.tui_view import TuiFrameProvider
    runtime = TuiRuntime('provider-transition')
    turn = runtime.begin_turn('request')
    tool_pair(turn)
    index = [0]
    provider = TuiFrameProvider(runtime.store, lambda width: TuiRenderContext(
        width=width, now=1000.0 + index[0] / 4, spinner_index=index[0]))
    first = provider.frame(80)
    index[0] += 1
    second = provider.frame(80)
    assert first.transcript_lines != second.transcript_lines
    runtime.complete_turn('request', TuiTurnSummary())
    terminal = provider.frame(80)
    index[0] += 1
    assert provider.frame(80) is terminal


def test_interrupt_intent_keeps_activity_until_real_terminal():
    runtime = TuiRuntime('interrupting')
    turn = runtime.begin_turn('request')
    tool_pair(turn)
    turn.request_interrupt()
    assert runtime.store.snapshot().status.phase == 'interrupting'
    rendered = frame(runtime.store.snapshot())
    assert spinner_present(rendered)
    assert any('正在中断' in ''.join(text for _, text in line) for line in rendered.transcript_lines)
    runtime.complete_turn('request', TuiTurnSummary(interrupted=True))
    assert not spinner_present(frame(runtime.store.snapshot()))


def test_one_column_one_row_control_keeps_gap_glyph_visible():
    from agent_py_agent.cli.chat_parts.tui_view import TuiFrameProvider, TuiTranscriptControl
    runtime = TuiRuntime('minimum-terminal')
    turn = runtime.begin_turn('request')
    turn.write_model('synthetic uncommitted bytes')
    control = TuiTranscriptControl(TuiFrameProvider(runtime.store, lambda width: TuiRenderContext(width=width)))
    content = control.create_content(1, 1)
    assert content.line_count == 1
    assert content.cursor_position.y == 0
    assert control.preferred_height(1, 1, False, None) == 1
    assert any('tui-spinner' in fragment[0] for fragment in content.get_line(0))
