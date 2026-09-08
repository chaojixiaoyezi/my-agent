from __future__ import annotations

from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext
from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_markdown import fragments_text
from agent_py_agent.cli.chat_parts.tui_transcript import TuiTranscriptModeState
from agent_py_agent.cli.chat_parts.tui_view import TuiFrameProvider
from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore


def test_frozen_detailed_transcript_keeps_live_refresh_warning_and_recovers():
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("frozen-refresh-warning")
    state = TuiTranscriptModeState()
    state.enter(runtime.store.snapshot())
    provider = TuiFrameProvider(
        runtime.store,
        lambda width: TuiRenderContext(
            width=width, detailed_transcript=True,
            background_sync_failed=runtime.store.snapshot().background_sync_failed,
        ),
        transcript_state=state,
    )
    normal_footer = provider.frame(100).footer
    runtime.publish_background_sync_status(ok=False)
    assert "状态刷新失败" in fragments_text(provider.frame(100).footer)
    runtime.publish_background_sync_status(ok=True)
    assert provider.frame(100).footer == normal_footer
    assert state.snapshot().active is True


def _provider(
    store: TuiStateStore,
    state: TuiTranscriptModeState,
) -> TuiFrameProvider:
    return TuiFrameProvider(
        store,
        lambda width: TuiRenderContext(
            width=width,
            detailed_transcript=state.snapshot().active,
            show_all=state.snapshot().show_all,
        ),
        transcript_state=state,
    )


def test_transcript_freezes_view_until_exit() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("transcript-freeze", clock=lambda: 1.0)
    store.publish(seq.emit("user_message", "completed", "one", {"text": "alpha"}))
    state = TuiTranscriptModeState()
    provider = _provider(store, state)

    state.enter(store.snapshot())
    store.publish(seq.emit("user_message", "completed", "two", {"text": "beta"}))
    frozen = "\n".join(
        fragments_text(line) for line in provider.frame(80).transcript_lines
    )
    state.exit()
    live = "\n".join(
        fragments_text(line) for line in provider.frame(80).transcript_lines
    )

    assert "alpha" in frozen
    assert "beta" not in frozen
    assert "beta" in live


def test_transcript_incremental_search_highlights_and_navigates() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("transcript-search", clock=lambda: 1.0)
    for index, text in enumerate(("alpha one", "middle", "alpha two")):
        store.publish(
            seq.emit(
                "system_message",
                "completed",
                f"line-{index}",
                {"text": text},
            )
        )
    state = TuiTranscriptModeState()
    provider = _provider(store, state)
    state.enter(store.snapshot())
    state.open_search(anchor_line=0)
    state.update_search_query("alpha")

    frame = provider.frame(80)
    snapshot = state.snapshot()

    assert snapshot.match_count == 2
    assert snapshot.current_match == 1
    assert any(
        "tui-search-current" in style
        for line in frame.transcript_lines
        for style, _text in line
    )
    first_line = state.commit_search()
    second_line = state.navigate()
    previous_line = state.navigate(reverse=True)
    assert first_line is not None
    assert second_line is not None and second_line > first_line
    assert previous_line == first_line


def test_transcript_search_cancel_restores_anchor_and_resize_clears_query() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("transcript-resize", clock=lambda: 1.0)
    store.publish(
        seq.emit("system_message", "completed", "line", {"text": "searchable"})
    )
    state = TuiTranscriptModeState()
    provider = _provider(store, state)
    state.enter(store.snapshot())
    state.open_search(anchor_line=7)
    state.update_search_query("search")
    provider.frame(80)

    assert state.cancel_search() == 7
    state.open_search(anchor_line=0)
    state.update_search_query("search")
    provider.frame(80)
    provider.invalidate()
    provider.frame(79)

    snapshot = state.snapshot()
    assert snapshot.search_open is False
    assert snapshot.search_query == ""
    assert snapshot.match_count == 0
