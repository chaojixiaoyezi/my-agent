# LLM: Goal drafts live only in the TUI modal until explicit save; authenticated backend owns CAS, identity and lifecycle.
# 模块用途: 主子代理共用目标编辑器，保存确认、版本冲突和放弃退出不阻塞聊天绘制或模型流。
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Float, HSplit
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Button, Dialog, Label, TextArea


# LLM: Client revision is an optimistic precondition, not an authority token. Token/time updates must not change it.
# 类用途: 保存目标的本地编辑基线，明确确认后才把草稿传给 Gateway。
@dataclass
class GoalDraft:
    goal_id: str
    run_id: str
    objective: str
    revision: int

    # LLM: Build only the supported structured save payload; no thread/owner override and no implicit resume flag.
    # 函数用途: 将用户按下保存时的正文与原始版本打包，后端冲突时保留这份草稿。
    def save_payload(self, text: str) -> dict:
        return {"operation": "save", "goal_id": self.goal_id, "run_id": self.run_id,
                "objective": text, "expected_revision": self.revision}


# LLM: IO runs off the UI loop; HTTP control errors use the public error field, while local errors use message. Preserve codes and never expose raw exceptions.
# 函数用途: 读取或保存当前代理目标，统一远端与本地错误文案，让版本冲突和权限拒绝能明确展示。
def _request_goal(agent, session_id: str, payload: dict) -> dict:
    body = {"user_id": "local-agent", "channel": "chat", "conversation_id": session_id, **payload}
    try:
        if getattr(agent, "gateway_client_only", False) is True:
            _status, result = agent.post_gateway_json("/client/goal", body, timeout=10.0)
            if result and result.get("ok") is False:
                return {**result, "message": result.get("message") or result.get("error")}
            return result or {"ok": False, "message": "Gateway 暂时不可用，草稿未清空。"}
        from ...agent.conversation.agent_control import AgentControlError
        from ...agent.conversation.goal_control import execute_agent_goal_control

        try:
            return execute_agent_goal_control(agent, scope=SimpleNamespace(**body), payload=payload)
        except AgentControlError as exc:
            return {"ok": False, "error_code": exc.error_code, "message": exc.message}
    except (OSError, RuntimeError, ValueError):
        return {"ok": False, "message": "暂时无法确认目标状态；保留草稿，请稍后重试。"}


