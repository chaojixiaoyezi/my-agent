# LLM: 登录是现有 TUI 的私密模态表单，不进入聊天/文件历史；只显示设备码或浏览器提示，令牌始终留在 Gateway owner 配置。
#   ChatGPT 订阅默认走 tui_browser_login 的浏览器登录，环境不适合或端口被占时退回设备码。新增账号入口在「新增模型 →
#   登录账号」（add_account），已有账号在「管理已有模型」里（manage_account）。
# 模块用途: 在 /model 完成订阅登录、通用设备码参数设置、取消和退出，等待时不阻塞界面。
from __future__ import annotations

import asyncio
from uuid import uuid4

from prompt_toolkit.layout import HSplit
from prompt_toolkit.widgets import Checkbox, Label, RadioList, TextArea

from ...agent.settings.model_oauth_schema import CHATGPT_BASE, oauth_config
from .tui_browser_login import LoginOutcome, browser_login, browser_login_available, open_browser
from .tui_model_menu import _dialog, _request, _request_data
from .tui_subscription_models import pick_subscription_models


# LLM: 结构化选项值控制动作，Esc 返回不发网络；不把认证菜单作为模型消息。
# 函数用途: 显示一个简短的认证动作选择框。
async def _choose(app, title: str, values: list[tuple[str, str]]):
    choices = RadioList(values, select_on_focus=True)
    return await _dialog(app, title, choices, (("进入", lambda: choices.current_value), ("返回", None)), focus=choices)


# LLM: 认证参数为显式用户配置；ChatGPT 端点、展示名固定，不再让用户填编号或地址；通用模式只支持服务商公开的
#   RFC 8628 接口。服务商编号自动生成 login-<8位>。返回 (服务商编号, 登录类型)，取消或保存失败返回两个空串。
# 函数用途: 创建一个未登录的账号服务商，不预填模型型号或容量，不调用模型。
async def _new_provider(app, agent, session: str) -> tuple[str, str]:
    mode = await _choose(app, "登录类型", [("chatgpt", "ChatGPT 订阅登录（Plus / Pro 等账号）"),
        ("oauth_device", "通用 OAuth 2.0（设备码授权，填写服务商参数）")])
    if not mode:
        return "", ""
    provider = ({"display_name": "ChatGPT 订阅", "api_base": CHATGPT_BASE, "api_key": "", "auth": {"mode": "chatgpt"}}
                if mode == "chatgpt" else await _generic_provider(app))
    if not provider:
        return "", ""
    key = "login-" + uuid4().hex[:8]
    result = await _request(app, agent, session, "save_provider", {"provider_id": key, "provider": provider})
    if not result.get("ok"):
        await _dialog(app, "未保存", Label(str(result.get("message") or "无法保存认证配置。")), (("返回", None),))
        return "", ""
    return key, mode


# LLM: 只收集展示名、模型 API 地址与设备码参数；Client Secret 由 _generic_parameters 掩码处理，不回显。
# 函数用途: 通用 OAuth 账号的基本信息与授权参数表单，取消返回 None。
async def _generic_provider(app) -> dict | None:
    name, base = TextArea(height=1, multiline=False), TextArea(height=1, multiline=False)
    form = HSplit([Label("展示名"), name, Label("模型 API 基础地址"), base])
    if not await _dialog(app, "通用 OAuth · 基本信息", form, (("下一步", True), ("取消", None)), focus=name):
        return None
    config = await _generic_parameters(app, base.text)
    if not config:
        return None
    config.pop("clear_client_secret", None)
    return {"display_name": name.text, "api_base": base.text, "api_key": "", "auth": config}


