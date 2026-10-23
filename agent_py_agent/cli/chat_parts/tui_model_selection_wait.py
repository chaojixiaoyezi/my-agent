# LLM: 保存沿原模型配置入口，不取消已发送请求；Esc 仅撤去 UI，迟到回执仍按版本发布。
# 模块用途: 管理模型选择的可退出等待与保存后的菜单收口。
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from prompt_toolkit.widgets import Label


# LLM: 结构化返回只代表当前会话选择已确认，不能由成功文案推断。
# 类用途: 告诉顶层菜单保存完成，应收起并显示结果。
@dataclass(frozen=True)
class ModelSelectionSaved:
    message: str


# LLM: 只作为菜单局部控制流，不是请求取消或代理中断。
# 类用途: 用户退出等待时，直接退回聊天输入。
class ModelMenuDetached(Exception):
    pass


# LLM: 原后台请求独立于对话框 Future；未知终态释放 UI，未知运输仅在原任务或原 outbox 行存续时阻挡新选择。
# 函数用途: 保存本会话选择，允许等待时按 Esc 回聊天。
async def save_selection(app, agent, session_id, runtime, payload):
    from .tui_model_menu import _dialog, _publish_selection

    if getattr(app, "_my_agent_model_save_pending", False) and await _original_setting_pending(app):
        return {"ok": False, "message": "上次选择尚待确认，请等待原回执。"}
    app._my_agent_model_save_pending = True
    app._my_agent_model_save_pending_id = ""
    completion = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(_submit_original_setting(app, payload))
    app._my_agent_model_save_task = task

    # LLM: 唯一保存任务结算；终态未知不等于运输仍进行，不让旧回调改写较新保存任务的锁定状态。
    # 函数用途: 在保存结束后更新显示并给退出等待的用户确认。
    def settled(finished):
        try:
            result = finished.result()
        except (Exception, asyncio.CancelledError):
            result = {"ok": False, "control_state": "transport_unknown", "message": "选择结果未知，请重新打开列表确认。"}
        if getattr(app, "_my_agent_model_save_task", None) is finished:
            app._my_agent_model_save_pending = result.get("control_state") == "transport_unknown"
            if not app._my_agent_model_save_pending:
                app._my_agent_model_save_pending_id = ""
        if not completion.done():
            completion.set_result(result)
        elif app.is_running:
            accepted = _publish_selection(runtime, result)
            if not result.get("ok") or accepted and not result.get("warning"):
                runtime.set_notice("本会话模型已保存，待下次请求采用。" if result.get("ok") else
                                   str(result.get("message") or "选择结果未知，请重新打开列表确认。"), duration_seconds=8)
            app.invalidate()

    task.add_done_callback(settled)
    result = await _dialog(app, "正在保存本会话模型", Label("正在等待保存确认；Esc 只关闭此窗口，不取消保存。"),
                           (("返回聊天（继续确认）", None),), completion=completion)
    if result is None:
        if not task.done():
            runtime.set_notice("已返回聊天，模型选择仍在等待确认。", duration_seconds=8)
        raise ModelMenuDetached
    return result


# LLM: 菜单和文字设置复用同一原 outbox、claim、回执；Future 只观察，Esc 不取消持久操作或发第二份 POST。
# 函数用途: 按精确档案编号持久提交菜单选择并等待原终态。
async def _submit_original_setting(app, payload):
    from ...agent.conversation.models import new_id
    from .tui_actions import _ensure_control_operation_reconciler
    from .tui_control_delivery import TuiControlOperationEntry

    params = app._my_agent_control_params
    reconciler = await asyncio.to_thread(_ensure_control_operation_reconciler, params)
    entry = TuiControlOperationEntry(new_id("control"), f"/model {payload['profile_id']}", "model", model_payload=dict(payload))
    app._my_agent_model_save_pending_id = entry.message_id
    app._my_agent_model_save_reconciler = reconciler
    future = reconciler._setting_enqueuer.results.watch(entry.message_id)
    try:
        reconciler.enqueue_setting(entry)
    except Exception:
        reconciler._setting_enqueuer.results.abandon(entry.message_id)
        return {"ok": False, "control_state": "not_submitted", "message": "控制操作无法保存，选择未发送；请稍后再试。"}
    return await asyncio.wrap_future(future)


# LLM: 只读原任务和同一 outbox；读盘后复核任务/ID/对账器，身份已变则重新观察，不能用旧快照放行新未知请求。
#   磁盘读取移出事件循环，读取失败保守等待，不重发或重建旧操作身份。
# 函数用途: 放行已经结束的未知选择，同时保留仍在确认的原模型选择。
async def _original_setting_pending(app):
    while True:
        task = getattr(app, "_my_agent_model_save_task", None)
        if task is not None and not task.done():
            return True
        message_id = getattr(app, "_my_agent_model_save_pending_id", "")
        reconciler = getattr(app, "_my_agent_model_save_reconciler", None)
        if not message_id or reconciler is None:
            return False
        try:
            entries = await asyncio.to_thread(reconciler._read_entries)
        except Exception:
            return True
        if (task is not getattr(app, "_my_agent_model_save_task", None)
                or message_id != getattr(app, "_my_agent_model_save_pending_id", "")
                or reconciler is not getattr(app, "_my_agent_model_save_reconciler", None)):
            continue
        return message_id in entries