# LLM: One modal captures its run ID; root resume help is display-only and never activates a paused goal or retargets a child.
# 函数用途: 查看并编辑当前 Goal；固定展示保存、放弃、停止与主目标恢复方法，不让保存反馈遮掉操作提示。
async def open_goal_editor(app, params) -> None:
    if getattr(app, "_my_agent_goal_editor_open", False):
        return
    row = params.agent_navigation.selected_goal()
    if row is None:
        return
    app._my_agent_goal_editor_open = True
    run_id = params.agent_navigation.snapshot().active_run_id
    draft = GoalDraft(str(row["goal_id"]), run_id, "", 0)
    done = asyncio.get_running_loop().create_future()
    text_area = TextArea(text="正在读取目标…", multiline=True, scrollbar=True, height=10,
                         read_only=Condition(lambda: busy))
    latest_area = TextArea(text="对比最新：点击“读取最新”后显示服务器正文，编辑草稿不被覆盖。", read_only=True, height=4, scrollbar=True)
    help_text = "Ctrl+S 保存 · Ctrl+G 放弃退出"
    help_text += (
        " · Esc 停止当前子代理（不保存）" if run_id else
        " · Esc 中断本轮（不保存，活动 Goal 继续）\n暂停目标：返回聊天后 /goal pause；恢复：/goal resume。"
    )
    feedback = Label("")
    busy = True
    bindings = KeyBindings()

    # LLM: Discard never sends a mutation. A submitted save must resolve before dismissing its confirmation state.
    # 函数用途: 放弃本地修改；保存已发出时先等待确定结果，避免用户误以为已撤销。
    def discard(event=None):
        if not busy and not done.done():
            done.set_result(None)

    # LLM: Saving captures the exact draft once, disables repeated submission, and updates the baseline only after an ACK.
    # 函数用途: 异步保存并显示确认；冲突或网络失败时继续保留用户输入。
    async def save():
        nonlocal busy
        if busy or draft.revision < 1:
            return
        busy = True
        submitted = text_area.text
        feedback.text = "正在保存…"
        app.invalidate()
        response = await asyncio.to_thread(_request_goal, params.agent, params.current_session_id, draft.save_payload(submitted))
        busy = False
        if response.get("ok") is True:
            draft.revision = int(response["goal"]["revision"])
            draft.objective = submitted
            feedback.text = "已保存。Ctrl+G 退出；继续编辑需再次 Ctrl+S 保存。"
        else:
            feedback.text = str(response.get("message") or "保存未获确认，草稿已保留。")
        app.invalidate()

    # LLM: Schedule one async operation in the existing app, not a nested event loop or blocking call.
    # 函数用途: 按钮与快捷键共用同一个保存入口。
    def submit(event=None):
        app.create_background_task(save())

    # LLM: Explicit refresh keeps edited text, displays server content, and adopts the observed revision for a later confirmed save.
    # 函数用途: 冲突后查看完整最新目标供用户比较，不清空草稿，也不自动覆盖服务器。
    async def reload_latest():
        nonlocal busy
        if busy:
            return
        busy = True
        response = await asyncio.to_thread(_request_goal, params.agent, params.current_session_id,
                                          {"operation": "view", "goal_id": draft.goal_id, "run_id": run_id})
        busy = False
        if response.get("ok") is True:
            latest_area.text = str(response["goal"]["objective"])
            draft.revision = int(response["goal"]["revision"])
            feedback.text = "下方是最新正文，上方保留草稿；比较后 Ctrl+S 才覆盖保存。"
        else:
            feedback.text = str(response.get("message") or "读取失败，草稿保留。")
        app.invalidate()

    # LLM: Modal Esc preserves the active agent stop contract and never saves or closes the draft implicitly.
    # 函数用途: 编辑时仍可立即请求停止当前代理。
    def stop(event):
        from .tui_keybindings import _handle_escape_keybinding

        _handle_escape_keybinding(event, params)

    bindings.add("c-s", eager=True)(submit)
    bindings.add("c-g", eager=True)(discard)
    bindings.add("escape", eager=True)(stop)
    dialog = Dialog(title=f"Goal · {params.agent_navigation.snapshot().active_name}",
                    body=HSplit([text_area, Label(help_text), feedback, latest_area]),
                    buttons=[Button("保存", handler=submit), Button("读取最新", handler=lambda: app.create_background_task(reload_latest())),
                             Button("放弃/退出", handler=discard)],
                    with_background=True, modal=False, width=Dimension(preferred=100))
    host = app._my_agent_model_float_container
    floating = Float(content=HSplit([dialog], key_bindings=bindings, modal=True))
    previous = app.layout.current_window
    host.floats.append(floating)
    app.layout.focus(text_area)
    app.invalidate()
    try:
        response = await asyncio.to_thread(_request_goal, params.agent, params.current_session_id,
                                          {"operation": "view", "goal_id": draft.goal_id, "run_id": run_id})
        busy = False
        if response.get("ok") is True:
            draft.revision = int(response["goal"]["revision"])
            draft.objective = str(response["goal"]["objective"])
            text_area.text = draft.objective
        else:
            text_area.text = str(row.get("objective") or "")
            feedback.text = str(response.get("message") or "未取得最新版本，暂不能保存；Ctrl+G 返回。")
        app.invalidate()
        await done
    finally:
        host.floats.remove(floating)
        app.layout.focus(previous)
        app._my_agent_goal_editor_open = False
        app.invalidate()
