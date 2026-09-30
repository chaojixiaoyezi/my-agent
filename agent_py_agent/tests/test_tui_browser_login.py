"""TUI 浏览器登录：本机回调只收本次登录、端口回退、不打印授权码；整条流程自动完成、可取消、端口被占退回验证码。"""
import asyncio
import socket
import threading
import urllib.error
import urllib.request

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_py_agent.cli.chat_parts import tui_browser_login, tui_model_auth
from agent_py_agent.cli.chat_parts.tui_browser_login import LoopbackCallback
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
from agent_py_agent.tests.test_tui_decision_menu import wait_app, wait_dialog_ready
from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params

STATE = "S" * 43
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def assert_closed(url):
    """监听已关：连接被拒，而不是连上后收到 HTTP 错误（HTTPError 也是 URLError 的子类）。"""
    with pytest.raises(urllib.error.URLError) as info:
        _LOCAL.open(url, timeout=2)
    assert not isinstance(info.value, urllib.error.HTTPError)


def get(url):
    try:
        with _LOCAL.open(url, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_callback_accepts_only_this_login_and_never_prints_the_code(capfd):
    callback = LoopbackCallback((0,))
    try:
        callback.expect(STATE)
        assert get(callback.redirect_uri.replace("/auth/callback", "/other"))[0] == 404
        assert get(f"{callback.redirect_uri}?code=forged&state=wrong")[0] == 400
        assert callback.wait(0.05) is None
        status, page = get(f"{callback.redirect_uri}?code=c-secret-1&state={STATE}")
        assert status == 200 and "登录已完成" in page and "c-secret-1" not in page
        assert callback.wait(1) == {"code": "c-secret-1", "state": STATE}
        assert get(f"{callback.redirect_uri}?code=later&state={STATE}")[0] == 200
        assert callback.wait(1)["code"] == "c-secret-1"
    finally:
        callback.close()
    captured = capfd.readouterr()
    assert "c-secret-1" not in captured.out + captured.err
    assert_closed(callback.redirect_uri)


def test_callback_reports_the_authorization_error():
    callback = LoopbackCallback((0,))
    try:
        callback.expect(STATE)
        assert get(f"{callback.redirect_uri}?error=access_denied&state={STATE}")[0] == 200
        assert callback.wait(1) == {"error": "access_denied"}
    finally:
        callback.close()


def test_busy_port_falls_back_and_all_busy_raises():
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    busy = holder.getsockname()[1]
    try:
        callback = LoopbackCallback((busy, 0))
        try:
            assert f":{busy}/" not in callback.redirect_uri
        finally:
            callback.close()
        with pytest.raises(OSError):
            LoopbackCallback((busy,))
    finally:
        holder.close()


def test_browser_login_is_not_offered_over_ssh_or_without_a_display(monkeypatch):
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 1 10.0.0.2 22")
    assert not tui_browser_login.browser_login_available()
    monkeypatch.delenv("SSH_CONNECTION")
    monkeypatch.setattr(tui_browser_login.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert not tui_browser_login.browser_login_available()
    monkeypatch.setenv("DISPLAY", ":0")
    assert tui_browser_login.browser_login_available()


def fake_gateway(monkeypatch, calls, *, browser_returns=True):
    """替身 Gateway 与替身浏览器：浏览器"登录完"就按回调地址跳回本机。"""
    seen = {}

    async def request(app, agent, session, operation, payload):
        calls.append(operation)
        if operation == "auth_browser_start":
            seen["redirect_uri"] = payload["redirect_uri"]
            return {"ok": True, "status": "pending", "attempt_id": "attempt",
                    "authorize_url": "https://auth.example.test/authorize", "state": STATE}
        return {"ok": True, "status": "pending", "attempt_id": "device", "interval": 60,
                "verification_uri": "https://example.test/device", "user_code": "DEVICE-CODE"}

    async def data(agent, session, operation, payload):
        calls.append(operation)
        seen.setdefault("payloads", []).append((operation, dict(payload)))
        return {"ok": True, "status": "connected"} if operation == "auth_browser_complete" else {"ok": True, "status": "signed_out"}

    def browser(url):
        if browser_returns and url.startswith("https://auth.example.test/"):
            target = f"{seen['redirect_uri']}?code=c-secret-2&state={STATE}"
            threading.Thread(target=get, args=(target,), daemon=True).start()
        return True

    for module in (tui_browser_login, tui_model_auth):
        monkeypatch.setattr(module, "_request", request)
        monkeypatch.setattr(module, "_request_data", data)
        monkeypatch.setattr(module, "open_browser", browser)
    monkeypatch.setattr(tui_model_auth, "browser_login_available", lambda: True)
    monkeypatch.setattr(tui_browser_login, "CALLBACK_PORTS", (0,))

    async def picker(app, agent, session, provider_id):
        calls.append("picker")
        return "（勾选模型）"

    monkeypatch.setattr(tui_model_auth, "pick_subscription_models", picker)
    return seen


def run_login(tmp_path, scenario):
    async def main():
        runtime = TuiRuntime("browser-login")
        params = _app_params(tmp_path, runtime)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app = make_tui_app(params)
            runner = asyncio.create_task(app.run_async())
            try:
                await wait_app(app, lambda: app.is_running, "TUI 启动")
                login = asyncio.create_task(tui_model_auth._sign_in(app, params.agent, "browser-login", ("account", "chatgpt")))
                result = await scenario(app, pipe, login)
                assert not app._my_agent_model_float_container.floats
                assert params.jobs.empty() and not params.stop_event.is_set()
                assert all("c-secret-2" not in block.text for block in runtime.store.snapshot().stable_blocks)
                return result
            finally:
                app.exit()
                await runner

    return asyncio.run(main())


def test_browser_login_completes_when_the_browser_comes_back(tmp_path, monkeypatch):
    calls = []
    seen = fake_gateway(monkeypatch, calls)

    async def scenario(app, pipe, login):
        return await asyncio.wait_for(login, 5)

    assert run_login(tmp_path, scenario) == "登录成功。（勾选模型）"
    assert calls == ["auth_browser_start", "auth_browser_complete", "auth_cancel", "picker"]
    complete = dict(seen["payloads"])["auth_browser_complete"]
    assert complete == {"provider_id": "account", "attempt_id": "attempt", "code": "c-secret-2", "state": STATE}


def test_escape_cancels_the_browser_login_and_closes_the_listener(tmp_path, monkeypatch):
    calls = []
    seen = fake_gateway(monkeypatch, calls, browser_returns=False)

    async def scenario(app, pipe, login):
        await wait_dialog_ready(app, None, "浏览器登录等待框出现")
        pipe.send_bytes(b"\x1b")
        return await asyncio.wait_for(login, 3)

    assert "已取消" in run_login(tmp_path, scenario)
    assert calls == ["auth_browser_start", "auth_cancel"]
    assert_closed(seen["redirect_uri"])


def test_busy_callback_ports_fall_back_to_the_device_code(tmp_path, monkeypatch):
    calls = []
    fake_gateway(monkeypatch, calls)

    def busy(ports):
        raise OSError("ports busy")

    monkeypatch.setattr(tui_browser_login, "LoopbackCallback", busy)

    async def scenario(app, pipe, login):
        await wait_dialog_ready(app, None, "验证码等待框出现")
        pipe.send_bytes(b"\x1b")
        return await asyncio.wait_for(login, 3)

    assert "已取消" in run_login(tmp_path, scenario)
    assert calls == ["auth_start", "auth_cancel"]


def test_device_login_success_for_chatgpt_also_opens_the_model_picker(tmp_path, monkeypatch):
    calls = []
    fake_gateway(monkeypatch, calls)
    monkeypatch.setattr(tui_model_auth, "browser_login_available", lambda: False)

    async def request(app, agent, session, operation, payload):
        calls.append(operation)
        return {"ok": True, "status": "pending", "attempt_id": "device", "interval": 1,
                "verification_uri": "https://example.test/device", "user_code": "DEVICE-CODE"}

    async def data(agent, session, operation, payload):
        calls.append(operation)
        return {"ok": True, "status": "connected"} if operation == "auth_poll" else {"ok": True, "status": "signed_out"}

    monkeypatch.setattr(tui_model_auth, "_request", request)
    monkeypatch.setattr(tui_model_auth, "_request_data", data)

    async def scenario(app, pipe, login):
        return await asyncio.wait_for(login, 6)

    assert run_login(tmp_path, scenario) == "登录成功。（勾选模型）"
    assert calls == ["auth_start", "auth_poll", "auth_cancel", "picker"]
