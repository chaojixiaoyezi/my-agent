# LLM: 本机回调只在登录模态页存续期间监听 127.0.0.1 的登记端口，只收与本次 state 一致的回调；授权码只转交 Gateway 兑换，
#   不写终端、日志、聊天或本机历史。远程登录（SSH）或没有图形界面时不走这条路，退回验证码登录。
#   改动须同步 test_tui_browser_login 与 docs/design/MODEL_OAUTH.md。
# 模块用途: 像官方命令行一样"点登录 → 自动打开浏览器 → 登录完自动回到程序"，给 TUI 的 ChatGPT 订阅登录用。
from __future__ import annotations

import asyncio
import html
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NamedTuple
from urllib.parse import parse_qs, urlsplit

from prompt_toolkit.layout import HSplit
from prompt_toolkit.widgets import Label

from ...agent.settings.model_oauth_schema import CHATGPT_CALLBACK_PORTS
from ...agent.settings.model_oauth_wire import BROWSER_LOGIN_SECONDS
from .tui_model_menu import _dialog, _request, _request_data

USE_DEVICE_CODE = "use_device_code"
# 回调端口按顺序尝试；只能是授权服务器登记过的端口，测试可以替换成临时端口。
CALLBACK_PORTS = CHATGPT_CALLBACK_PORTS
_PAGE = ("<!doctype html><html lang=zh><meta charset=utf-8><title>my-agent 登录</title>"
         "<body style='font-family:sans-serif;margin:3em'><p>{}</p></body></html>")
_ACTIONS = (("重新打开浏览器", "reopen"), ("改用验证码登录", USE_DEVICE_CODE), ("取消登录", None))


# LLM: 调用方只按 connected / use_device 两个结构化字段决定下一步，不解析 message 文字。
# 类用途: 一次登录的结果：给用户看的文字、是否已登录、是否要改走验证码登录。
class LoginOutcome(NamedTuple):
    message: str
    connected: bool = False
    use_device: bool = False


# LLM: 纯判定，只看路径与查询参数的结构化字段；state 不符不产生结果（不结束等待），伪造请求打断不了真实登录。
# 函数用途: 决定本机回调该回什么页面，以及要不要把授权码或授权错误交给等待方。
def _callback_outcome(path: str, params: dict, expected_state: str) -> tuple[int, str, dict | None]:
    if path != "/auth/callback":
        return 404, "找不到这个页面。", None
    if not expected_state or params.get("state") != expected_state:
        return 400, "这个登录回调不属于当前的登录，已忽略。请回到 my-agent 重新登录。", None
    if params.get("error"):
        return 200, "ChatGPT 没有完成授权。可以关闭此页，回到 my-agent 重试。", {"error": params["error"][:100]}
    return 200, "登录已完成，可以关闭此页面，回到 my-agent。", {"code": params.get("code", ""), "state": params["state"]}


# LLM: 不覆写 log_message 会把带授权码的请求行打到终端；响应只含固定中文提示，不回显任何参数。
# 类用途: 处理浏览器登录完成后跳回本机的那一次请求。
class _CallbackHandler(BaseHTTPRequestHandler):
    # LLM: 一次登录只接受第一个有效回调（由 LoopbackCallback.finish 保证）；判定逻辑在 _callback_outcome。
    # 函数用途: 收下授权码或授权错误，并给浏览器回一个中文提示页。
    def do_GET(self) -> None:  # noqa: N802 标准库回调名
        parts = urlsplit(self.path)
        params = {key: values[0] for key, values in parse_qs(parts.query).items() if values}
        status, text, result = _callback_outcome(parts.path, params, self.server.login.expected_state)
        if result is not None:
            self.server.login.finish(result)
        body = _PAGE.format(html.escape(text)).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    # LLM: 请求行带授权码，一律不打印；标准库默认会写到 stderr，破坏 TUI 画面并泄漏授权码。
    # 函数用途: 关闭标准库的访问日志。
    def log_message(self, format: str, *args) -> None:  # noqa: A002 标准库签名
        return


