"""模型菜单通过真实 prompt_toolkit 按键验证；不代替真机 TUI + MiniMax 验收。"""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_py_agent.agent.settings.model_profiles import model_profiles_path, read_model_profiles
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
from agent_py_agent.tests.test_model_profiles import Host, add
from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params


async def settle():
    await asyncio.sleep(0.15)


def test_tui_menu_add_select_cancel_and_secret_history(tmp_path):
    async def scenario():
        runtime = TuiRuntime("models")
        runtime.publish_session(version="0.3.0", model="deployment-model", workspace=str(tmp_path))
        params = _app_params(tmp_path, runtime)
        host = Host(tmp_path / "config")
        params.agent.home_paths = host.home_paths
        params.agent.config = host.config
        host.config.session_workspace = str(tmp_path / "sessions")
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                app = make_tui_app(params)
                runner = asyncio.create_task(app.run_async())
                try:
                    await settle()
                    pipe.send_text("/model\r")
                    await settle()
                    assert app._my_agent_model_menu_active
                    assert len(app._my_agent_model_float_container.floats) == 1
                    pipe.send_bytes(b"\x1b[B\r")  # 新增
                    await settle()
                    pipe.send_bytes(b"\x1b[B\r")  # Anthropic
                    await settle()
                    pipe.send_text("MiniMax-M2.7\thttps://example.test/anthropic\tonly-private-secret\t")
                    await settle()
                    pipe.send_bytes(b"\x01\x0b")
                    pipe.send_text("96000\t\r")  # 保存
                    await settle()
                    data = read_model_profiles(model_profiles_path(host.home_paths))
                    assert len(data["profiles"]) == 1
                    row = next(iter(data["profiles"].values()))
                    assert data["providers"][row["provider_id"]]["api_key"] == "only-private-secret"
                    assert row["model_context_window_tokens"] == 96000
                    assert runtime.store.snapshot().selected_model_name == "deployment-model"
                    pipe.send_bytes(b"\r")  # 选择已有模型
                    await settle()
                    pipe.send_bytes(b"\x1b[B\r")
                    await settle()
                    data = read_model_profiles(model_profiles_path(host.home_paths))
                    assert data["selected"] != "default"
                    assert runtime.store.snapshot().selected_model_name == "MiniMax-M2.7"
                    assert host.config.model_name == "deployment-model"
                    pipe.send_bytes(b"\x1b")
                    await asyncio.sleep(0.3)
                    assert not app._my_agent_model_menu_active
                    assert params.jobs.empty() and not params.stop_event.is_set()
                    history_files = list(tmp_path.rglob("input_history"))
                    assert all("only-private-secret" not in path.read_text() for path in history_files)
                    assert all("only-private-secret" not in block.text for block in runtime.store.snapshot().stable_blocks)
                finally:
                    app.exit()
                    await runner
    asyncio.run(scenario())


@pytest.mark.parametrize("gateway", [False, True])
def test_saved_model_label_refresh_does_not_change_execution_or_history(tmp_path, gateway):
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
    from agent_py_agent.cli.chat_parts.tui_model_menu import refresh_model_selection

    host = Host(tmp_path / "config")
    profile_id, _ = add(host, model_name="qwen-test")
    execute_model_profile_operation(host, "select", {"profile_id": profile_id})
    source = host if not gateway else SimpleNamespace(
        gateway_client_only=True,
        request_models=lambda *, session_id, operation, payload: execute_model_profile_operation(host, operation, payload),
    )
    runtime = TuiRuntime("saved-choice")
    runtime.publish_session(version="0.3.0", model="deployment-model", workspace="workspace")
    runtime.enqueue_prompt("hello", "你好", queued=False)
    before = runtime.store.snapshot()
    refresh_model_selection(source, "saved-choice", runtime)
    after = runtime.store.snapshot()
    assert after.selected_model_name == "qwen-test"
    assert after.stable_blocks == before.stable_blocks
    assert after.status == before.status
    assert host.config.model_name == "deployment-model"
    stopped = threading.Event()
    stopped.set()
    execute_model_profile_operation(host, "select", {"profile_id": "default"})
    refresh_model_selection(source, "saved-choice", runtime, stopped)
    assert runtime.store.snapshot().selected_model_name == "qwen-test"
    refresh_model_selection(source, "saved-choice", runtime)
    assert runtime.store.snapshot().selected_model_name == "deployment-model"


def test_model_refresh_failure_keeps_last_confirmed_display():
    from agent_py_agent.cli.chat_parts.tui_model_menu import refresh_model_selection

    runtime = TuiRuntime("unavailable")
    runtime.publish_model_selection("confirmed-model")
    source = SimpleNamespace(gateway_client_only=True, request_models=lambda **kwargs: {"ok": False})
    refresh_model_selection(source, "unavailable", runtime)
    assert runtime.store.snapshot().selected_model_name == "confirmed-model"
    assert "尚未同步" in runtime.notice()


@pytest.mark.parametrize("width", [58, 120])
def test_current_model_renders_in_frozen_welcome_after_selection(tmp_path, width):
    from agent_py_agent.cli.chat_parts.tui_interaction import TuiInteractionState
    from agent_py_agent.cli.chat_parts.tui_transcript import TuiTranscriptModeState
    from agent_py_agent.cli.chat_parts.tui_ui_setup import _make_render_context_factory
    from agent_py_agent.cli.chat_parts.tui_view import TuiFrameProvider

    runtime = TuiRuntime("render-selection")
    runtime.publish_session(version="0.3.0", model="old-model", workspace="workspace")
    mode = TuiTranscriptModeState()
    mode.enter(runtime.store.snapshot())
    params = _app_params(tmp_path, runtime)
    factory = _make_render_context_factory(params, TuiInteractionState(), mode)
    provider = TuiFrameProvider(runtime.store, factory, transcript_state=mode)
    before = "\n".join("".join(text for _, text in row) for row in provider.frame(width).transcript_lines)
    assert "old-model" in before
    runtime.publish_model_selection("qwen-test")
    after = "\n".join("".join(text for _, text in row) for row in provider.frame(width).transcript_lines)
    assert "qwen-test" in after and "old-model" not in after
