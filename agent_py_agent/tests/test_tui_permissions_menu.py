"""通过 prompt_toolkit 真实按键覆盖权限表单与审批浮层并存，不用它代替真机 TUI 验收。"""

import asyncio

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_py_agent.agent.user_space.approval_mode import read_permission_mode
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
from agent_py_agent.tests.test_model_profiles import Host
from agent_py_agent.tests.test_owner_approval_mode import owner_home
from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params


@pytest.mark.parametrize("admin", [False, True])
def test_tui_permissions_save_cancel_and_full_confirmation(tmp_path, admin):
    async def scenario():
        runtime = TuiRuntime("permissions")
        params = _app_params(tmp_path, runtime)
        host = Host(tmp_path / "config")
        host.config.session_workspace = str(tmp_path / "sessions")
        params.agent.config = host.config
        params.agent.home_paths = home = owner_home(tmp_path, admin=admin)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app = make_tui_app(params)
            runner = asyncio.create_task(app.run_async())
            try:
                await asyncio.sleep(0.15)
                pipe.send_text("/permissions\r")
                await asyncio.sleep(0.2)
                assert app._my_agent_model_menu_active
                assert len(app._my_agent_model_float_container.floats) == 1
                pipe.send_bytes(b"\x1b[B\r")
                await asyncio.sleep(0.2)
                assert read_permission_mode(home) == "auto"
                assert not app._my_agent_model_menu_active
                assert "自主工作" in runtime.notice()
                pipe.send_bytes(b"\x1bOS")  # F4
                await asyncio.sleep(0.2)
                assert app._my_agent_model_menu_active
                pipe.send_bytes(b"\x1b[B\r")
                await asyncio.sleep(0.2)
                if admin:
                    assert app._my_agent_model_menu_active
                    assert read_permission_mode(home) == "auto"
                    pipe.send_bytes(b"\x1b")
                    await asyncio.sleep(0.3)
                    assert read_permission_mode(home) == "auto"
                    pipe.send_text("/permissions full-access\r")
                    await asyncio.sleep(0.2)
                    assert read_permission_mode(home) == "full-access"
                else:
                    assert read_permission_mode(home) == "auto"
                assert params.jobs.empty() and not params.stop_event.is_set()
            finally:
                app.exit()
                await runner
    asyncio.run(scenario())


def test_f4_menu_keeps_focus_while_tool_approval_is_pending(tmp_path):
    from agent_py_agent.tests.test_tui_runtime import _approval_request

    async def scenario():
        runtime = TuiRuntime("permissions-pending")
        params = _app_params(tmp_path, runtime)
        host = Host(tmp_path / "config")
        host.config.session_workspace = str(tmp_path / "sessions")
        params.agent.config = host.config
        params.agent.home_paths = home = owner_home(tmp_path)
        runtime.enqueue_prompt("request-permission", "整理资料", queued=False)
        turn = runtime.begin_turn("request-permission")
        request = _approval_request("request-permission")
        turn.configure_gateway_permission_sink(lambda req, decision: None)
        turn.on_gateway_event({"kind": "permission_requested", "permission": request.to_dict()})
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app = make_tui_app(params)
            runner = asyncio.create_task(app.run_async())
            try:
                await asyncio.sleep(0.15)
                pipe.send_bytes(b"\x1bOS")
                await asyncio.sleep(0.2)
                assert app._my_agent_model_menu_active
                pipe.send_bytes(b"\x1b[B\r")
                await asyncio.sleep(0.2)
                assert read_permission_mode(home) == "auto"
                assert runtime.store.snapshot().permission.permission_id == request.permission_id
                assert not params.stop_event.is_set()
                turn.on_gateway_event({"kind": "permission_resolved", "permission_id": request.permission_id,
                                       "decision": "approved"})
                assert runtime.store.snapshot().permission is None
            finally:
                app.exit()
                await runner
    asyncio.run(scenario())
