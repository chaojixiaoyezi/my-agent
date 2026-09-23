import threading
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.cli.chat_parts import tui_safe_lines
from agent_py_agent.cli.chat_parts.tui_block_renderer import (
    TuiBlockRenderCache,
    TuiRenderContext,
    TuiRenderFrame,
    sanitize_tui_render_frame,
)
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_view import TuiFrameProvider


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