# LLM: 仅回显无秘密配置；Client Secret 掩码且留空保留，显式勾选才清除，不进入本机历史。
# 函数用途: 新建或修改设备码参数；保存认证变化会撤销旧登录，不支持设备码的网站不能使用此方式。
async def _generic_parameters(app, base: str, existing: dict | None = None) -> dict:
    fields = {name: TextArea(height=1, multiline=False, password=name == "client_secret", text=(existing or {}).get(name, "")) for name in
              ("client_id", "device_url", "token_url", "scope", "audience", "client_secret")}
    labels = ("Client ID（服务商签发）", "Device Authorization URL", "Token URL", "Scope（空格分隔，可留空）",
              "Audience（可留空）", "Client Secret（编辑时留空保留；公共客户端无需填写）")
    clear = Checkbox("清除已保存的 Client Secret")
    notice = Label("服务商须支持设备码授权及 Bearer 模型接口；不要填写网页密码。")
    body = HSplit([notice, *[widget for label, field in zip(labels, fields.values()) for widget in (Label(label), field)], clear])
    try:
        while await _dialog(app, "通用 OAuth · 服务商参数", body, (("保存参数", True), ("取消", None)), focus=fields["client_id"]):
            try:
                config = oauth_config({"mode": "oauth_device", **{name: field.text for name, field in fields.items()}}, base)
                return {**config, "clear_client_secret": clear.checked}
            except ValueError as exc:
                notice.text = str(exc)
    finally:
        fields["client_secret"].text = ""
    return {}


# LLM: 回显只读公开参数，不取出 secret；修改成功清旧授权，取消无写入，沿唯一 provider 保存入口。
# 函数用途: 修正已有通用登录服务商的端点、Client ID 和 Scope，再由用户重新授权。
async def _edit_parameters(app, agent, session: str, provider: dict) -> str:
    result = await _request(app, agent, session, "auth_parameters", {"provider_id": provider["id"]})
    if not result.get("ok"):
        return str(result.get("message") or "无法读取认证参数。")
    config = await _generic_parameters(app, provider["api_base"], result["parameters"])
    if not config:
        return ""
    result = await _request(app, agent, session, "save_provider", {"provider_id": provider["id"], "editing": True,
        "clear_auth_secret": config.pop("clear_client_secret"), "provider": {"auth": config}})
    return "参数已保存；认证参数变化后需重新登录，未自动切换模型。" if result.get("ok") else str(result.get("message") or "未保存。")


# LLM: target 是 (服务商编号, 登录类型)。ChatGPT 订阅先走浏览器登录（不需复制），环境不适合、端口被占或用户选择时
#   再走设备码；通用 OAuth 只走设备码。ChatGPT 登录成功（按 LoginOutcome.connected）后直接弹出账号可用模型的勾选框。
# 函数用途: 按登录类型选择浏览器或设备码方式发起登录，返回给用户看的结果文字。
async def _sign_in(app, agent, session: str, target: tuple[str, str]) -> str:
    provider_id, mode = target
    outcome = LoginOutcome("", use_device=True)
    if mode == "chatgpt" and browser_login_available():
        outcome = await browser_login(app, agent, session, provider_id)
    if outcome.use_device:
        outcome = await _device_login(app, agent, session, provider_id)
    if outcome.connected and mode == "chatgpt":
        return "登录成功。" + await pick_subscription_models(app, agent, session, provider_id)
    return outcome.message


# LLM: 保留原签名给既有调用方；只取设备码登录结果里给用户看的文字。
# 函数用途: 设备码登录，返回结果文字。
async def _login(app, agent, session: str, provider_id: str) -> str:
    return (await _device_login(app, agent, session, provider_id)).message


