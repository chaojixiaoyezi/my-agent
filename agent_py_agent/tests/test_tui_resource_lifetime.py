import threading
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.cli.chat_parts import tui_safe_lines, tui_view
from agent_py_agent.cli.chat_parts.tui_block_renderer import (
    TuiBlockRenderCache,
    TuiRenderContext,
    TuiRenderFrame,
    render_tui_snapshot,
    sanitize_tui_render_frame,
)
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_view import TuiFrameProvider


def test_frame_key_reuses_history_across_animation_and_stream_updates(monkeypatch):
    runtime = TuiRuntime("natural-history-view")
    for index in range(3):
        runtime._publish("assistant_completed", "completed", f"old-{index}", {"text": f"旧消息 {index}"})
    visits = []
    original = tui_view._block_versions

    def counted(blocks):
        visits.append(len(blocks))
        return original(blocks)

    monkeypatch.setattr(tui_view, "_block_versions", counted)
    context = TuiRenderContext(width=80)
    provider = TuiFrameProvider(runtime.store, lambda _width: context)
    first = provider.frame(80)
    assert visits == [3, 0]
    for index in range(8):
        context = replace(context, notice=f"状态 {index}", spinner_index=index)
        provider.invalidate()
        assert provider.frame(80).transcript_lines == first.transcript_lines
    assert visits == [3, 0]

    runtime._publish("assistant_started", "started", "new")
    runtime._publish("assistant_delta", "delta", "new", {"text": "正在回复"})
    provider.frame(80)
    assert visits == [3, 0, 1], "流式增量仅更新活动组版本键"
    runtime._publish("assistant_completed", "completed", "new", {"text": "已完成回复"})
    completed = provider.frame(80)
    assert visits == [3, 0, 1, 4, 0]
    assert "已完成回复" in "\n".join("".join(part[1] for part in line) for line in completed.transcript_lines)


@pytest.mark.parametrize("width", [18, 80])
@pytest.mark.parametrize("detailed", [False, True])
def test_stable_prefix_preserves_interleaved_input_order_and_anchors(width, detailed):
    runtime = TuiRuntime("interleaved-prefix")
    runtime._publish("user_message", "completed", "old", {"text": "最早的中文消息与长行"})
    runtime._publish("assistant_started", "started", "live")
    runtime._publish("assistant_delta", "delta", "live", {"text": "活动正文\n下一行"})
    runtime._publish("user_message", "completed", "steer", {"text": "中途插话"})
    cache = TuiBlockRenderCache()
    context = TuiRenderContext(width=width, detailed_transcript=detailed, now=1.0)
    snapshot = runtime.store.snapshot()
    assert render_tui_snapshot(snapshot, context, cache=cache) == render_tui_snapshot(snapshot, context)
    assert render_tui_snapshot(snapshot, replace(context, notice="新提示"), cache=cache) == render_tui_snapshot(
        snapshot, replace(context, notice="新提示"),
    )
    runtime._publish("assistant_completed", "completed", "live", {"text": "完整正文\n尾行"})
    completed = runtime.store.snapshot()
    assert render_tui_snapshot(completed, context, cache=cache) == render_tui_snapshot(completed, context)
    changed = replace(completed, stable_blocks=tuple(
        replace(block, text="替换后的原始消息", updated_seq=block.updated_seq + 100)
        if block.block_id == "old" else block for block in completed.stable_blocks
    ))
    assert render_tui_snapshot(changed, context, cache=cache) == render_tui_snapshot(changed, context)
    assert render_tui_snapshot(changed, replace(context, width=11), cache=cache) == render_tui_snapshot(
        changed, replace(context, width=11),
    )


def test_aggregate_prefix_keeps_character_budget_without_hiding_overflow():
    runtime = TuiRuntime("prefix-budget")
    for index in range(4):
        runtime._publish("assistant_completed", "completed", f"message-{index}", {"text": "正文" * 10})
    cache = TuiBlockRenderCache(max_chars=45)
    snapshot, context = runtime.store.snapshot(), TuiRenderContext(width=80)
    assert render_tui_snapshot(snapshot, context, cache=cache) == render_tui_snapshot(snapshot, context)
    prefix = cache.stable_prefix(snapshot.stable_blocks, context)
    assert sum(len(fragment[1]) for line in prefix.lines for fragment in line) <= 45
    assert len(prefix.line_ends) < len(snapshot.stable_blocks)


