from __future__ import annotations

import asyncio
import io
import threading
from queue import Queue
from types import SimpleNamespace

from prompt_toolkit.application import create_app_session
from prompt_toolkit.cursor_shapes import CursorShape
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output

from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
from agent_py_agent.cli.chat_parts.tui_params import MakeTuiAppParams
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_ui_setup import _prepare_tui_app_parts, make_tui_app


def _app_params(
    tmp_path,
    runtime: TuiRuntime,
    *,
    agent_navigation: object | None = None,
) -> MakeTuiAppParams:
    pending = [0]
    return MakeTuiAppParams(
        agent=SimpleNamespace(
            root=tmp_path,
            config=SimpleNamespace(agent_name="my-agent", model_name="fixture"),
        ),
        state_lock=threading.Lock(),
        is_running_ref=[False],
        pending_jobs_ref=pending,
        running_started_at_ref=[0.0],
        last_token_estimate_ref=[0],
        jobs=Queue(),
        pending_jobs_ref_for_enqueue=pending,
        runtime_inject=[],
        prompt_files=[],
        args=SimpleNamespace(memory_limit=0),
        use_gateway=False,
        paths=SimpleNamespace(),
        assistant_outputs=[],
        shutting_down_ref=[False],
        running_prompt_ref=[""],
        running_request_id_ref=[""],
        stop_event=threading.Event(),
        current_session_id="pipe-session",
        tui_runtime=runtime,
        agent_navigation=agent_navigation,
    )


def test_real_prompt_toolkit_pipe_handles_wrapped_arrows_and_bracketed_paste(tmp_path) -> None:
    async def scenario() -> None:
        runtime = TuiRuntime("pipe-session")
        runtime.publish_session(version="test", model="fixture", workspace=str(tmp_path))
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=DummyOutput()):
                app = make_tui_app(_app_params(tmp_path, runtime))
                run_task = asyncio.create_task(app.run_async())
                await asyncio.sleep(0.05)

                pipe_input.send_text("x" * 110)
                await asyncio.sleep(0.05)
                cursor_at_tail = app.current_buffer.cursor_position
                pipe_input.send_bytes(b"\x1b[A")
                await asyncio.sleep(0.05)
                assert app.current_buffer.cursor_position < cursor_at_tail

                pipe_input.send_bytes(b"\x1b[200~one\r\ntwo\x1b[31mred\x1b[0m\x1b[201~")
                await asyncio.sleep(0.08)
                assert "one\ntwored" in app.current_buffer.text
                assert "\r" not in app.current_buffer.text
                assert "\x1b" not in app.current_buffer.text

                app.exit(result=0)
                assert await run_task == 0

    asyncio.run(scenario())


def test_bracketed_paste_followed_immediately_by_enter_submits_once(tmp_path) -> None:
    async def scenario() -> None:
        runtime = TuiRuntime("paste-enter-session")
        runtime.publish_session(version="test", model="fixture", workspace=str(tmp_path))
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=DummyOutput()):
                params = _app_params(tmp_path, runtime)
                app = make_tui_app(params)
                run_task = asyncio.create_task(app.run_async())
                await asyncio.sleep(0.05)

                pipe_input.send_bytes(
                    b"\x1b[200~paste then submit\x1b[201~\r"
                )
                await asyncio.sleep(0.12)

                assert app.current_buffer.text == ""
                assert params.jobs.qsize() == 1
                assert params.jobs.get_nowait().user == "paste then submit"

                app.exit(result=0)
                assert await run_task == 0

    asyncio.run(scenario())


def test_tui_declares_block_cursor_instead_of_inheriting_terminal_shape(tmp_path) -> None:
    runtime = TuiRuntime("cursor-session")
    runtime.publish_session(version="test", model="fixture", workspace=str(tmp_path))

    with create_pipe_input() as pipe_input:
        with create_app_session(input=pipe_input, output=DummyOutput()):
            app = make_tui_app(_app_params(tmp_path, runtime))

    assert app.cursor.get_cursor_shape(app) is CursorShape.BLOCK


