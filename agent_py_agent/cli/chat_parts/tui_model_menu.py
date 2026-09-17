# LLM: /model 是本机表单，不进入聊天、LLM 或 FileHistory；网络与文件操作在线程中执行，密钥只在掩码控件短暂保留。
# 模块用途: 在现有 TUI 管理服务商和模型、主动测试连接，并在选择确认后刷新欢迎区；不热改运行配置。

from __future__ import annotations

import asyncio
from uuid import uuid4

from prompt_toolkit.filters import has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Float, HSplit
from prompt_toolkit.widgets import Button, Dialog, Label, RadioList, TextArea

from ...agent.settings.model_profiles import (
    ModelProfileError,
    validate_model_profile,
)
from ...agent.settings.thread_model_selection import execute_local_model_operation


# LLM: modal 只占现有浮层；外部 Future 仅供认证状态完成，Esc 只取消表单、不停止代理。
# 函数用途: 显示一个可返回结果的对话框，关闭时恢复此前输入焦点，不销毁聊天界面。
async def _dialog(app, title: str, body, actions: tuple, *, focus=None, completion=None):
    future = completion if completion is not None else asyncio.get_running_loop().create_future()
    bindings = KeyBindings()

    # LLM: Future 只结算一次；返回值是按钮显式提供的动作，不解析标签。
    # 函数用途: 完成对话框选择。
    def finish(value=None):
        if not future.done():
            future.set_result(value)

    # LLM: Esc/Control-C 只取消当前模型表单，不能冒充任务控制事件。
    # 函数用途: 允许用户随时退出尚未提交的表单。
    @bindings.add("escape")
    @bindings.add("c-c")
    def cancel(event):
        finish()

    if isinstance(focus, RadioList):
        # LLM: Enter 根据当前结构化选项提交，不要求额外按空格，也不把文字当命令。
        # 函数用途: 让上下键选择后直接回车进入或保存。
        @bindings.add("enter", filter=has_focus(focus), eager=True)
        def accept_choice(event):
            value = actions[0][1]
            finish(value() if callable(value) else value)

    buttons = [Button(label, handler=lambda value=value: finish(value() if callable(value) else value)) for label, value in actions]
    dialog = Dialog(title=title, body=body, buttons=buttons, with_background=True, modal=False)
    floating = Float(content=HSplit([dialog], key_bindings=bindings, modal=True))
    host = app._my_agent_model_float_container
    previous = app.layout.current_window
    host.floats.append(floating)
    app.layout.focus(focus or buttons[0])
    app.invalidate()
    try:
        return await future
    finally:
        host.floats.remove(floating)
        app.layout.focus(previous)
        app.invalidate()


# LLM: 请求参数通过单独payload映射传递；走现有owner认证，保存重试复用profile_id，未知结果不自动另建记录。
# 函数用途: 不阻塞 TUI 绘制地管理配置或执行用户明确选择的连接检查，等待提示区分网络与纯配置操作。
async def _request(app, agent, session_id: str, operation: str, payload: dict[str, object] | None = None) -> dict:
    text = ("正在发送短问候，等待模型回复…" if operation == "probe" else "正在读取模型目录…" if operation == "discover"
            else "正在联系认证服务…（不会调用模型）" if operation.startswith("auth_")
            else "正在等待配置操作确认…（不会向模型发请求）")
    waiting = TextArea(text=text, read_only=True, height=2, width=64)
    busy = Float(content=Dialog(title="模型配置", body=waiting, buttons=[], with_background=True))
    host = app._my_agent_model_float_container
    previous = app.layout.current_window
    host.floats.append(busy)
    app.layout.focus(waiting)
    app.invalidate()
    try:
        return await _request_data(agent, session_id, operation, payload or {})
    finally:
        host.floats.remove(busy)
        app.layout.focus(previous)
        app.invalidate()


# LLM: 文件/网络操作离开事件线程；只有固定 ModelProfileError 文案可公开，未知异常不得带出秘密。
# 函数用途: 获取模型配置操作的确认结果，并安全处理可预期错误。
async def _request_data(agent, session_id: str, operation: str, payload: dict) -> dict:
    return await asyncio.to_thread(_request_data_sync, agent, session_id, operation, payload)


# LLM: 菜单与启动共用原配置入口，Gateway只传显式payload映射；必须由后台线程调用，异常不泄漏秘密。
# 函数用途: 读取或保存用户模型配置；Gateway 模式发认证请求，本地模式访问私有配置文件。
def _request_data_sync(agent, session_id: str, operation: str, payload: dict) -> dict:
    try:
        if getattr(agent, "gateway_client_only", False) is True:
            return agent.request_models(session_id=session_id, operation=operation, payload=payload)
        return execute_local_model_operation(agent, session_id, operation, payload)
    except ModelProfileError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception:  # noqa: BLE001 不显示带有密钥或路径的未知异常
        return {"ok": False, "message": "模型配置操作失败，请检查本机配置目录是否可写。"}


