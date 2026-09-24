from __future__ import annotations

from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext
from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_markdown import fragments_text
from agent_py_agent.cli.chat_parts.tui_transcript import TuiTranscriptModeState
from agent_py_agent.cli.chat_parts.tui_view import make_tui_transcript_view
from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore


def _fixture(kind: str, text: str, phase: str = "completed"):
    store = TuiStateStore()
    seq = TuiEventSequencer("complete-detail", clock=lambda: 1.0)
    if kind == "thinking_delta":
        store.publish(seq.emit("thinking_started", "started", "long"))
    store.publish(seq.emit(kind, phase, "long", {"text": text, "output": text}))
    state = TuiTranscriptModeState()
    view = make_tui_transcript_view(store, lambda width: TuiRenderContext(
        width=width, detailed_transcript=state.snapshot().active,
        show_all=state.snapshot().show_all,
    ), transcript_state=state)
    state.enter(store.snapshot())
    state.toggle_show_all()
    return store, seq, state, view


def test_complete_detail_pages_retain_every_line_and_bound_each_render():
    text = "\n".join(f"line-{i:05d}" for i in range(12000))
    _, _, state, view = _fixture("assistant_completed", text)
    collected = []
    while True:
        lines = state.complete_page_lines(80)
        collected.extend(fragments_text(line) for line in lines)
        assert len(lines) <= 1024
        if not state.move_complete_page(1):
            break
    rendered = "\n".join(collected)
    assert all(f"line-{i:05d}" in rendered for i in range(12000))
    assert len(collected) < 12500


def test_complete_detail_active_thinking_and_wide_lines_are_not_cut():
    text = "\n".join(f"thinking-{i:03d}" for i in range(150)) + "\n" + "界" * 600 + "END-MARKER"
    _, _, state, view = _fixture("thinking_delta", text, "delta")
    collected = []
    while True:
        collected.extend(fragments_text(line) for line in view.provider.frame(40).transcript_lines)
        if not state.move_complete_page(1):
            break
    rendered = "\n".join(collected)
    assert "thinking-149" in rendered
    assert "END-MARKER" in rendered
    assert rendered.count("界") == 600


def test_jump_latest_leaves_frozen_transcript_and_restores_live_tail():
    store, seq, state, view = _fixture("assistant_completed", "old")
    store.publish(seq.emit("assistant_completed", "completed", "new", {"text": "LIVE-NEW"}))
    assert "LIVE-NEW" not in str(view.provider.frame(80).transcript_lines)
    view.end()
    assert state.snapshot().active is False
    assert view.control.is_following()
    assert "LIVE-NEW" in str(view.provider.frame(80).transcript_lines)


def test_fast_page_key_before_first_render_builds_index_without_network():
    _, _, state, view = _fixture("assistant_completed", "\n".join(f"row-{i}" for i in range(900)))
    requests = []
    state.request_complete_page = lambda *args: requests.append(args)
    assert state.snapshot().complete_page_count == 0
    assert state.move_complete_page(1)
    assert state.snapshot().complete_page == 1
    assert requests == []
    assert "row-255" in str(view.provider.frame(80).transcript_lines)


def test_empty_system_placeholder_is_not_an_empty_original_header():
    _, _, _, view = _fixture("system_message", "")
    assert "── system" not in str(view.provider.frame(80).transcript_lines)


def test_remote_page_sparse_index_requests_only_current_page_and_rebind_discards_late_reply():
    store = TuiStateStore()
    seq = TuiEventSequencer("remote")
    reference = {"schema": "display_archive_ref.v1", "archive_id": "a" * 32,
                 "thread_id": "thread", "page_count": 100_000}
    store.publish(seq.emit("tool_completed", "completed", "tool", {"output": "preview", "display_archive_ref": reference}))
    state = TuiTranscriptModeState()
    state.enter(store.snapshot())
    state.toggle_show_all()
    requests = []
    state.request_complete_page = lambda ref, page: requests.append((ref, page))
    assert "正在读取" in str(state.complete_page_lines(80))
    assert len(state._complete_pages.segments) == 1
    assert requests[-1] == (reference, 0)
    state.accept_complete_page(reference, 0, {"ok": True, "page_index": 0, "rows": [{"text": "完整页0"}]})
    assert "完整页0" in str(state.complete_page_lines(80))
    assert len(requests) == 1
    state.move_complete_page(1)
    state.complete_page_lines(80)
    assert requests[-1] == (reference, 1)
    state.rebind_view_snapshot(TuiStateStore().snapshot())
    state.accept_complete_page(reference, 1, {"ok": True, "page_index": 1, "rows": [{"text": "其他代理"}]})
    assert not state._complete_cache


def test_invalid_or_failed_remote_page_is_bounded_and_requires_explicit_retry():
    store = TuiStateStore()
    seq = TuiEventSequencer("remote-failure")
    reference = {"schema": "display_archive_ref.v1", "archive_id": "b" * 32, "thread_id": "thread", "page_count": 1}
    store.publish(seq.emit("tool_completed", "completed", "tool", {"output": "preview", "display_archive_ref": reference}))
    state = TuiTranscriptModeState()
    state.enter(store.snapshot())
    state.toggle_show_all()
    state.complete_page_lines(80)
    state.accept_complete_page(reference, 0, {"ok": True, "page_index": 0, "rows": [{"text": "x" * 100_000}]})
    assert "读取失败" in str(state.complete_page_lines(80))
    assert len(state._complete_cache) == 1
    state.retry_complete_page()
    assert not state._complete_cache


def test_real_key_pipeline_complete_pages_then_ctrl_end_returns_input(tmp_path):
    import asyncio

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    from agent_py_agent.cli.chat_parts.tui_ui_setup import (
        _assemble_tui_application,
        _make_normal_tui_body,
        _make_transcript_tui_body,
        _prepare_tui_app_parts,
    )
    from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params

    async def scenario():
        runtime = TuiRuntime("keys")
        seq = TuiEventSequencer("keys")
        runtime.store.publish(seq.emit("assistant_completed", "completed", "long", {
            "text": "\n".join(f"line-{i}" for i in range(900))}))
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            params = _app_params(tmp_path, runtime)
            parts = _prepare_tui_app_parts(params)
            app = _assemble_tui_application(params, parts, _make_normal_tui_body(parts), _make_transcript_tui_body(parts))
            task = asyncio.create_task(app.run_async())
            try:
                await asyncio.sleep(.05)
                pipe.send_bytes(b"\x0f\x05]")
                await asyncio.sleep(.1)
                assert parts.transcript_state.snapshot().show_all
                assert parts.transcript_state.snapshot().complete_page_count > 1
                assert parts.transcript_state.snapshot().complete_page == 1
                pipe.send_text("]")
                await asyncio.sleep(.05)
                assert parts.transcript_state.snapshot().complete_page == 2
                pipe.send_bytes(b"\x05")
                await asyncio.sleep(.05)
                assert not parts.transcript_state.snapshot().show_all
                pipe.send_bytes(b"\x1b[1;5F")
                await asyncio.sleep(.05)
                assert not parts.transcript_state.snapshot().active
                assert app.layout.current_control is parts.input_area.control
                pipe.send_text("[x]")
                await asyncio.sleep(.05)
                assert parts.input_area.text == "[x]"
            finally:
                app.exit(result=0)
                await task
    asyncio.run(scenario())
