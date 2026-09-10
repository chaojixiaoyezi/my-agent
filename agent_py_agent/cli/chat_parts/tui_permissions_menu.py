# LLM: /permissions 与 F4 是显式用户控制，枚举通过认证接口写 owner 策略；不进入 LLM，不解析用户普通聊天。
# 模块用途: 显示三档权限及解释、保存/取消，主代理与子代理页面共用；不默认选择 Full Access。

from __future__ import annotations

import asyncio

from prompt_toolkit.layout import HSplit
from prompt_toolkit.widgets import Label, RadioList

from ...agent.user_space.approval_mode import (
    APPROVAL_MODES,
    ApprovalModeError,
    execute_approval_mode_operation,
)
from .tui_model_menu import _dialog


# LLM: HTTP 和文件操作离开 UI 线程；服务端再次核验 owner，客户端隐藏选项不作为安全门。
# 函数用途: 获取或保存用户选择，失败只显示固定错误，不把策略正文外发。
def request_permissions(agent, session_id: str, operation: str, mode: str | None = None) -> dict:
    try:
        if getattr(agent, "gateway_client_only", False) is True:
            return agent.request_permissions(session_id=session_id, operation=operation, mode=mode)
        return execute_approval_mode_operation(agent.home_paths, operation, mode)
    except ApprovalModeError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception:  # noqa: BLE001 不打印策略或鉴权细节
        return {"ok": False, "message": "权限配置操作失败，未确认保存成功。"}


# LLM: 上下键只改变待选值，保存后才生效；Full Access 二次确认，取消/关窗不写文件，原模型回合不被中断。
# 函数用途: 打开模式选择菜单，明确本用户范围、子代理上限和下一回合生效边界。
async def run_permissions_menu(app, agent, session_id: str, runtime) -> None:
    result = await asyncio.to_thread(request_permissions, agent, session_id, "get")
    if not result.get("ok"):
        runtime.set_notice(result.get("message", "权限配置读取失败。"), duration_seconds=8)
        return
    choices = RadioList([(mode, label) for mode, label in APPROVAL_MODES.items()
                         if mode != "full-access" or result.get("is_admin")],
                        default=result["mode"], select_on_focus=True)
    body = HSplit([
        Label("权限模式 · 适用于本用户所有会话和子代理"), choices,
        Label("子代理始终限定自己家目录。SOUL 等本人确认、禁用工具、灾难命令保护不变。\n"
              "普通用户无法开启 Full Access；本机 TUI 默认管理员身份不等于默认全盘权限。\n"
              "审批选择保存后生效；路径权限下一回合生效，不打断当前工作。\n"
              "↑↓ 选择 · Enter 保存 · Tab 切换按钮 · Esc 取消 · F4 也可打开本菜单"),
    ])
    mode = await _dialog(app, "权限与审批", body,
                         (("保存", lambda: choices.current_value), ("取消", None)), focus=choices)
    if mode is None:
        return
    if mode == "full-access" and result["mode"] != mode:
        if not await _dialog(app, "确认开启 Full Access", Label(
            "仅管理员：主代理可读写家目录外的文件、运行系统命令。\n"
            "默认仍从自己的家目录开始工作；不自动授权子代理访问全盘。\n"
            "请确认你信任接下来交给代理的任务。"), (("确认开启", True), ("取消", False))):
            return
    saved = await asyncio.to_thread(request_permissions, agent, session_id, "set", mode)
    runtime.set_notice(str(saved.get("message") or "未确认保存成功。"), duration_seconds=12)


# LLM: 快捷键只打开同一个菜单；modal 已打开时不叠加，不能借快捷键批准当前工具。
# 函数用途: 让用户在普通输入或审批等待时通过 F4 打开模式选择。
def open_permissions_menu(event, params, *, mode: str | None = None) -> None:
    host = event.app._my_agent_model_float_container
    if host.floats or getattr(event.app, "_my_agent_model_menu_active", False):
        return
    event.app._my_agent_model_menu_active = True

    # LLM: 菜单请求发出前占用标志，任何退出都释放；F4 连按不能并发保存多个模式。
    # 函数用途: 在后台打开同一个表单，错误或取消后恢复再次打开能力。
    async def run():
        try:
            session_id = str(params.current_session_id or "default")
            if mode is None:
                await run_permissions_menu(event.app, params.agent, session_id, params.tui_runtime)
            else:
                result = await asyncio.to_thread(request_permissions, params.agent, session_id, "set", mode)
                params.tui_runtime.set_notice(str(result.get("message") or "权限配置操作失败。"), duration_seconds=12)
        finally:
            event.app._my_agent_model_menu_active = False
            event.app.invalidate()

    event.app.create_background_task(run())
