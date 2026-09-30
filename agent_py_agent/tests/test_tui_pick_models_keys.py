"""/model 弹窗的按键：勾选添加后 Esc 能关菜单、聊天框 Tab 可用；模型表单与「高级」页 Tab/Esc 可用；鼠标把焦点点到弹窗外后
自动拉回（09-30 用户卡住的回归）。进入路径：/model →「管理已有模型」→ 账号 →「添加模型」。"""
import asyncio
import time

from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput


class SmallOutput(DummyOutput):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def get_size(self):
        return Size(rows=self.rows, columns=80)

from agent_py_agent.agent.settings import model_oauth as auth_runtime
from agent_py_agent.agent.settings import model_provider_network as network
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    model_profiles_path,
    read_model_profiles,
)
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
from agent_py_agent.tests.test_thread_model_selection import host_with_store
from agent_py_agent.tests.test_tui_decision_menu import visible, wait_app, wait_dialog_ready
from agent_py_agent.tests.test_tui_model_add import wait_redrawn
from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params


def signed_in_chatgpt(host, monkeypatch):
    execute_model_profile_operation(host, "save_provider", {"provider_id": "account", "provider": {
        "display_name": "ChatGPT 订阅", "api_base": "https://chatgpt.com/backend-api/codex", "auth": {"mode": "chatgpt"}}})
    monkeypatch.setattr(auth_runtime, "exchange_browser_code", lambda auth, pending, code: {
        "access_token": "tok", "refresh_token": "ref", "account_id": "acct", "expires_at": time.time() + 3600})
    started = execute_model_profile_operation(host, "auth_browser_start", {"provider_id": "account",
                                                                          "redirect_uri": "http://127.0.0.1:1455/auth/callback"})
    execute_model_profile_operation(host, "auth_browser_complete", {"provider_id": "account", "attempt_id": started["attempt_id"],
                                                                    "code": "c", "state": started["state"]})
    names = ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
             "gpt-daybreak-blue-latest", "gpt-5.5"]
    monkeypatch.setattr(network, "get_json", lambda request: {"models": [
        {"slug": name, "display_name": name.upper(), "context_window": 272000, "visibility": "list"} for name in names]})


import pytest


# 函数用途: 从 /model 主菜单进「管理已有模型」→ 这个订阅账号 →「添加模型」，停在勾选框。
async def open_account_picker(app, pipe):
    pipe.send_bytes(b"\x1b[B\x1b[B\r")  # 顶层第 3 项「管理已有模型」
    await wait_dialog_ready(app, "管理已有模型 · 选择连接或账号", "连接和账号列表")
    pipe.send_bytes(b"\r")  # 唯一的订阅账号
    await wait_dialog_ready(app, "账号操作 · ChatGPT 订阅", "账号操作")
    pipe.send_bytes(b"\x1b[B\r")  # 「添加模型」
    await wait_dialog_ready(app, "选择要使用的模型（可多选）", "模型勾选框")


@pytest.mark.parametrize("rows", [24, 12])
def test_after_adding_models_escape_closes_menu_and_tab_still_works(tmp_path, monkeypatch, rows):
    async def scenario():
        runtime = TuiRuntime("models")
        runtime.publish_session(version="0.3.0", model="deployment-model", workspace=str(tmp_path))
        params = _app_params(tmp_path, runtime)
        host = host_with_store(tmp_path)
        params.agent.home_paths = host.home_paths
        params.agent.config = host.config
        params.agent.conversation_store = host.conversation_store
        host.config.session_workspace = str(tmp_path / "sessions")
        signed_in_chatgpt(host, monkeypatch)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=SmallOutput(rows)):
            app = make_tui_app(params)
            runner = asyncio.create_task(app.run_async())
            try:
                await wait_app(app, lambda: app.is_running, "TUI 启动")
                pipe.send_text("/model\r")
                await wait_dialog_ready(app, "模型配置 /model", "打开模型菜单")
                await open_account_picker(app, pipe)
                for index in range(9):
                    pipe.send_bytes(b" " + (b"\x1b[B" if index < 8 else b""))
                    await asyncio.sleep(0.05)
                pipe.send_bytes(b"\t")
                await asyncio.sleep(0.1)
                pipe.send_bytes(b"\r")
                await wait_dialog_ready(app, "已添加 9 个模型", "添加后回到模型菜单")
                data = read_model_profiles(model_profiles_path(host.home_paths))
                assert len(data["profiles"]) == 9
                pipe.send_bytes(b"\x1b")
                await wait_app(app, lambda: not app._my_agent_model_menu_active, "Esc 关闭模型菜单")
                assert not app._my_agent_model_float_container.floats
                pipe.send_text("/mo")
                await asyncio.sleep(0.2)
                pipe.send_bytes(b"\t")
                await wait_app(app, lambda: "/model" in visible(app), "聊天框 Tab 仍可用")
            finally:
                app.exit()
                await runner

    asyncio.run(scenario())


