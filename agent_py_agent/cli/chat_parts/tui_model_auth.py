# LLM: 登录是现有 TUI 的私密模态表单，不进入聊天/文件历史；仅显示设备码，令牌始终留在 Gateway owner 配置。
# 模块用途: 在 /model 完成订阅登录、通用设备码参数设置、取消和退出，等待时不阻塞界面。
from __future__ import annotations

import asyncio
from uuid import uuid4

from prompt_toolkit.layout import HSplit
from prompt_toolkit.widgets import Checkbox, Label, RadioList, TextArea

from ...agent.settings.model_oauth_schema import CHATGPT_BASE, oauth_config
from .tui_model_menu import _dialog, _request, _request_data


# LLM: 结构化选项值控制动作，Esc 返回不发网络；不把认证菜单作为模型消息。
# 函数用途: 显示一个简短的认证动作选择框。
async def _choose(app, title: str, values: list[tuple[str, str]]):
    choices = RadioList(values, select_on_focus=True)
    return await _dialog(app, title, choices, (("进入", lambda: choices.current_value), ("返回", None)), focus=choices)


# LLM: 认证参数为显式用户配置；ChatGPT 端点固定，通用模式只支持服务商公开的 RFC 8628 接口。
# 函数用途: 创建一个未登录的服务商，不预填模型型号或容量，不调用模型。
async def _new_provider(app, agent, session: str) -> str:
    mode = await _choose(app, "登录类型", [("chatgpt", "ChatGPT 订阅登录（Plus / Pro 等账号）"),
        ("oauth_device", "通用 OAuth 2.0（设备码授权，填写服务商参数）")])
    if not mode:
        return ""
    key = TextArea(height=1, multiline=False, text="login-" + uuid4().hex[:8])
    name = TextArea(height=1, multiline=False, text="ChatGPT 订阅" if mode == "chatgpt" else "")
    base = TextArea(height=1, multiline=False, text=CHATGPT_BASE if mode == "chatgpt" else "", read_only=mode == "chatgpt")
    form = HSplit([Label("Provider ID（本人目录内唯一）"), key, Label("展示名"), name,
                   Label("模型 API 基础地址（ChatGPT 使用固定订阅接口）"), base])
    if not await _dialog(app, "认证服务商 · 基本信息", form, (("下一步", True), ("取消", None)), focus=key):
        return ""
    config = {"mode": mode}
    if mode == "oauth_device":
        config = await _generic_parameters(app, base.text)
        if not config:
            return ""
        config.pop("clear_client_secret", None)
    result = await _request(app, agent, session, "save_provider", {"provider_id": key.text, "provider": {
        "display_name": name.text, "api_base": base.text, "api_key": "", "auth": config}})
    if not result.get("ok"):
        await _dialog(app, "未保存", Label(str(result.get("message") or "无法保存认证配置。")), (("返回", None),))
        return ""
    return key.text


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


# LLM: 只在此模态页存续期间轮询，使用上游 interval；取消后 CAS 撤销，迟到响应不能恢复登录。
# 函数用途: 展示网页登录地址和验证码，自动等待结果，Esc 退出不会停止代理任务。
async def _login(app, agent, session: str, provider_id: str) -> str:
    result = await _request(app, agent, session, "auth_start", {"provider_id": provider_id})
    if not result.get("ok"):
        return str(result.get("message") or "发起登录失败。")
    completion = asyncio.get_running_loop().create_future()
    label = Label("等待网页确认…")
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
        return "登录成功。请添加该账号可用的模型、接口和上下文容量，再选择使用；尚未发送模型请求。"
    return str((final or {}).get("message") or "已取消本次登录；此前有效登录未被清除。")


# LLM: 账号目录与已有 provider 同源；重新登录和退出须显式动作，不以菜单关闭触发 logout。
# 函数用途: 管理当前用户的登录账号，并复用已有模型编辑器。
async def manage_auth(app, agent, session: str, *, create: bool = False) -> str:
    from .tui_provider_menu import _edit_model

    result = await _request(app, agent, session, "list")
    if not result.get("ok"):
        return str(result.get("message") or "无法读取登录配置。")
    rows = [row for row in result.get("providers", []) if row.get("auth_mode") in {"chatgpt", "oauth_device"}]
    provider = "new" if create else await _choose(app, "登录认证（本人账号）", [("new", "新增登录服务商"), *[
        (row["id"], f"{row['display_name']} · {'已登录' if row['signed_in'] else '未登录'}") for row in rows]])
    if not provider:
        return ""
    if provider == "new":
        provider = await _new_provider(app, agent, session)
        return await _login(app, agent, session, provider) if provider else ""
    row = next(row for row in rows if row["id"] == provider)
    choices = [("login", "登录 / 重新登录"), ("model", "添加此账号的模型")]
    if row["auth_mode"] == "oauth_device":
        choices.append(("edit", "编辑认证参数（变化后需重新登录）"))
    choices.append(("logout", "退出登录（后续请求停止使用此凭据）"))
    action = await _choose(app, "账号操作", choices)
    if action == "login":
        return await _login(app, agent, session, provider)
    if action == "model":
        return await _edit_model(app, agent, session, provider,
            {"model_backend": "openai_responses"} if row["auth_mode"] == "chatgpt" else None)
    if action == "edit":
        return await _edit_parameters(app, agent, session, row)
    if action == "logout" and await _dialog(app, "确认退出登录", Label("将删除本用户保存的访问/刷新令牌。\n不会退出其他应用或其他用户；已经发出的请求无法撤回。"),
                                              (("退出账号", True), ("取消", None))):
        result = await _request(app, agent, session, "auth_logout", {"provider_id": provider})
        return "已退出登录；再次使用需重新授权。" if result.get("ok") else str(result.get("message") or "退出未确认。")
    return ""
