# LLM: /model 是本机表单，不进入聊天、LLM 或 FileHistory；网络与文件操作在线程中执行，密钥只在掩码控件短暂保留。
# 模块用途: 在现有 TUI 中选择或新增模型，并在启动和选择确认后刷新欢迎区；不热改运行配置。

from __future__ import annotations

import asyncio
from uuid import uuid4

from prompt_toolkit.filters import has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Float, HSplit
from prompt_toolkit.widgets import Button, Dialog, Label, RadioList, TextArea

from ...agent.settings.model_profiles import (
    ModelProfileError,
    execute_model_profile_operation,
    validate_model_profile,
)


# LLM: modal 只占现有 Application 的浮层；Esc 关闭该表单，不复用停止代理的键盘路由。
# 函数用途: 显示一个可返回结果的对话框，关闭时恢复此前输入焦点，不销毁聊天界面。
async def _dialog(app, title: str, body, actions: tuple, *, focus=None):
    future = asyncio.get_running_loop().create_future()
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


# LLM: 请求走现有 owner 认证客户端，保存重试复用 profile_id；结果未知不能宣称失败或自动生成另一记录。
# 函数用途: 在不阻塞 TUI 绘制的情况下读取、保存或选择配置。
async def _request(app, agent, session_id: str, operation: str, **payload) -> dict:
    waiting = TextArea(text="正在等待配置操作确认…（不会向模型发请求）", read_only=True, height=2)
    busy = Float(content=Dialog(title="模型配置", body=waiting, buttons=[], with_background=True))
    host = app._my_agent_model_float_container
    previous = app.layout.current_window
    host.floats.append(busy)
    app.layout.focus(waiting)
    app.invalidate()
    try:
        return await _request_data(agent, session_id, operation, payload)
    finally:
        host.floats.remove(busy)
        app.layout.focus(previous)
        app.invalidate()


# LLM: 文件/网络操作离开事件线程；只有固定 ModelProfileError 文案可公开，未知异常不得带出秘密。
# 函数用途: 获取模型配置操作的确认结果，并安全处理可预期错误。
async def _request_data(agent, session_id: str, operation: str, payload: dict) -> dict:
    return await asyncio.to_thread(_request_data_sync, agent, session_id, operation, payload)


# LLM: 菜单与启动共用原配置入口；必须由启动前或后台线程调用，异常不泄漏秘密。
# 函数用途: 读取或保存用户模型配置；Gateway 模式发认证请求，本地模式访问私有配置文件。
def _request_data_sync(agent, session_id: str, operation: str, payload: dict) -> dict:
    try:
        if getattr(agent, "gateway_client_only", False) is True:
            return agent.request_models(session_id=session_id, operation=operation, **payload)
        return execute_model_profile_operation(agent, operation, payload)
    except ModelProfileError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception:  # noqa: BLE001 不显示带有密钥或路径的未知异常
        return {"ok": False, "message": "模型配置操作失败，请检查本机配置目录是否可写。"}


# LLM: 只认配置操作成功回执里的 selected ID；失败、取消和未选择的新增记录不得改变当前模型显示。
# 函数用途: 从脱敏模型列表更新 TUI，整个配置和密钥都不进入显示事件。
def _publish_selection(runtime, result: dict) -> None:
    if result.get("ok") is True:
        row = next((row for row in result["profiles"] if row["id"] == result["selected"]), None)
        if row is not None:
            runtime.publish_model_selection(row["model_name"])


# LLM: 启动读取在 Gateway readiness 后的后台执行；退出后的迟到结果不发布，也不阻止正常历史恢复。
# 函数用途: 新开或恢复 TUI 时读取已保存模型名；失败明确提示，不凭启动默认值声称同步成功。
def refresh_model_selection(agent, session_id: str, runtime, stop_event=None) -> None:
    result = _request_data_sync(agent, session_id, "list", {})
    if stop_event is not None and stop_event.is_set():
        return
    _publish_selection(runtime, result)
    if not result.get("ok"):
        runtime.set_notice("当前模型名称尚未同步，可打开 /model 重新确认。", duration_seconds=6)


