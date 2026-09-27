"""模型菜单通过真实 prompt_toolkit 按键验证；不代替真机 TUI + MiniMax 验收。"""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_py_agent.agent.settings.model_profiles import model_profiles_path, read_model_profiles
from agent_py_agent.agent.settings.thread_model_selection import execute_local_model_operation
from agent_py_agent.cli.chat_parts import tui_model_menu
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
from agent_py_agent.tests.test_model_profiles import add
from agent_py_agent.tests.test_thread_model_selection import host_with_store
from agent_py_agent.tests.test_tui_decision_menu import visible, wait_app, wait_dialog_ready
from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params


# LLM: 只在模型配置读写线程里多等 delay 秒，返回值不变；_request_data 按调用时的模块属性取 _request_data_sync。
# 函数用途: 让模型菜单的读写变慢，证明测试按界面与落盘状态等待，而不是赌固定时长够用。
def slow_model_requests(monkeypatch, delay):
    if not delay:
        return
    original = tui_model_menu._request_data_sync

    def slow(*args, **kwargs):
        time.sleep(delay)
        return original(*args, **kwargs)

    monkeypatch.setattr(tui_model_menu, "_request_data_sync", slow)


@pytest.mark.parametrize("delay", [0.0, 0.5], ids=["fast", "slow_io"])
def test_tui_menu_add_select_cancel_and_secret_history(tmp_path, monkeypatch, delay):
    slow_model_requests(monkeypatch, delay)

    async def scenario():
        runtime = TuiRuntime("models")
        runtime.publish_session(version="0.3.0", model="deployment-model", workspace=str(tmp_path))
        params = _app_params(tmp_path, runtime)
        host = host_with_store(tmp_path)
        params.agent.home_paths = host.home_paths
        params.agent.config = host.config
        params.agent.conversation_store = host.conversation_store
        host.config.session_workspace = str(tmp_path / "sessions")
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                app = make_tui_app(params)
                runner = asyncio.create_task(app.run_async())
                try:
                    await wait_app(app, lambda: app.is_running, "TUI 启动")
                    pipe.send_text("/model\r")
                    await wait_dialog_ready(app, "模型配置 /model", "/model 打开模型菜单")
                    assert app._my_agent_model_menu_active
                    assert len(app._my_agent_model_float_container.floats) == 1
                    pipe.send_bytes(b"\x1b[B\r")  # 新增
                    await wait_dialog_ready(app, "新增模型 · 选择接口", "进入选择接口")
                    pipe.send_bytes(b"\x1b[B\r")  # Anthropic
                    await wait_dialog_ready(app, "新增模型 · 填写配置", "进入填写配置")
                    pipe.send_text("MiniMax-M2.7\thttps://example.test/anthropic\tonly-private-secret\t")
                    await wait_app(app, lambda: "only-private-secret" in visible(app), "三项配置都已填入")
                    pipe.send_bytes(b"\x01\x0b")
                    pipe.send_text("96000\t\r")  # 保存
                    # 保存在线程里确认成功后才回到主菜单并显示结果，所以看到“保存成功”时配置必须已经落盘。
                    await wait_dialog_ready(app, "保存成功。", "保存后回到模型菜单")
                    data = read_model_profiles(model_profiles_path(host.home_paths))
                    assert len(data["profiles"]) == 1
                    row = next(iter(data["profiles"].values()))
                    assert data["providers"][row["provider_id"]]["api_key"] == "only-private-secret"
                    assert row["model_context_window_tokens"] == 96000
                    assert runtime.store.snapshot().selected_model_name == "deployment-model"
                    pipe.send_bytes(b"\r")  # 选择已有模型
                    await wait_dialog_ready(app, "当前会话模型 · 不影响其他 TUI / IM 会话", "打开当前会话模型列表")
                    pipe.send_bytes(b"\x1b[B\r")
                    await wait_dialog_ready(app, "本会话已选择 MiniMax-M2.7", "选择后回到模型菜单")
                    data = read_model_profiles(model_profiles_path(host.home_paths))
                    assert data["selected"] == "default"
                    assert execute_local_model_operation(host, params.current_session_id, "list", {})["selected"] != "default"
                    assert runtime.store.snapshot().selected_model_name == "MiniMax-M2.7"
                    assert host.config.model_name == "deployment-model"
                    pipe.send_bytes(b"\x1b")
                    await wait_app(app, lambda: not app._my_agent_model_menu_active, "Esc 关闭模型菜单")
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
    from agent_py_agent.cli.chat_parts.tui_model_menu import refresh_model_selection

    host = host_with_store(tmp_path)
    profile_id, _ = add(host, model_name="qwen-test")
    execute_local_model_operation(host, "saved-choice", "select", {"profile_id": profile_id})
    source = host if not gateway else SimpleNamespace(
        gateway_client_only=True,
        request_models=lambda *, session_id, operation, payload: execute_local_model_operation(host, session_id, operation, payload),
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
    execute_local_model_operation(host, "saved-choice", "select", {"profile_id": "default"})
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


def test_revoked_selection_does_not_keep_misleading_deployment_banner():
    from agent_py_agent.cli.chat_parts.tui_model_menu import _publish_selection

    runtime = TuiRuntime("revoked")
    runtime.publish_model_selection("deployment-model")
    _publish_selection(runtime, {"ok": True, "selection_available": False, "warning": "原模型已撤销"})
    assert "不可用" in runtime.store.snapshot().selected_model_name
    assert "撤销" in runtime.notice()


def test_auth_action_refreshes_revoked_current_model(monkeypatch):
    from agent_py_agent.cli.chat_parts import tui_model_auth, tui_model_menu

    calls = []

    async def manage(app, agent, session):
        calls.append(("logout", session))
        return "已退出登录"

    async def request(app, agent, session, operation):
        calls.append((operation, session))
        return {"ok": True, "selected": "oauth", "selection_available": False, "warning": "账号已退出"}

    monkeypatch.setattr(tui_model_auth, "manage_auth", manage)
    monkeypatch.setattr(tui_model_menu, "_request", request)
    runtime = TuiRuntime("logout-selection")
    runtime.publish_model_selection("signed-in-model")
    result = asyncio.run(tui_model_menu._run_model_action(None, None, "logout-selection", runtime, "auth"))
    assert result == "已退出登录"
    assert calls == [("logout", "logout-selection"), ("list", "logout-selection")]
    assert "不可用" in runtime.store.snapshot().selected_model_name
    assert "退出" in runtime.notice()


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


def test_auth_escape_cancels_login_without_interrupting_agent(tmp_path, monkeypatch):
    from agent_py_agent.cli.chat_parts import tui_model_auth

    calls = []

    async def request(app, agent, session, operation, payload):
        calls.append(operation)
        return {"ok": True, "status": "pending", "attempt_id": "attempt", "interval": 60,
                "verification_uri": "https://example.test/device", "user_code": "TEST-CODE"}

    async def data(agent, session, operation, payload):
        calls.append(operation)
        assert payload["attempt_id"] == "attempt"
        return {"ok": True, "status": "signed_out"}

    monkeypatch.setattr(tui_model_auth, "_request", request)
    monkeypatch.setattr(tui_model_auth, "_request_data", data)

    async def scenario():
        runtime = TuiRuntime("auth-cancel")
        params = _app_params(tmp_path, runtime)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app = make_tui_app(params)
            runner = asyncio.create_task(app.run_async())
            try:
                await wait_app(app, lambda: app.is_running, "TUI 启动")
                login = asyncio.create_task(tui_model_auth._login(app, params.agent, "auth-cancel", "account"))
                await wait_dialog_ready(app, None, "登录对话框出现且可按键")
                pipe.send_bytes(b"\x1b")
                result = await asyncio.wait_for(login, 2)
                assert "已取消" in result and calls == ["auth_start", "auth_cancel"]
                assert not app._my_agent_model_float_container.floats
                assert params.jobs.empty() and not params.stop_event.is_set()
                assert all("TEST-CODE" not in block.text for block in runtime.store.snapshot().stable_blocks)
            finally:
                app.exit()
                await runner

    asyncio.run(scenario())