@pytest.mark.parametrize("rows", [40, 24])
def test_model_form_and_advanced_page_tab_and_escape_work(tmp_path, monkeypatch, rows):
    from agent_py_agent.cli.chat_parts import tui_provider_menu

    async def scenario():
        runtime = TuiRuntime("models")
        params = _app_params(tmp_path, runtime)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=SmallOutput(rows)):
            app = make_tui_app(params)
            runner = asyncio.create_task(app.run_async())
            try:
                await wait_app(app, lambda: app.is_running, "TUI 启动")
                app._my_agent_model_menu_active = True
                task = asyncio.create_task(tui_provider_menu._edit_model(app, params.agent, "models", "account", {
                    "model_name": "gpt-a", "model_context_window_tokens": 272000, "model_backend": "openai_responses"}))
                # 原记录已有接口类型，直接进表单，不再先弹接口选择。
                await wait_dialog_ready(app, "温度、思考控制等在「高级」", "模型表单")
                for _ in range(4):  # 名称 → 上下文 → 启用 → 「保存」→「高级」
                    pipe.send_bytes(b"\t")
                    await asyncio.sleep(0.1)
                pipe.send_bytes(b"\r")
                await wait_dialog_ready(app, "思考控制（决定 /effort", "「高级」页")
                for _ in range(6):
                    pipe.send_bytes(b"\t")
                    await asyncio.sleep(0.05)
                pipe.send_bytes(b"\x1b")  # Esc 只关「高级」页，回到表单
                await wait_redrawn(app, "温度、思考控制等在「高级」", "回到模型表单")
                assert len(app._my_agent_model_float_container.floats) == 1
                pipe.send_bytes(b"\x1b")
                result = await asyncio.wait_for(task, 3)
                assert result == "" and not app._my_agent_model_float_container.floats
            finally:
                app.exit()
                await runner

    asyncio.run(scenario())


def test_escape_still_closes_picker_after_focus_leaves_the_dialog(tmp_path, monkeypatch):
    async def scenario():
        runtime = TuiRuntime("models")
        runtime.publish_session(version="0.3.0", model="deployment-model", workspace=str(tmp_path))
        params = _app_params(tmp_path, runtime)
        host = host_with_store(tmp_path)
        params.agent.home_paths = host.home_paths
        params.agent.config = host.config
        params.agent.conversation_store = host.conversation_store
        host.config.session_workspace = str(tmp_path / "sessions")
        signed_in_chatgpt(host, monkeypatch)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=SmallOutput(40)):
            app = make_tui_app(params)
            runner = asyncio.create_task(app.run_async())
            try:
                await wait_app(app, lambda: app.is_running, "TUI 启动")
                chat_input = app.layout.current_window
                pipe.send_text("/model\r")
                await wait_dialog_ready(app, "模型配置 /model", "打开模型菜单")
                await open_account_picker(app, pipe)
                app.layout.focus(chat_input)  # 模拟鼠标点到弹窗外面
                app.invalidate()
                await wait_app(app, lambda: app.layout.has_focus(app._my_agent_model_float_container.floats[-1].content),
                               "焦点被拉回勾选框")
                pipe.send_bytes(b" ")
                await asyncio.sleep(0.1)
                pipe.send_bytes(b"\t")
                await asyncio.sleep(0.1)
                pipe.send_bytes(b"\r")
                await wait_dialog_ready(app, "已添加 1 个模型", "焦点拉回后 Tab 到「添加」回车能保存")
                app.layout.focus(chat_input)  # 回到主菜单后再点一次外面
                app.invalidate()
                await asyncio.sleep(0.2)
                pipe.send_bytes(b"\x1b")
                await wait_app(app, lambda: not app._my_agent_model_menu_active, "Esc 仍能关掉模型菜单")
            finally:
                app.exit()
                await runner

    asyncio.run(scenario())