# LLM: 只认成功回执里的 selected ID；未配置明确显示空状态，不用部署占位值，保存未选择不自动切换。
# 函数用途: 从脱敏模型列表更新 TUI，整个配置和密钥都不进入显示事件。
def _publish_selection(runtime, result: dict) -> None:
    if result.get("ok") is True:
        if result.get("selection_available") is False:
            runtime.publish_model_selection("未配置模型（/model 配置）" if result.get("selected") == "default"
                                            else "当前模型不可用（/model 重新选择）")
            runtime.set_notice(str(result.get("warning") or "当前模型不可用，请重新选择。"), duration_seconds=8)
            return
        row = next((row for row in result["profiles"] if row["id"] == result["selected"]), None)
        if row is not None:
            runtime.publish_model_selection(row["model_name"])
        if result.get("warning"):
            runtime.set_notice(str(result["warning"]), duration_seconds=8)


# LLM: 启动读取在 Gateway readiness 后的后台执行；退出后的迟到结果不发布，也不阻止正常历史恢复。
# 函数用途: 新开或恢复 TUI 时读取已保存模型名；失败明确提示，不凭启动默认值声称同步成功。
def refresh_model_selection(agent, session_id: str, runtime, stop_event=None) -> None:
    result = _request_data_sync(agent, session_id, "list", {})
    if stop_event is not None and stop_event.is_set():
        return
    _publish_selection(runtime, result)
    if not result.get("ok"):
        runtime.set_notice("当前模型名称尚未同步，可打开 /model 重新确认。", duration_seconds=6)


# LLM: 协议与认证分开选择；快捷新增可进入 Auth，已有 provider 的模型编辑不重复创建账号。
# 函数用途: 选择新模型的接口类型。
async def _choose_interface(app, *, default=None, allow_auth=True):
    values = [
        ("openai_compatible", "OpenAI 风格（Chat Completions）"),
        ("anthropic_compatible", "Anthropic 风格（Messages）"),
        ("openai_responses", "OpenAI Responses（响应 / 工具流）"),
    ]
    if allow_auth:
        values.append(("auth", "Auth 登录（ChatGPT 订阅 / 通用 OAuth）"))
    choices = RadioList(values, default=default, select_on_focus=True)
    return await _dialog(app, "新增模型 · 选择接口", choices,
                         (("下一步", lambda: choices.current_value), ("返回", None)), focus=choices)


# LLM: 表单不使用持久 history；密码控件掩码，校验仅检查配置格式，保存不自动启用或测试接口。
# 函数用途: 填写模型名、基础地址、密钥及总上下文窗口，保存成功后返回主菜单。
async def _add_model(app, agent, session_id: str) -> str:
    backend = await _choose_interface(app)
    if backend is None:
        return ""
    if backend == "auth":
        from .tui_model_auth import manage_auth

        return await manage_auth(app, agent, session_id, create=True)
    name = TextArea(height=1, multiline=False)
    address = TextArea(height=1, multiline=False)
    key = TextArea(height=1, multiline=False, password=True)
    window = TextArea(height=1, multiline=False, text="128000")
    notice = Label("")
    form = HSplit([
        Label("模型名称（区分大小写）"), name,
        Label("接口基础地址（不要填 /chat/completions 或 /messages）"), address,
        Label("密钥（不会进入聊天记录）"), key,
        Label("上下文窗口（总 tokens，不是每次回复长度）"), window,
        notice, Label("Tab/Shift+Tab 切换 · Esc 返回不保存"),
    ])
    profile_id = str(uuid4())
    try:
        while await _dialog(app, "新增模型 · 填写配置", form, (("保存", True), ("返回", None)), focus=name):
            try:
                profile = validate_model_profile({"model_backend": backend, "model_name": name.text,
                    "api_base": address.text, "api_key": key.text, "model_context_window_tokens": window.text})
            except ValueError as exc:
                notice.text = str(exc)
                continue
            result = await _request(app, agent, session_id, "add", {"profile_id": profile_id, "profile": profile})
            profile.clear()
            if result.get("ok"):
                return "保存成功。进入“选择已有模型”后可启用；尚未测试接口连接。"
            notice.text = str(result.get("message") or "保存结果未知，请返回列表确认；不要重复新增。")
    finally:
        key.text = ""
    return ""