# LLM: 只在此模态页存续期间轮询，使用上游 interval；取消后 CAS 撤销，迟到响应不能恢复登录。
# 函数用途: 设备码登录：自动打开确认页并显示验证码，自动等待结果，Esc 退出不会停止代理任务。
async def _device_login(app, agent, session: str, provider_id: str) -> LoginOutcome:
    result = await _request(app, agent, session, "auth_start", {"provider_id": provider_id})
    if not result.get("ok"):
        return LoginOutcome(str(result.get("message") or "发起登录失败。"))
    completion = asyncio.get_running_loop().create_future()
    opened = browser_login_available() and await asyncio.to_thread(open_browser, result["verification_uri"])
    label = Label("已在浏览器打开确认页，请输入下面的验证码并确认…" if opened else "等待网页确认…")
    code = TextArea(text=f"{result['verification_uri']}\n\n验证码：{result['user_code']}", read_only=True, height=4, width=72)
    attempt = {"provider_id": provider_id, "attempt_id": result["attempt_id"]}

    # LLM: 后台仅等待授权状态，不生成模型回复；每次网络交互移到原线程池，主事件循环保持可响应。
    # 函数用途: 按授权服务器间隔检查完成状态并关闭等待框。
    async def watch():
        interval = result.get("interval", 5)
        try:
            while not completion.done():
                await asyncio.sleep(max(1, interval))
                status = await _request_data(agent, session, "auth_poll", attempt)
                if completion.done():
                    return
                if not status.get("ok") or status.get("status") != "pending":
                    completion.set_result(status)
                    return
                interval = status.get("interval", interval)
                label.text = "仍在等待网页确认 · 可随时取消，不影响正在运行的任务"
                app.invalidate()
        except asyncio.CancelledError:
            raise
        except Exception:
            if not completion.done():
                completion.set_result({"ok": False, "message": "登录等待出错，请重新打开认证菜单。"})

    watcher = asyncio.create_task(watch())
    try:
        final = await _dialog(app, "请在服务商网页登录并确认", HSplit([code, label]),
                              (("取消登录", None),), focus=code, completion=completion)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        await _request_data(agent, session, "auth_cancel", attempt)
        code.text = ""
    if final and final.get("ok") and final.get("status") == "connected":
        return LoginOutcome("登录成功。在 /model →「管理已有模型」→ 这个账号 →「添加此账号的模型」里添加可用的模型，"
                            "再到「选择模型」里选用；尚未发送模型请求。", connected=True)
    return LoginOutcome(str((final or {}).get("message") or "已取消本次登录；此前有效登录未被清除。"))


# LLM: 新账号先保存未登录的服务商再发起登录；ChatGPT 登录成功后直接弹出勾选框（见 _sign_in）。
# 函数用途: 「新增模型 → 登录账号」：选登录类型、登录，然后勾选这个账号能用的模型。
async def add_account(app, agent, session: str) -> str:
    provider, mode = await _new_provider(app, agent, session)
    return await _sign_in(app, agent, session, (provider, mode)) if provider else ""


# LLM: row 来自同一次 list 投影的公开服务商行；重新登录、退出和删除都须显式动作，不以菜单关闭触发 logout。
#   模型的编辑/删除复用 tui_provider_menu 的同一入口；删除账号前须先删掉其下模型（服务端强制）。
# 函数用途: 「管理已有模型」里选中一个登录账号后的操作：登录、添加模型、编辑或删除模型、改参数、退出、删除账号。
async def manage_account(app, agent, session: str, row: dict) -> str:
    from .tui_provider_menu import _delete, _edit_model, manage_provider_models

    provider, chatgpt = row["id"], row["auth_mode"] == "chatgpt"
    choices = [("login", "登录 / 重新登录"), ("model", "添加模型（勾选这个账号能用的模型）" if chatgpt else "添加此账号的模型"),
               ("models", "编辑或删除这个账号下的模型")]
    if row["auth_mode"] == "oauth_device":
        choices.append(("edit", "编辑认证参数（变化后需重新登录）"))
    choices += [("logout", "退出登录（后续请求停止使用此凭据）"), ("delete", "删除这个账号（需先删掉其下模型）")]
    action = await _choose(app, f"账号操作 · {row['display_name']}", choices)
    if action == "login":
        return await _sign_in(app, agent, session, (provider, row["auth_mode"]))
    if action == "model":
        return await (pick_subscription_models(app, agent, session, provider) if chatgpt
                      else _edit_model(app, agent, session, provider, None))
    if action == "models":
        return await manage_provider_models(app, agent, session, provider)
    if action == "edit":
        return await _edit_parameters(app, agent, session, row)
    if action == "delete":
        return await _delete(app, agent, session, "delete_provider", provider)
    return await _logout(app, agent, session, provider) if action == "logout" else ""


# LLM: 退出只删除本用户保存的令牌，需二次确认；取消无副作用。
# 函数用途: 确认后退出一个账号的登录。
async def _logout(app, agent, session: str, provider: str) -> str:
    if not await _dialog(app, "确认退出登录", Label("将删除本用户保存的访问/刷新令牌。\n不会退出其他应用或其他用户；已经发出的请求无法撤回。"),
                         (("退出账号", True), ("取消", None))):
        return ""
    result = await _request(app, agent, session, "auth_logout", {"provider_id": provider})
    return "已退出登录；再次使用需重新授权。" if result.get("ok") else str(result.get("message") or "退出未确认。")