# LLM: 只绑定 127.0.0.1 的登记端口，占用就换下一个，都占用抛 OSError 由调用方退回验证码登录；不去关别的程序的监听。
# 类用途: 登录期间在本机开一个只收一次登录结果的小服务，结束或取消时关掉。
class LoopbackCallback:
    # LLM: 构造即开始监听（后台守护线程）；调用方必须在 finally 里 close()。
    # 函数用途: 绑定第一个可用的登记端口并开始监听。
    def __init__(self, ports: tuple[int, ...]) -> None:
        self.expected_state = ""
        self._result: dict | None = None
        self._done = threading.Event()
        self._server = _bind(ports)
        self._server.login = self
        self.redirect_uri = f"http://127.0.0.1:{self._server.server_address[1]}/auth/callback"
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.2},
                         name="chatgpt-login-callback", daemon=True).start()

    # LLM: state 来自 Gateway 开始登录的回执；设置前到达的回调一律按不匹配处理。
    # 函数用途: 记下本次登录应带回的 state。
    def expect(self, state: str) -> None:
        self.expected_state = state

    # LLM: 只记第一次结果；close() 用 None 结束等待，让等待线程及时退出。
    # 函数用途: 结束等待并保存结果。
    def finish(self, result: dict | None) -> None:
        if not self._done.is_set():
            self._result = result
            self._done.set()

    # LLM: 阻塞调用，只能放在线程里；超时或被关闭返回 None。
    # 函数用途: 等浏览器跳回来，返回授权码或错误。
    def wait(self, timeout: float) -> dict | None:
        self._done.wait(timeout)
        return self._result

    # LLM: 可重复调用；shutdown 会等监听循环退出，所以要放在线程里调，不能卡住界面。
    # 函数用途: 关掉本机监听并结束等待。
    def close(self) -> None:
        self.finish(None)
        self._server.shutdown()
        self._server.server_close()


# LLM: 端口顺序即优先级；SO_REUSEADDR 只帮助刚关闭的端口，不会和正在监听的程序共用端口。
# 函数用途: 在登记端口里找一个能绑定的，都不行就抛 OSError。
def _bind(ports: tuple[int, ...]) -> ThreadingHTTPServer:
    error: OSError | None = None
    for port in ports:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), _CallbackHandler)
        except OSError as exc:
            error = exc
            continue
        server.daemon_threads = True
        return server
    raise error or OSError("没有可用的本机回调端口")


# LLM: 只按环境事实判断：SSH 会话里浏览器和回调不在同一台机器；Linux 没有图形会话时 webbrowser 会拉起终端浏览器占住 TUI。
# 函数用途: 判断当前能不能用"自动打开浏览器"的登录方式。
def browser_login_available() -> bool:
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


# LLM: 系统默认浏览器打开，失败只返回 False，不抛到界面；调用方放在线程里调用。
# 函数用途: 用默认浏览器打开登录页。
def open_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:  # noqa: BLE001 打不开浏览器由界面提示重试或改用验证码
        return False


# LLM: 开始登录 → 打开浏览器 → 等回调 → 交给 Gateway 兑换；取消、超时、改用验证码都会关监听并撤销本次等待状态。
#   返回 LoginOutcome：connected 表示已登录，use_device 表示调用方应改走验证码登录。
# 函数用途: TUI 里 ChatGPT 订阅的"浏览器登录"，登录中不需要用户复制任何东西。
async def browser_login(app, agent, session: str, provider_id: str) -> LoginOutcome:
    try:
        callback = await asyncio.to_thread(LoopbackCallback, CALLBACK_PORTS)
    except OSError:
        return LoginOutcome("", use_device=True)
    flow = _BrowserLoginFlow(app, agent, session, callback)
    try:
        final = await flow.run(provider_id)
    finally:
        await asyncio.to_thread(callback.close)
        if flow.attempt:
            await _request_data(agent, session, "auth_cancel", flow.attempt)
    if final == USE_DEVICE_CODE:
        return LoginOutcome("", use_device=True)
    if isinstance(final, dict) and final.get("ok") and final.get("status") == "connected":
        return LoginOutcome("登录成功。", connected=True)
    return LoginOutcome(str((final or {}).get("message") or "已取消本次登录；此前有效登录未被清除。"))