def test_real_prompt_toolkit_pipe_defaults_tui_mouse_and_f6_toggles(tmp_path) -> None:
    async def scenario() -> None:
        runtime = TuiRuntime("mouse-filter-session")
        runtime.publish_session(version="test", model="fixture", workspace=str(tmp_path))
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=DummyOutput()):
                app = make_tui_app(_app_params(tmp_path, runtime))
                assert app.mouse_support() is True
                run_task = asyncio.create_task(app.run_async())
                await asyncio.sleep(0.05)

                pipe_input.send_bytes(b"\x1b[17~")
                await asyncio.sleep(0.05)
                assert app.mouse_support() is False

                pipe_input.send_bytes(b"\x1b[17~")
                await asyncio.sleep(0.05)
                assert app.mouse_support() is True

                app.exit(result=0)
                assert await run_task == 0

    asyncio.run(scenario())


def test_real_vt100_output_enables_default_mouse_and_f6_emits_both_transitions(
    tmp_path,
) -> None:
    async def scenario() -> None:
        runtime = TuiRuntime("mouse-protocol-session")
        runtime.publish_session(version="test", model="fixture", workspace=str(tmp_path))
        terminal = io.StringIO()
        output = Vt100_Output(
            terminal,
            get_size=lambda: Size(rows=30, columns=100),
            term="xterm-256color",
        )
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=output):
                app = make_tui_app(_app_params(tmp_path, runtime))
                run_task = asyncio.create_task(app.run_async())
                await asyncio.sleep(0.08)

                started = terminal.getvalue()
                assert "\x1b[?1000h" in started
                assert "\x1b[?1003h" in started
                assert "\x1b[?1006h" in started

                offset = len(started)
                pipe_input.send_bytes(b"\x1b[17~")
                await asyncio.sleep(0.08)
                disabled = terminal.getvalue()[offset:]
                assert "\x1b[?1000l" in disabled
                assert "\x1b[?1003l" in disabled
                assert "\x1b[?1006l" in disabled

                offset = len(terminal.getvalue())
                pipe_input.send_bytes(b"\x1b[17~")
                await asyncio.sleep(0.08)
                restored = terminal.getvalue()[offset:]
                assert "\x1b[?1000h" in restored
                assert "\x1b[?1003h" in restored
                assert "\x1b[?1006h" in restored

                app.exit(result=0)
                assert await run_task == 0

    asyncio.run(scenario())


def test_navigation_callback_restores_root_viewport_and_explains_native_scroll(
    tmp_path,
) -> None:
    root = TuiRuntime("navigation-viewport-session")
    for index in range(10):
        root.write_console(f"root history {index}")
    navigation = TuiAgentNavigationState(root)
    navigation.update_rows(
        "",
        [
            {
                "run_id": "child-scroll",
                "parent_run_id": "",
                "name": "child-scroll",
                "status": "DONE",
                "description": "滚动验证",
            }
        ],
    )
    params = _app_params(tmp_path, root, agent_navigation=navigation)
    params.agent.config.tui_mouse_capture_default = False
    parts = _prepare_tui_app_parts(params)
    parts.transcript_view.control.create_content(40, 5)
    parts.transcript_view.scroll(-6)
    root_anchor = parts.transcript_view.control.current_line()

    assert navigation.move_selection(1) is True
    assert navigation.enter_selected() is True
    assert parts.transcript_view.provider.state_store is navigation.active_runtime().store

    assert navigation.back() is True
    restored = parts.transcript_view.control.create_content(40, 5)

    assert parts.transcript_view.provider.state_store is root.store
    assert restored.cursor_position.y == root_anchor
    assert "PgUp/Ctrl+Home" in root.notice()
    assert parts.interaction.snapshot().mouse_capture_enabled is False
