"""模型菜单通过真实 prompt_toolkit 按键验证；不代替真机 TUI + MiniMax 验收。"""

import asyncio

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_py_agent.agent.settings.model_profiles import model_profiles_path, read_model_profiles
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
from agent_py_agent.tests.test_model_profiles import Host
from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params


async def settle():
    await asyncio.sleep(0.15)


def test_tui_menu_add_select_cancel_and_secret_history(tmp_path):
    async def scenario():
        runtime = TuiRuntime("models")
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
                    assert row["api_key"] == "only-private-secret"
                    assert row["model_context_window_tokens"] == 96000
                    pipe.send_bytes(b"\r")  # 选择已有模型
                    await settle()
                    pipe.send_bytes(b"\x1b[B\r")
                    await settle()
                    data = read_model_profiles(model_profiles_path(host.home_paths))
                    assert data["selected"] != "default"
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