def test_safe_line_groups_reuse_checked_prefix_but_sanitize_new_suffix(monkeypatch):
    from agent_py_agent.cli.chat_parts.tui_safe_lines import SafeFormattedLines

    prefix = SafeFormattedLines(((("", "原文"),),))
    calls = []
    original = tui_safe_lines.sanitize_terminal_text

    def checked(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(tui_safe_lines, "sanitize_terminal_text", checked)
    combined = SafeFormattedLines.join((prefix[:], ((("", "新文字\x1b[2J"),),)))
    frame = TuiRenderFrame(combined, (), (), (), (), ())
    assert sanitize_tui_render_frame(frame).transcript_lines is combined
    assert combined[0] is prefix[0]
    assert calls == ["新文字\x1b[2J"]
    assert "\x1b" not in combined[1][0][1]


def test_cached_history_is_not_rescanned_when_frame_changes(monkeypatch):
    runtime = TuiRuntime("long-history")
    for index in range(1200):
        runtime._publish("assistant_completed", "completed", f"block-{index}", {
            "text": "\n".join(f"row {index}:{line}" for line in range(7)),
        })
    calls = []
    original = tui_safe_lines.sanitize_terminal_text

    def checked(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(tui_safe_lines, "sanitize_terminal_text", checked)
    context = TuiRenderContext(width=100)
    provider = TuiFrameProvider(runtime.store, lambda _width: context)
    first = provider.frame(100)
    before = len(calls)
    assert before > 4000, "覆盖旧短文本缓存容量外的长历史"
    context = replace(context, notice="new status")
    second = provider.frame(100)
    assert first.transcript_lines == second.transcript_lines
    assert len(calls) - before < 30, "状态变化不能再次扫描全部稳定正文"
    assert first.transcript_lines[0] is second.transcript_lines[0]


def test_new_decorated_lines_still_pass_terminal_safety():
    def handler(_event):
        return None
    safe = tui_safe_lines.SafeFormattedLine((("style", "正文\x1b[2J\x00", handler),))
    assert safe == (("style", "正文[2J", handler),)
    assert tui_safe_lines.SafeFormattedLine(safe) is safe
    frame = TuiRenderFrame((safe,), (), (), (), (), (("", "new\x9bunsafe"),))
    cleaned = sanitize_tui_render_frame(frame)
    assert cleaned.transcript_lines[0] is safe
    assert cleaned.footer == (("", "newunsafe"),)


def test_sanitized_block_cache_does_not_retain_prior_versions():
    from agent_py_agent.cli.chat_parts.tui_view_model import TuiBlock

    cache = TuiBlockRenderCache(max_entries=16, max_chars=2000)
    block = TuiBlock("stream", "assistant", "assistant", "delta", text="initial", created_seq=1, updated_seq=1)
    for index in range(80):
        cache.render(replace(block, text=f"version {index}\x1b", updated_seq=index + 2),
                     TuiRenderContext(width=80, now=index + 1.0))
    assert cache._chars <= 2000
    assert cache.stats().entries <= 16


@pytest.mark.parametrize("failure", [EOFError, KeyboardInterrupt, RuntimeError])
def test_tui_exit_always_stops_client_threads(monkeypatch, failure):
    from agent_py_agent.cli.chat_parts import tui

    def run(**_kwargs):
        raise failure()

    monkeypatch.setattr(tui, "patch_stdout", nullcontext)
    monkeypatch.setattr(tui, "_cprint", lambda *_args: None)
    context = SimpleNamespace(
        app=SimpleNamespace(run=run), pre_run=None,
        stop_event=threading.Event(), refresh_stop=threading.Event(),
        session_manager=SimpleNamespace(touch_session=lambda *_args, **_kwargs: None),
        current_session_id="resource-exit",
    )
    if failure is RuntimeError:
        with pytest.raises(RuntimeError):
            tui._run_tui_loop(context)
    else:
        assert tui._run_tui_loop(context) == 0
    assert context.stop_event.is_set() and context.refresh_stop.is_set()


def test_stream_diagnostics_have_text_and_identity_budgets():
    from agent_py_agent.cli.chat_parts.tui_events import TuiEventJournal, TuiEventSequencer

    journal = TuiEventJournal(max_events=10, max_seen_ids=100, max_payload_chars=4000, max_streams=4)
    for stream in range(15):
        sequencer = TuiEventSequencer(str(stream))
        for index in range(20):
            event = sequencer.emit("assistant_delta", "delta", "block", {"text": "x" * 1000})
            assert journal.append(event).accepted
    assert journal._payload_chars <= 4000 and len(journal._seen) <= 4
    assert len(journal.snapshot()) <= 4 and len(journal.cursors()) == 4
    assert journal.append(event).status == "duplicate"


def test_long_lived_identity_indexes_and_stable_window_are_bounded():
    from agent_py_agent.cli.chat_parts.tui_identity_window import TuiIdentityWindow
    from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore, TuiViewModelReducer

    identities = TuiIdentityWindow(maximum=32)
    for index in range(100_000):
        identities.add(str(index))
    assert len(identities) == 32 and '99999' in identities and '0' not in identities
    assert identities & {'99999'} == {'99999'}
    runtime = TuiRuntime('identity-bound', store=TuiStateStore(reducer=TuiViewModelReducer(max_stable_blocks=16)))
    for index in range(1000):
        runtime._publish('assistant_completed', 'completed', f'block-{index}', {'text':'x'})
    assert len(runtime.store.snapshot().stable_blocks) == 16
    assert len(runtime.store.reducer._stable_ids) == 32


def test_frozen_history_remains_bounded_across_repeated_older_pages():
    from agent_py_agent.cli.chat_parts.tui_transcript import TuiTranscriptModeState

    runtime = TuiRuntime('frozen-budget')
    runtime._publish('assistant_completed', 'completed', 'recent', {'text': 'recent'})
    mode = TuiTranscriptModeState(max_frozen_blocks=40)
    mode.enter(runtime.store.snapshot())
    for page in range(50):
        ids = []
        for row in range(10):
            key = f'history:{page}:{row}'
            runtime._publish('assistant_completed', 'completed', key, {'text': key})
            ids.append(key)
        mode.prepend_history(runtime.store.snapshot(), tuple(ids))
        frozen = mode.snapshot_for_render(runtime.store.snapshot())
        assert len(frozen.stable_blocks) <= 40
        assert [block.block_id for block in frozen.stable_blocks[:10]] == ids
    assert len(runtime.store.snapshot().stable_blocks) == 501
    mode.exit()
    assert mode.snapshot_for_render(runtime.store.snapshot()).stable_blocks[-1].block_id == 'history:49:9'