# LLM: 显式 ID 是唯一选项身份；成功后发布公开模型显示事件，不能热改共享 Agent 的 config/backend。
# 函数用途: 分别选择当前会话模型或未来新会话默认值；修改默认值不会替换其他已打开会话。
async def _select_model(app, agent, session_id: str, runtime, *, operation: str = "select") -> str:
    result = await _request(app, agent, session_id, "list")
    if not result.get("ok"):
        return str(result.get("message") or "无法读取模型列表。")
    _publish_selection(runtime, result)
    rows = [row for row in result["profiles"] if row["id"] == "default" or row.get("available", True)]
    setting_default = operation == "set_default"
    current = result.get("default_selected", "default") if setting_default else result["selected"]
    choices = RadioList([(row["id"], f"{'● ' if row['id'] == current else ''}{row['model_name']}"
                         f" · {row['model_backend']} · {row['model_context_window_tokens']} tokens"
                         f"{'（部署默认）' if row['id'] == 'default' else ' · ' + row['api_base']}"
                         f"{'（管理员共享）' if row.get('shared') else ''}") for row in rows],
                        default=current, select_on_focus=True)
    title = "新会话默认模型 · 不修改已打开会话" if setting_default else "当前会话模型 · 不影响其他 TUI / IM 会话"
    selected = await _dialog(app, title, choices,
                              (("选择并保存", lambda: choices.current_value), ("返回", None)), focus=choices)
    if selected is None:
        return ""
    result = await _request(app, agent, session_id, operation, {"profile_id": selected})
    if not result.get("ok"):
        return str(result.get("message") or "选择结果未知，请重新打开列表确认。")
    row = next(row for row in result["profiles"] if row["id"] == selected)
    _publish_selection(runtime, result)
    if setting_default:
        return f"新会话默认使用 {row['model_name']}；当前及其他已打开会话的模型保持不变。"
    return f"本会话已选择 {row['model_name']}，上下文 {row['model_context_window_tokens']} tokens。下个工作片生效，当前执行不被中断。"


# LLM: 菜单始终在现有事件循环内；期间隔离聊天键盘处理，退出后恢复，异常不打印请求或密钥。
# 函数用途: 打开 /model 的快捷新增、选择、服务商管理和主动测试入口。
async def run_model_menu(app, agent, session_id: str, runtime) -> None:
    app._my_agent_model_menu_active = True
    message = "每个会话独立选模型；正在执行的工作片保持原模型。"
    try:
        while True:
            choices = RadioList([("select", "选择已有模型（当前会话）"), ("add", "新增模型（快捷）"),
                ("auth", "登录认证（ChatGPT Plus / Pro、通用 OAuth）"),
                ("provider_add", "新增服务商（支持多个模型）"), ("providers", "管理服务商 / 模型 / 请求头"),
                ("probe", "连接测试（短问候，不做任务）"),
                ("set_default", "新会话默认模型（不修改已有会话）"),
                ("sharing", "管理员共享模型（显式开放给其他用户）")], select_on_focus=True)
            action = await _dialog(app, "模型配置 /model", HSplit([Label(message), choices]),
                                   (("进入", lambda: choices.current_value), ("退出", None)), focus=choices)
            if action is None:
                return
            update = await _run_model_action(app, agent, session_id, runtime, action)
            message = update or message
    except Exception:  # noqa: BLE001 TUI 菜单错误不得关闭 Agent 或泄露秘密
        runtime.set_notice("模型菜单暂时不可用，聊天任务未被停止。", duration_seconds=6)
    finally:
        app._my_agent_model_menu_active = False
        app.invalidate()


# LLM: 只分发菜单结构化选项；网络与配置副作用仍由各动作的确认入口负责，未知选项不能默认切模型。
# 函数用途: 保持主菜单循环轻量，分别进入会话选择、默认值、服务商、共享和连接测试。
async def _run_model_action(app, agent, session_id: str, runtime, action: str) -> str:
    if action == "auth":
        from .tui_model_auth import manage_auth

        message = await manage_auth(app, agent, session_id)
        _publish_selection(runtime, await _request(app, agent, session_id, "list"))
        return message
    from .tui_provider_menu import manage_providers, test_connection
    from .tui_shared_model_menu import manage_shared_models

    if action in {"provider_add", "providers"}:
        return await manage_providers(app, agent, session_id, create=action == "provider_add")
    if action == "probe":
        return await test_connection(app, agent, session_id)
    if action in {"select", "set_default"}:
        return await _select_model(app, agent, session_id, runtime, operation=action)
    if action == "sharing":
        return await manage_shared_models(app, agent, session_id)
    if action == "add":
        return await _add_model(app, agent, session_id)
    return "未知模型菜单操作，配置未修改。"
