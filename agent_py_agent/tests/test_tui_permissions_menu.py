"""通过 prompt_toolkit 真实按键覆盖权限表单与审批浮层并存，不用它代替真机 TUI 验收。"""

import asyncio
import time

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_py_agent.agent.user_space.approval_mode import read_permission_mode
from agent_py_agent.cli.chat_parts import tui_permissions_menu
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
from agent_py_agent.tests.test_model_profiles import Host
from agent_py_agent.tests.test_owner_approval_mode import owner_home
from agent_py_agent.tests.test_tui_decision_menu import wait_app, wait_dialog_ready
from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params

# 权限菜单与 Full Access 二次确认各自独有的一行说明；出现即表示对话框已挂上并拿到焦点（_dialog 同步挂浮层、给焦点），
# 但浮层上的回车/Esc 要等重绘登记父子关系后才生效，见 test_tui_decision_menu.wait_dialog_ready。
_MENU = "权限模式 · 适用于本用户所有会话和子代理"
_CONFIRM = "仅管理员：主代理可读写家目录外的文件、运行系统命令。"
# 读写权限走 asyncio.to_thread；slow_io 让查询与保存各慢 0.5 秒，模拟慢机器上“按键已处理、还没读到/存好”的窗口。
_DELAYS = pytest.mark.parametrize("delay", [0.0, 0.5], ids=["fast", "slow_io"])


# LLM: 只在权限读写线程里多等 delay 秒，返回值不变；菜单按调用时的模块属性取 request_permissions，替换即生效。
# 函数用途: 让权限菜单的查询与保存都变慢，证明测试按状态等待，而不是赌固定时长够用。
def slow_permissions(monkeypatch, delay):
    if not delay:
        return
    original = tui_permissions_menu.request_permissions

    def slow(*args, **kwargs):
        time.sleep(delay)
        return original(*args, **kwargs)

    monkeypatch.setattr(tui_permissions_menu, "request_permissions", slow)


@_DELAYS
@pytest.mark.parametrize("admin", [False, True])
def test_tui_permissions_save_cancel_and_full_confirmation(tmp_path, monkeypatch, admin, delay):
    slow_permissions(monkeypatch, delay)

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
                await wait_app(app, lambda: app.is_running, "TUI 启动")
                pipe.send_text("/permissions\r")
                await wait_dialog_ready(app, _MENU, "/permissions 打开权限菜单")
                assert app._my_agent_model_menu_active
                assert len(app._my_agent_model_float_container.floats) == 1
                pipe.send_bytes(b"\x1b[B\r")
                # 保存在线程里完成后才清菜单标志，所以标志清掉时模式必须已经落盘。
                await wait_app(app, lambda: not app._my_agent_model_menu_active, "保存后菜单关闭")
                assert read_permission_mode(home) == "auto"
                assert "自主工作" in runtime.notice()
                pipe.send_bytes(b"\x1bOS")  # F4
                await wait_dialog_ready(app, _MENU, "F4 打开权限菜单")
                pipe.send_bytes(b"\x1b[B\r")
                if admin:
                    await wait_dialog_ready(app, _CONFIRM, "Full Access 二次确认出现")
                    assert app._my_agent_model_menu_active
                    assert read_permission_mode(home) == "auto"
                    pipe.send_bytes(b"\x1b")
                    await wait_app(app, lambda: not app._my_agent_model_menu_active, "取消二次确认后菜单关闭")
                    assert read_permission_mode(home) == "auto"
                    pipe.send_text("/permissions full-access\r")
                    await wait_app(app, lambda: read_permission_mode(home) == "full-access", "直接命令保存 full-access")
                else:
                    await wait_app(app, lambda: not app._my_agent_model_menu_active, "保存后菜单关闭")
                    assert read_permission_mode(home) == "auto"
                assert params.jobs.empty() and not params.stop_event.is_set()
            finally:
                app.exit()
                await runner
    asyncio.run(scenario())


@_DELAYS
def test_f4_menu_keeps_focus_while_tool_approval_is_pending(tmp_path, monkeypatch, delay):
    from agent_py_agent.tests.test_tui_runtime import _approval_request

    slow_permissions(monkeypatch, delay)

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
                await wait_app(app, lambda: app.is_running, "TUI 启动")
                pipe.send_bytes(b"\x1bOS")
                await wait_dialog_ready(app, _MENU, "审批等待时 F4 打开权限菜单")
                assert app._my_agent_model_menu_active
                pipe.send_bytes(b"\x1b[B\r")
                await wait_app(app, lambda: not app._my_agent_model_menu_active, "保存后菜单关闭")
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