# LLM: 一次浏览器登录的界面与等待状态；按钮会结算传入对话框的 future，所以每次显示都用新 future，由后台结果转发，
#   "重新打开浏览器"不结束登录。授权码只作为请求参数交给 Gateway，不留在对象里。
# 类用途: 串起"开始登录、打开浏览器、显示等待框、收到回调后兑换"几步。
class _BrowserLoginFlow:
    # LLM: attempt 在 Gateway 开始登录成功后才有值，调用方据此决定是否撤销等待状态。
    # 函数用途: 保存界面、会话和本机回调，准备一次登录。
    def __init__(self, app, agent, session: str, callback: LoopbackCallback) -> None:
        self.app, self.agent, self.session, self.callback = app, agent, session, callback
        self.attempt: dict = {}

    # LLM: 失败回执原样返回给调用方展示；成功后等待框返回 Gateway 的结构化回执或用户的按钮选择。
    # 函数用途: 请 Gateway 开始登录，打开浏览器并等待结果。
    async def run(self, provider_id: str):
        started = await _request(self.app, self.agent, self.session, "auth_browser_start",
                                 {"provider_id": provider_id, "redirect_uri": self.callback.redirect_uri})
        if not started.get("ok"):
            return {"ok": False, "message": str(started.get("message") or "发起浏览器登录失败。")}
        self.attempt = {"provider_id": provider_id, "attempt_id": started["attempt_id"]}
        self.callback.expect(started["state"])
        outcome = asyncio.create_task(self._complete_after_callback())
        try:
            opened = await asyncio.to_thread(open_browser, started["authorize_url"])
            label = Label(("已经在浏览器里打开 ChatGPT 登录页。" if opened else "没能自动打开浏览器，请点「重新打开浏览器」。")
                          + "\n在网页上登录并同意授权，完成后这里会自动继续，不用复制任何东西。")
            choice = await self._show_once(outcome, label)
            while choice == "reopen":
                await asyncio.to_thread(open_browser, started["authorize_url"])
                choice = await self._show_once(outcome, label)
            return choice
        finally:
            outcome.cancel()
            await asyncio.gather(outcome, return_exceptions=True)

    # LLM: 每次显示用新的 future；后台结果完成时转交给正在显示的这一次，显示结束后解除转交。
    # 函数用途: 显示一次等待框，返回按钮选择或后台登录结果。
    async def _show_once(self, outcome: asyncio.Task, label: Label):
        shown = asyncio.get_running_loop().create_future()

        def forward(task: asyncio.Task) -> None:
            if not shown.done() and not task.cancelled():
                shown.set_result(task.result())

        outcome.add_done_callback(forward)
        try:
            return await _dialog(self.app, "用浏览器登录 ChatGPT", HSplit([label]), _ACTIONS, completion=shown)
        finally:
            outcome.remove_done_callback(forward)

    # LLM: 等待在线程里进行，关闭监听即可让它返回；异常只给可重试提示，不泄漏授权码。
    # 函数用途: 等浏览器跳回来后请 Gateway 兑换令牌，返回 Gateway 的结构化回执。
    async def _complete_after_callback(self) -> dict:
        try:
            result = await asyncio.to_thread(self.callback.wait, BROWSER_LOGIN_SECONDS)
            if not result:
                return {"ok": False, "message": "登录等待已结束，请重新登录。"}
            if result.get("error"):
                return {"ok": False, "message": f"ChatGPT 没有完成授权（{result['error']}），可以重新登录。"}
            return await _request_data(self.agent, self.session, "auth_browser_complete",
                                       {**self.attempt, "code": result["code"], "state": result["state"]})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 网络或 Gateway 异常只给出可重试提示，不泄漏授权码
            return {"ok": False, "message": "登录完成后兑换凭据出错，请重新登录。"}