# LLM: provider 的动作值是后端枚举，Auth 仅保留菜单项，不发登录请求、不落不完整记录。
# 函数用途: 选择新模型的接口类型。
async def _choose_interface(app):
    choices = RadioList([
        ("openai_compatible", "OpenAI 风格（Chat Completions）"),
        ("anthropic_compatible", "Anthropic 风格（Messages）"),
        ("auth", "Auth 认证（暂未开放）"),
    ], select_on_focus=True)
    while True:
        value = await _dialog(app, "新增模型 · 选择接口", choices,
                              (("下一步", lambda: choices.current_value), ("返回", None)), focus=choices)
        if value != "auth":
            return value
        await _dialog(app, "Auth 认证", Label("此选项已预留，暂未实现认证接入。"), (("返回", None),))


# LLM: 表单不使用持久 history；密码控件掩码，校验仅检查配置格式，保存不自动启用或测试接口。
# 函数用途: 填写模型名、基础地址、密钥及总上下文窗口，保存成功后返回主菜单。
async def _add_model(app, agent, session_id: str) -> str:
    backend = await _choose_interface(app)
    if backend is None:
        return ""
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
            result = await _request(app, agent, session_id, "add", profile_id=profile_id, profile=profile)
            profile.clear()
            if result.get("ok"):
                return "保存成功。进入“选择已有模型”后可启用；尚未测试接口连接。"
            notice.text = str(result.get("message") or "保存结果未知，请返回列表确认；不要重复新增。")
    finally:
        key.text = ""
    return ""


# LLM: 显式 ID 是唯一选项身份；成功后发布公开模型显示事件，不能热改共享 Agent 的 config/backend。
# 函数用途: 选择已保存模型，让当前用户后续工作片采用它。
async def _select_model(app, agent, session_id: str, runtime) -> str:
    result = await _request(app, agent, session_id, "list")
    if not result.get("ok"):
        return str(result.get("message") or "无法读取模型列表。")
    _publish_selection(runtime, result)
    rows = result["profiles"]
    choices = RadioList([(row["id"], f"{'● ' if row['id'] == result['selected'] else ''}{row['model_name']}"
                         f" · {row['model_backend']} · {row['model_context_window_tokens']} tokens"
                         f"{'（部署默认）' if row['id'] == 'default' else ' · ' + row['api_base']}") for row in rows],
                        default=result["selected"], select_on_focus=True)
    selected = await _dialog(app, "选择模型 · 当前用户后续工作片生效", choices,
                              (("选择并保存", lambda: choices.current_value), ("返回", None)), focus=choices)
    if selected is None:
        return ""
    result = await _request(app, agent, session_id, "select", profile_id=selected)
    if not result.get("ok"):
        return str(result.get("message") or "选择结果未知，请重新打开列表确认。")
    row = next(row for row in result["profiles"] if row["id"] == selected)
    _publish_selection(runtime, result)
    return f"已选择 {row['model_name']}，上下文 {row['model_context_window_tokens']} tokens。当前执行不被中断。"


# LLM: 菜单始终在现有事件循环内；期间隔离聊天键盘处理，退出后恢复，异常不打印请求或密钥。
# 函数用途: 打开 /model 的新增、选择、退出入口。
async def run_model_menu(app, agent, session_id: str, runtime) -> None:
    app._my_agent_model_menu_active = True
    message = "配置仅影响当前用户；正在执行的工作片保持原模型。"
    try:
        while True:
            choices = RadioList([("select", "选择已有模型"), ("add", "新增模型")], select_on_focus=True)
            action = await _dialog(app, "模型配置 /model", HSplit([Label(message), choices]),
                                   (("进入", lambda: choices.current_value), ("退出", None)), focus=choices)
            if action is None:
                return
            message = await (_add_model(app, agent, session_id) if action == "add" else _select_model(app, agent, session_id, runtime)) or message
    except Exception:  # noqa: BLE001 TUI 菜单错误不得关闭 Agent 或泄露秘密
        runtime.set_notice("模型菜单暂时不可用，聊天任务未被停止。", duration_seconds=6)
    finally:
        app._my_agent_model_menu_active = False
        app.invalidate()
